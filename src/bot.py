"""
bot.py — Single bot game loop.
Handles: wait for joiner assignment -> recover if needed -> wait for start -> turn loop -> reincarnate via rejoin_queue.
Join logic is fully delegated to joiner.py.
"""

import asyncio
import time
from src.api_client import ApiClient
from src.state_manager import GameState
from src.strategy import decide_action
from src.loot import pickup_all_valuable, equip_best
from src.combat import get_smart_swap_action, select_target
from src.logger import BotLogger
from src.god_mode import GodModeIntel
from src.config import TURN_INTERVAL, STATE_POLL_INTERVAL


def is_api_error(resp: dict) -> bool:
    """Check if API response is an error. Handles both wrapped and unwrapped formats."""
    if not resp:
        return True
    # Explicit error
    if resp.get("success") is False:
        return True
    # Has error object
    if "error" in resp and isinstance(resp["error"], dict):
        return True
    return False


def get_error_info(resp: dict) -> tuple[str, str]:
    """Extract error message and code from API response."""
    if not resp:
        return "Empty response", "EMPTY"
    error = resp.get("error", {})
    if isinstance(error, dict):
        return error.get("message", "Unknown error"), error.get("code", "UNKNOWN")
    return str(error), "UNKNOWN"


class MoltyBot:
    """Single bot lifecycle — handles registration, recovery, game loop, reincarnation."""

    def __init__(
        self,
        account: dict,
        bot_index: int,
        all_api_keys: list[str],
        all_bot_ids: set = None,
        god_cache=None,
        game_id: str = "",
        agent_id: str = "",
        rejoin_queue: asyncio.Queue = None,
    ):
        self.account = account
        self.name = account["name"]
        self.api_key = account["apiKey"]
        self.account_id = account.get("accountId", "")
        self.bot_index = bot_index

        self.api = ApiClient(self.api_key, bot_index=bot_index)
        self.logger = BotLogger(self.name, bot_index)

        self.god_cache = god_cache
        self.intel = None

        self.game_id = game_id
        self.agent_id = agent_id
        self.all_bot_ids = all_bot_ids or set()
        self.turn_count = 0
        self._last_state: GameState | None = None
        self._god_mode_logged = False  # log GODMODE ACTIVE only once per game

        # Joiner signals this event when it assigns a new game after reincarnation
        self._ready_event: asyncio.Event = asyncio.Event()
        if game_id:
            self._ready_event.set()  # already assigned at startup

        # Queue to request a new game from joiner after reincarnation
        self.rejoin_queue: asyncio.Queue = rejoin_queue or asyncio.Queue()

        self._running = True

    async def run(self):
        """Full bot lifecycle with reincarnation loop. Join is fully handled by joiner."""
        try:
            while self._running:
                # 1. Wait for joiner to assign a game (initial or after reincarnation)
                await self._ready_event.wait()

                if not self._running:
                    break

                # 2. If ALREADY_IN_GAME case: agent_id is empty.
                # Because we can't get agent_id without God Mode, we just wait for the game to end.
                if self.game_id and not self.agent_id:
                    self.logger.warn(
                        f"Stuck in running game. Waiting for {self.game_id[:8]} to end..."
                    )
                    await self._wait_for_game_to_finish(self.game_id)
                    await self._request_rejoin()
                    continue

                if not self.game_id or not self.agent_id:
                    self.logger.warn("No game assigned. Requeuing...")
                    await self._request_rejoin()
                    continue

                # Set fallback game number immediately (UUID short)
                self.logger.update_game_number(self.game_id[:4])

                self.logger.info(
                    f"game={self.game_id[:8]}.. agent={self.agent_id[:12]}.."
                )

                # Initialize God Mode V2 for this game
                if self.god_cache:
                    await self.god_cache.ensure_listening(self.game_id, self.api)
                self.intel = GodModeIntel(self.god_cache, self.game_id)

                # 3. Wait for game to start
                started = await self._wait_for_start()
                if not started:
                    self.logger.warn("Game start timeout. Requeuing...")
                    await self._request_rejoin()
                    continue

                # 4. Main game loop
                await self._game_loop()

                # 5. Game ended — reincarnate via joiner
                if self._running:
                    self.logger.info("Game ended. Requeuing...")
                    await self._request_rejoin()

        except asyncio.CancelledError:
            self.logger.shutdown()
        except Exception as e:
            self.logger.error(f"Fatal error: {type(e).__name__}: {e}")
            import traceback

            traceback.print_exc()
        finally:
            await self.api.close()

    async def _request_rejoin(self):
        """Clear state and signal joiner to assign a new game."""
        self.game_id = ""
        self.agent_id = ""
        self.turn_count = 0
        self._god_mode_logged = False
        self._ready_event.clear()
        await self.rejoin_queue.put(self.account)

    async def stop(self):
        self._running = False
        self._ready_event.set()  # unblock wait() so run() can exit cleanly

    # --- Waiting for Game to Finish (Silent Polling) ---

    async def _wait_for_game_to_finish(self, game_id: str):
        """
        Polls game status silently every 15s until 'finished'.
        Used when bot dies or gets ALREADY_IN_GAME (stuck) to prevent spam.
        """
        if not game_id:
            return

        self.logger.info(f"Waiting silently for game {game_id[:8]} to finish...")

        while self._running:
            # Random jitter to prevent thunderherd from 50 dead bots
            jitter = (self.bot_index % 100) * 0.1
            await asyncio.sleep(15 + jitter)

            game_resp = await self.api.get_game_info(game_id)

            # If error, ignore and keep waiting (silent)
            if is_api_error(game_resp):
                msg, code = get_error_info(game_resp)
                if code == "GAME_NOT_FOUND":
                    self.logger.info("Game no longer exists on server. Free to join.")
                    break
                continue

            data = game_resp.get("data", game_resp)
            status = data.get("status", "")

            if status == "finished":
                self.logger.info("Game finally finished. Free to join new game.")
                break

    # --- Wait for Start ---

    async def _wait_for_start(self) -> bool:
        """Poll game info until status is 'running' (avoids get_state rate limit)."""
        if not self.game_id or not self.agent_id:
            return False

        self.logger.info("Waiting for game to start...")

        attempts = 0
        max_attempts = 20  # 20 * 15s = 300s (5 minutes)

        while self._running and attempts < max_attempts:
            game_resp = await self.api.get_game_info(self.game_id)

            if is_api_error(game_resp):
                msg, code = get_error_info(game_resp)
                self.logger.warn(f"Game poll error: {code}")
                await asyncio.sleep(15)
                attempts += 1
                continue

            data = game_resp.get("data", game_resp)
            status = data.get("status", "")

            # Extract game number from name (e.g. "30141st MoltyRoyale...")
            game_name = data.get("name", "")
            if game_name:
                import re

                match = re.search(r"\d+", game_name)
                if match:
                    self.logger.update_game_number(match.group(0))

            if status == "running":
                self.logger.info("Game started!")
                return True
            if status == "finished":
                self.logger.info("Game already finished.")
                return False

            count = data.get("agentCount", data.get("currentAgents", "?"))
            max_a = data.get("maxAgents", "?")
            self.logger.info(f"Waiting... ({count}/{max_a})")

            await asyncio.sleep(15)
            attempts += 1

        return False

    # --- Main Game Loop ---

    async def _game_loop(self):
        """Main 61-second turn loop."""
        if not self.game_id or not self.agent_id:
            return

        _retry_none_count = 0

        while self._running:
            try:
                self.turn_count += 1

                # Jitter State Fetch: Spread out 100 API requests over 5 seconds based on index
                # This prevents HTTP 502 Bad Gateway / Thundering Herd when fetching states
                jitter_delay = (self.bot_index % 100) * 0.05
                await asyncio.sleep(jitter_delay)

                # ── Step 1: Fetch agent state ──
                # God Mode (get_full_state) disabled: API only allows spectator
                # access for finished games, not running ones.
                state_resp = await self.api.get_state(self.game_id, self.agent_id)

                # Handle state errors — fallback to last known state
                if isinstance(state_resp, Exception):
                    self.logger.error(f"State exception: {state_resp}")
                    if self._last_state:
                        state_resp = None
                    else:
                        await asyncio.sleep(TURN_INTERVAL)
                        continue

                if state_resp is not None and is_api_error(state_resp):
                    msg, code = get_error_info(state_resp)

                    if code != "RATE_LIMIT_EXCEEDED":
                        self.logger.error(f"State failed: {code}")

                    if code in (
                        "AGENT_NOT_FOUND",
                        "GAME_NOT_FOUND",
                        "GAME_NOT_RUNNING",
                    ):
                        self.logger.info("Game/agent gone. Reincarnating...")
                        break

                    if self._last_state:
                        state_resp = None
                    else:
                        await asyncio.sleep(TURN_INTERVAL)
                        continue

                # Build state — fresh or fallback
                if state_resp is not None:
                    state = GameState.from_api(state_resp)
                    # Detect kills
                    if self._last_state and state.kills > self._last_state.kills:
                        kills_gained = state.kills - self._last_state.kills
                        for _ in range(kills_gained):
                            self.logger.kill("Enemy", state.region_name)

                    self._last_state = state  # save for next time
                else:
                    state = self._last_state

                # Check death
                if not state.is_alive:
                    self.logger.death()
                    self.logger.info(f"Kills: {state.kills}")
                    await self._wait_for_game_to_finish(self.game_id)
                    break

                # Check game finished
                if state.is_finished:
                    result = state.result
                    is_winner = result.get("isWinner", False)
                    rewards = result.get("rewards", 0)
                    rank = result.get("finalRank", "?")
                    self.logger.info(
                        f"Game over! Rank: #{rank}, Winner: {is_winner}, "
                        f"Rewards: {rewards}, Kills: {state.kills}"
                    )
                    break

                if not state.is_running:
                    await asyncio.sleep(STATE_POLL_INTERVAL)
                    continue

                # Log state summary
                self.logger.state_summary(
                    region_name=state.region_name,
                    terrain=state.terrain,
                    weather=state.weather,
                    weapon_name=state.weapon.name,
                    enemies=len(state.visible_enemies),
                    monsters=len(state.visible_monsters),
                    items=len(state.items_in_region()),
                    kills=state.kills,
                    is_death_zone=state.is_death_zone,
                )

                # Log God Mode activation once per game (not every turn)
                if self.intel and self.intel.available and not self._god_mode_logged:
                    agents = len(self.intel.all_agents)
                    regions = len(self.intel.all_regions)
                    self.logger.godmode(
                        f"ACTIVE — agents={agents} regions={regions} game_status={self.intel.game_status}"
                    )
                    self._god_mode_logged = True

                # ── Step 2: FREE actions — pickup then equip ──
                await pickup_all_valuable(
                    state, self.api, self.game_id, self.agent_id, self.logger, retries=1
                )

                _pending_ids = {dz.get("id", "") for dz in state.pending_deathzones}
                target_action = select_target(
                    state, self.intel, self.all_bot_ids, _pending_ids, False
                )
                smart_swap = None
                if (
                    target_action
                    and target_action.get("type") == "attack"
                    and "_dist" in target_action
                ):
                    smart_swap = get_smart_swap_action(state, target_action["_dist"])

                if smart_swap:
                    # Execute smart swap
                    result = await self.api.do_action(
                        self.game_id,
                        self.agent_id,
                        {"type": "equip", "itemId": smart_swap["itemId"]},
                        retries=1,
                    )
                    if result and result.get("success"):
                        self.logger.equip(smart_swap.get("_name", "Weapon"))
                        # Refetch state so the main action uses the right weapon
                        state_resp = await self.api.get_state(
                            self.game_id, self.agent_id
                        )
                        if not is_api_error(state_resp):
                            state = GameState.from_api(state_resp)
                            self._last_state = state
                            # Refresh _pending_ids dari state baru
                            _pending_ids = {
                                dz.get("id", "") for dz in state.pending_deathzones
                            }
                else:
                    await equip_best(
                        state,
                        self.api,
                        self.game_id,
                        self.agent_id,
                        self.logger,
                        retries=1,
                    )

                # ── Step 5: Main action (retry=2) ──
                action = decide_action(state, self.intel, self.all_bot_ids)

                if action is None:
                    _retry_none_count += 1
                    if _retry_none_count <= 3:
                        self.logger.warn(
                            f"decide_action returned None (retry {_retry_none_count}/3). Re-evaluating..."
                        )
                        # Bot akan skip sisa kode di bawah, kembali ke awal while loop,
                        # dan nge-fetch state terbaru tanpa menunggu 60 detik.
                        continue
                    else:
                        # Udah mentok re-evaluate 3 kali tetep dapet None,
                        # berarti murni lagi nunggu EP. Gak usah kirim apa-apa ke server.
                        self.logger.info("Standing by. Auto-regen +1 EP...")
                        _retry_none_count = 0
                        # Lanjut ke baris absolute clock sync di bawah untuk tidur
                else:
                    _retry_none_count = 0
                    self._log_action(action, state)

                    clean_action = {
                        k: v for k, v in action.items() if not k.startswith("_")
                    }

                    thought = {
                        "reasoning": (
                            f"T{self.turn_count} HP:{state.hp} EP:{state.ep} "
                            f"W:{state.weapon.name} K:{state.kills}"
                        ),
                        "plannedAction": clean_action.get("type", "rest"),
                    }

                    result = await self.api.do_action(
                        self.game_id, self.agent_id, clean_action, thought, retries=2
                    )

                    if is_api_error(result):
                        msg, code = get_error_info(result)
                        self.logger.warn(f"Action failed: {code}")
                        if "cooldown" in msg.lower() or "wait" in msg.lower():
                            await asyncio.sleep(5)

                # Wait for next turn: Absolute Clock Sync
                # Rather than sleeping exactly 61s (which causes accumulated time drift),
                # we calculate the time remaining to the next absolute minute boundary.
                # This ensures all 100 bots wake up and execute exactly simultaneously.
                now = time.time()
                drift_correction = TURN_INTERVAL - (now % TURN_INTERVAL)
                await asyncio.sleep(drift_correction)

            except Exception as e:
                self.logger.error(f"Fatal error in game loop: {e}")
                import traceback

                traceback.print_exc()

                now = time.time()
                drift_correction = TURN_INTERVAL - (now % TURN_INTERVAL)
                await asyncio.sleep(drift_correction)

    # --- Logging helpers---

    def _log_action(self, action: dict, state: GameState):
        action_type = action.get("type", "")
        reason = action.get("_reason", "")
        name = action.get("_name", "")

        # Log God Mode-powered decisions prominently
        reason_upper = reason.upper()
        if any(
            kw in reason_upper for kw in ("KILLER", "SULTAN", "DOOMSDAY", "SAFE CENTER")
        ):
            self.logger.godmode(f"Decision: {reason}")

        if action_type == "attack":
            self.logger.attack(
                action.get("_name", action.get("targetId", "")),
                state.region_name,
                f"HP:{action.get('_hp', '?')}",
            )
        elif action_type == "move":
            to_name = action.get("_to_name", action.get("regionId", ""))
            if "flee" in reason.lower() or "evacuat" in reason.lower():
                self.logger.flee(state.region_name, to_name, reason)
            else:
                self.logger.move(state.region_name, to_name, reason)
        elif action_type == "explore":
            self.logger.explore(state.region_name)
        elif action_type == "rest":
            self.logger.rest()
        elif action_type == "use_item":
            self.logger.heal(name, action.get("_hp_before", state.hp), state.hp)
        elif action_type == "interact":
            self.logger.interact(name, state.region_name)
        else:
            self.logger.decision(f"{action_type}: {reason}")
