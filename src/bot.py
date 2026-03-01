"""
bot.py — Single bot game loop.
Handles: join game -> recover stuck accounts -> wait -> turn loop -> reincarnate.
"""

import asyncio
import random
import time
from src.api_client import ApiClient
from src.state_manager import GameState
from src.god_mode import GodModeIntel
from src.strategy import decide_action, TargetLock
from src.loot import pickup_all_valuable, equip_best
from src.combat import get_smart_swap_action, select_target
from src.logger import BotLogger
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
    ):
        self.name = account["name"]
        self.api_key = account["apiKey"]
        self.account_id = account.get("accountId", "")
        self.bot_index = bot_index

        self.api = ApiClient(self.api_key, bot_index=bot_index)
        self.logger = BotLogger(self.name, bot_index)
        self.intel = GodModeIntel()
        self.god_cache = god_cache  # shared god mode cache

        self.game_id = ""
        self.agent_id = ""
        self.all_bot_ids = all_bot_ids or set()
        self.turn_count = 0
        self.target_lock = TargetLock()
        self._last_state: GameState | None = None  # fallback for rate-limited state
        self._running = True

    async def run(self):
        """Full bot lifecycle with reincarnation loop."""
        try:
            while self._running:
                # 1. Find/join/recover game
                joined = await self._join_or_recover()
                if not joined:
                    self.logger.warn("Could not join any game. Retrying in 3s...")
                    await asyncio.sleep(3)
                    continue

                # 2. Wait for game to start
                started = await self._wait_for_start()
                if not started:
                    self.logger.warn("Game start timeout. Leaving room...")
                    self.game_id = ""
                    self.agent_id = ""
                    await asyncio.sleep(5)
                    continue

                # 3. Main game loop
                await self._game_loop()

                # 4. Game ended -- reincarnate
                if self._running:
                    self.logger.info("Game ended. Waiting 10s before next game...")
                    self.game_id = ""
                    self.agent_id = ""
                    self.turn_count = 0
                    self.target_lock = TargetLock()
                    await asyncio.sleep(10)

        except asyncio.CancelledError:
            self.logger.shutdown()
        except Exception as e:
            self.logger.error(f"Fatal error: {type(e).__name__}: {e}")
            import traceback

            traceback.print_exc()
        finally:
            await self.api.close()

    async def stop(self):
        self._running = False

    # --- Join / Recover ---

    async def _join_or_recover(self) -> bool:
        """
        Flow:
        1. Try to join a waiting game immediately. (Fastest approach)
        2. If join fails because account is already in a game, extract game ID and recover.
        3. If no waiting games, do a quick recovery scan. If not stuck, poll for new waiting games.
        """
        # Step 1: Look for waiting game immediately
        self.logger.info("Looking for a waiting game to join...")
        games_resp = await self.api.find_games("waiting")
        games = self._extract_games(games_resp)

        # Filter free games only and ensure they are not fully packed yet
        free_games = [
            g
            for g in games
            if g.get("entryType") == "free"
            and g.get("agentCount", 0) < g.get("maxAgents", 100)
        ]

        if free_games:
            # Sort games by current agents (highest first, so we join games about to start)
            free_games.sort(key=lambda g: g.get("agentCount", 0), reverse=True)

            # Shotgun Join: Try games one by one instantly if full
            for game in free_games:
                game_id = game.get("id", "")
                game_name = game.get("name", game_id)
                self.logger.info(
                    f'Found waiting free game: "{game_name}" ({game_id[:8]}...) - Agents: {game.get("agentCount")}/{game.get("maxAgents", 100)}'
                )

                result = await self._try_register(game_id, game_name)

                if result == "ok":
                    return True
                elif result == "recover":
                    # Account stuck — try fast-track recover
                    return await self._recover_existing_game()
                # If result == "fail" (e.g. MAX_AGENTS_REACHED), loop continues to next game immediately

        else:
            self.logger.warn("No free waiting games with empty slots available.")

        # Step 2: If we failed to join any or none exist, maybe we are stuck in a RUNNING game.
        # Let's do a full scan JUST IN CASE we disconnected.
        recovered = await self._recover_existing_game()
        if recovered:
            return True

        # Step 3: If we are completely free (not in any game), instead of exiting and waiting 3 seconds
        # in the main loop, we will enter an aggressive polling loop to snipe new games.
        self.logger.info("Bot is free. Polling for a new game...")

        # Jitter start to prevent all bots from polling at the exact same millisecond (Thundering Herd)
        await asyncio.sleep(random.uniform(0.5, 2.0))

        for _ in range(15):  # Poll before returning False to reset connection
            if not self._running:
                return False

            # Wait 4-7 seconds between checks to avoid Cloudflare 502/429 blocks
            await asyncio.sleep(random.uniform(4.0, 7.0))

            games_resp = await self.api.find_games("waiting")

            if is_api_error(games_resp):
                msg, code = get_error_info(games_resp)
                if code in ("HTTP_502", "HTTP_429", "RATE_LIMIT_EXCEEDED"):
                    self.logger.warn(
                        f"Server anti-spam triggered ({code}). Cooling down..."
                    )
                    await asyncio.sleep(15)
                    return False  # Escape polling loop to cooldown safely
                continue

            games = self._extract_games(games_resp)
            fresh_games = [
                g
                for g in games
                if g.get("entryType") == "free"
                and g.get("agentCount", 0) < g.get("maxAgents", 100)
            ]

            if fresh_games:
                self.logger.info("New game detected! Sniping slot...")
                # Exit polling loop and let the main run() loop restart the process
                # which will immediately catch it in Step 1.
                return False

        return False

        game = free_games[0]
        game_id = game.get("id", "")
        game_name = game.get("name", game_id)
        self.logger.info(f'Found waiting free game: "{game_name}" ({game_id[:8]}...)')

        result = await self._try_register(game_id, game_name)
        if result == "ok":
            return True
        elif result == "recover":
            # Account stuck — try recover again
            return await self._recover_existing_game()

        return False

    def _extract_games(self, resp: dict) -> list:
        """Extract games list from various response formats."""
        if not resp:
            return []
        # Format: {success: true, data: [...]}
        data = resp.get("data", [])
        if isinstance(data, list):
            return data
        # Format: {data: {games: [...]}}
        if isinstance(data, dict):
            return data.get("games", [])
        # Format: bare list (shouldn't happen with our api_client)
        if isinstance(resp, list):
            return resp
        return []

    def _extract_game_id_from_error(self, error_msg: str) -> str:
        """Try to extract a game ID (UUID) from an error message."""
        import re

        match = re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", error_msg
        )
        return match.group(0) if match else ""

    async def _try_register(self, game_id: str, game_name: str) -> str:
        """
        Try to register in a game.
        Returns: "ok", "recover", or "fail"
        """
        if not game_id:
            return "fail"

        self.logger.info(f'Registering in "{game_name}"...')
        reg = await self.api.register_agent(game_id, self.name)

        if not is_api_error(reg):
            # Success - extract agent data
            agent_data = reg.get("data", reg)
            if isinstance(agent_data, dict) and "id" in agent_data:
                self.game_id = game_id
                self.agent_id = agent_data["id"]
            elif isinstance(agent_data, dict) and "self" in agent_data:
                self.game_id = game_id
                self.agent_id = agent_data["self"].get("id", "")
            else:
                # Try to find our ID from the response
                self.game_id = game_id
                self.agent_id = str(agent_data.get("id", agent_data.get("agentId", "")))

            if self.agent_id:
                self.all_bot_ids.add(self.agent_id)
                self.logger.startup(game_name, self.name)
                self.logger.info(f"Agent ID: {self.agent_id}")
                return "ok"
            else:
                self.logger.warn(f"Registered but no agent ID in response: {reg}")
                return "fail"

        msg, code = get_error_info(reg)
        self.logger.warn(f"Register failed: {msg} ({code})")

        if code == "ACCOUNT_ALREADY_IN_GAME":
            # Extract the Game ID directly from the message if possible
            # Message example: "Account is already in another game (waiting or running). Only one game per account at a time. Current game: be6a46a7-6d96-47f0-b716-154fa47ea08e"
            extracted_game_id = self._extract_game_id_from_error(msg)
            if extracted_game_id:
                self.logger.info(f"Extracted Game ID from error: {extracted_game_id}")
                # Save it so _recover_existing_game can prioritize it
                self._stuck_game_id = extracted_game_id
            return "recover"

        if code == "ONE_AGENT_PER_API_KEY":
            return "recover"

        if code in ("MAX_AGENTS_REACHED", "GAME_ALREADY_STARTED"):
            return "fail"
        return "fail"

    async def _recover_existing_game(self) -> bool:
        """
        Recover agent ID when account is already in a game.
        Uses God Mode to find our agent by accountId (fallback to name).
        """

        # 1. Fast Track: If we extracted the game ID from the error message
        stuck_id = getattr(self, "_stuck_game_id", None)
        if stuck_id:
            self.logger.info(f"Fast-track recovery for game: {stuck_id}")
            success = await self._check_game_for_recovery(stuck_id)
            if success:
                self._stuck_game_id = None  # Clear it
                return True
            else:
                self.logger.warn(f"Fast-track recovery failed. Cannot recover.")
                self._stuck_game_id = None  # Clear it
                return False

        self.logger.warn("No fast-track game ID available to recover.")
        return False

    async def _check_game_for_recovery(self, gid: str, gname: str = None) -> bool:
        """Helper to check a specific game ID for our agent"""
        if not gname:
            gname = gid

        # Try God Mode to find our agent
        full_state = await self.api.get_full_state(gid)
        if is_api_error(full_state):
            msg, code = get_error_info(full_state)
            if code == "RATE_LIMIT_EXCEEDED":
                self.logger.warn(f"Rate limited during recovery scan. Sleeping 5s...")
                await asyncio.sleep(5)
            return False

        # God Mode response might be wrapped or unwrapped
        data = full_state.get("data", full_state)
        agents = data.get("agents", [])

        for agent in agents:
            agent_account_id = agent.get("accountId", "")

            # Compare by accountId if available, fallback to name
            is_match = False
            if self.account_id and agent_account_id:
                is_match = agent_account_id == self.account_id
            else:
                is_match = agent.get("name", "") == self.name

            if is_match:
                self.game_id = gid
                self.agent_id = agent.get("id", "")
                self.all_bot_ids.add(self.agent_id)

                is_alive = agent.get("isAlive", True)
                hp = agent.get("hp", 0)
                status = data.get("status", "")

                if not status:
                    game_resp = await self.api.get_game_info(gid)
                    if not is_api_error(game_resp):
                        status = game_resp.get("data", game_resp).get("status", "")

                # If the game hasn't started yet, agents might be flagged as dead/unspawned
                if is_alive or status == "waiting":
                    self.logger.info(
                        f'Recovered! Game: "{gname}", Agent: "{self.name}" (HP:{hp}, Status:{status})'
                    )
                    return True
                else:
                    self.logger.info(
                        f'Found in "{gname}" but DEAD. Leaving game to register new one...'
                    )
                    # Don't wait 2.7 hours, just return False to retry joining a new game
                    return False
        return False

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
                self.logger.warn(f"Game poll error: {msg} ({code})")
                await asyncio.sleep(15)
                attempts += 1
                continue

            data = game_resp.get("data", game_resp)
            status = data.get("status", "")

            if status == "running":
                self.logger.info("Game started!")
                return True
            if status == "finished":
                self.logger.info("Game already finished.")
                return False

            count = data.get("agentCount", data.get("currentAgents", "?"))
            max_a = data.get("maxAgents", "?")
            self.logger.info(f"Waiting... ({count}/{max_a} agents)")

            await asyncio.sleep(15)
            attempts += 1

        return False

    # --- Main Game Loop ---

    async def _game_loop(self):
        """Main 61-second turn loop."""
        if not self.game_id or not self.agent_id:
            return

        while self._running:
            try:
                self.turn_count += 1

                # Jitter State Fetch: Spread out 100 API requests over 5 seconds based on index
                # This prevents HTTP 502 Bad Gateway / Thundering Herd when fetching states
                jitter_delay = (self.bot_index % 100) * 0.05
                await asyncio.sleep(jitter_delay)

                # ── Step 1: Parallel fetch — state + god mode (shared cache) ──
                god_source = self.god_cache if self.god_cache else self.api
                state_resp, full_state_resp = await asyncio.gather(
                    self.api.get_state(self.game_id, self.agent_id),
                    god_source.get_full_state(self.game_id),
                    return_exceptions=True,
                )

                # Handle state errors — fallback to last known state
                if isinstance(state_resp, Exception):
                    self.logger.error(f"State exception: {state_resp}")
                    if self._last_state:
                        self.logger.info("Using last known state (fallback)")
                        state_resp = None  # flag to use fallback below
                    else:
                        await asyncio.sleep(TURN_INTERVAL)
                        continue

                if state_resp is not None and is_api_error(state_resp):
                    msg, code = get_error_info(state_resp)

                    if code == "RATE_LIMIT_EXCEEDED":
                        self.logger.warn("State Rate Limited (using fallback)")
                    else:
                        self.logger.error(f"State failed: {msg} ({code})")

                    if code in (
                        "AGENT_NOT_FOUND",
                        "GAME_NOT_FOUND",
                        "GAME_NOT_RUNNING",
                    ):
                        self.logger.info("Game/agent gone. Reincarnating...")
                        break

                    if self._last_state:
                        self.logger.info("Using last known state (fallback)")
                        state_resp = None  # flag to use fallback below
                    else:
                        await asyncio.sleep(TURN_INTERVAL)
                        continue

                # Build state — fresh or fallback
                if state_resp is not None:
                    state = GameState.from_api(state_resp)
                    self._last_state = state  # save for next time
                else:
                    state = self._last_state

                # Check death
                if not state.is_alive:
                    self.logger.death()
                    self.logger.info(f"Final stats: kills={state.kills}")
                    # Reincarnate immediately instead of waiting for game to finish
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

                # Update logger stats
                self.logger.update_stats(
                    hp=state.hp,
                    ep=state.ep,
                    moltz=state.moltz_count,
                    bag_count=state.bag_count,
                )

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

                # ── Step 2: Process god mode result (already fetched in parallel) ──
                god_mode_ok = False
                if isinstance(full_state_resp, Exception):
                    self.intel.available = False
                    self.logger.godmode(f"Error: {full_state_resp}")
                else:
                    try:
                        self.intel.update(full_state_resp)
                        if self.intel.available:
                            weak = self.intel.find_weak_enemies()
                            weapons = self.intel.find_high_value_weapons()
                            moltz = self.intel.find_moltz_locations()
                            alive_count = sum(
                                1 for a in self.intel.all_agents if a.get("isAlive")
                            )
                            self.logger.godmode(
                                f"Map: {alive_count} agents, "
                                f"{len(weak)} weak, "
                                f"{len(weapons)} weapons, "
                                f"{len(moltz)} moltz"
                            )
                            god_mode_ok = True
                        else:
                            self.logger.godmode("Failed -- using normal vision")
                    except Exception as e:
                        self.intel.available = False
                        self.logger.godmode(f"Error: {e}")

                # ── Step 4: FREE actions — pickup THEN equip (sequential: equip needs updated inventory) ──
                await pickup_all_valuable(
                    state, self.api, self.game_id, self.agent_id, self.logger, retries=1
                )

                # Check for smart swap based on best target
                target_action = select_target(state, self.intel, self.all_bot_ids)
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
                    if result.get("success"):
                        self.logger.equip(smart_swap.get("_name", "Weapon"))
                        # Refetch state so the main action uses the right weapon
                        state_resp = await self.api.get_state(
                            self.game_id, self.agent_id
                        )
                        if not is_api_error(state_resp):
                            state = GameState.from_api(state_resp)
                            self._last_state = state
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
                action = decide_action(
                    state, self.intel, self.all_bot_ids, self.target_lock
                )

                # Log lock status
                if self.target_lock.is_locked:
                    self.logger.decision(
                        f'LOCKED on "{self.target_lock.target_name}" '
                        f"(chase: {self.target_lock.chase_turns}/{self.target_lock.max_chase_turns})"
                    )

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

                # Log use_item attempt for clarity
                if clean_action.get("type") == "use_item":
                    item_id = clean_action.get("itemId", "")
                    item_name = "Unknown Item"
                    for i in state.inventory:
                        if i.id == item_id:
                            item_name = i.name
                            break
                    self.logger.info(f'Attempting to use: "{item_name}"...')

                result = await self.api.do_action(
                    self.game_id, self.agent_id, clean_action, thought, retries=2
                )

                if is_api_error(result):
                    msg, code = get_error_info(result)
                    self.logger.warn(f"Action failed: {msg} ({code})")
                    # Cooldown not expired? Short wait and retry
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

        if action_type == "attack":
            self.logger.attack(
                action.get("_name", action.get("targetId", "")),
                state.region_name,
                f"HP:{action.get('_hp', '?')}",
            )
        elif action_type == "move":
            to_name = action.get("_to_name", action.get("regionId", ""))
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
