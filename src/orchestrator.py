"""
orchestrator.py — Multi-bot async launcher with graceful shutdown.
Joiner runs persistently alongside bots. Bots launch as soon as joiner assigns them a game.
After each game ends, bots signal joiner via rejoin_queue to get a new game.
"""

import asyncio
import signal
import json
import os

from src.bot import MoltyBot
from src.god_mode_cache import GodModeCache
from src.joiner import run_joiner


class Orchestrator:
    """Launch and manage multiple bots concurrently."""

    def __init__(self, accounts_file: str = "accounts.json", bot_index=None):
        self.accounts = self._load_accounts(accounts_file)
        self.bot_index = bot_index  # None = all bots
        self.bots: list[MoltyBot] = []
        self.shared_bot_ids: set = set()
        self.god_cache = GodModeCache(ttl=30.0)
        self._tasks: list[asyncio.Task] = []

        # Shared queue: bots push their account here when they need a new game
        self._rejoin_queue: asyncio.Queue = asyncio.Queue()

        # Map account name → MoltyBot for joiner callback
        self._bot_map: dict[str, MoltyBot] = {}

    def _load_accounts(self, path: str) -> list[dict]:
        abs_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), path)
        if not os.path.exists(abs_path):
            abs_path = path
        with open(abs_path, "r") as f:
            return json.load(f)

    async def run(self):
        """Run persistent joiner + bots concurrently. Bots launch as joiner assigns them."""
        if self.bot_index is not None:
            if isinstance(self.bot_index, int):
                if 0 <= self.bot_index < len(self.accounts):
                    selected = [(self.bot_index, self.accounts[self.bot_index])]
                else:
                    print(
                        f"ERROR: Bot index {self.bot_index} out of range (0-{len(self.accounts) - 1})"
                    )
                    return
            else:
                selected = []
                for i, acc in enumerate(self.accounts):
                    if acc["name"].lower() == str(self.bot_index).lower():
                        selected = [(i, acc)]
                        break
                if not selected:
                    print(f"ERROR: Bot '{self.bot_index}' not found")
                    return
        else:
            selected = list(enumerate(self.accounts))

        all_keys = [acc["apiKey"] for acc in self.accounts]

        print(f"{'=' * 60}")
        print(f"  MOLTY ROYALE v2 BOT -- Launching {len(selected)} bot(s)")
        print(f"  Accounts: {', '.join(acc['name'] for _, acc in selected)}")
        print(f"{'=' * 60}")
        print()

        self.shared_bot_ids = {acc["name"] for acc in self.accounts}

        # Pre-create all bots — no game_id/agent_id yet, joiner will assign
        for idx, account in selected:
            bot = MoltyBot(
                account=account,
                bot_index=idx,
                all_api_keys=all_keys,
                all_bot_ids=self.shared_bot_ids,
                god_cache=self.god_cache,
                rejoin_queue=self._rejoin_queue,
            )
            self.bots.append(bot)
            self._bot_map[account["name"]] = bot

        # Setup signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_signal)
            except NotImplementedError:
                pass  # Windows

        # ── Launch persistent joiner as background task ──
        # Joiner never stops — it handles initial join + all reincarnations
        joiner_task = asyncio.create_task(
            run_joiner(
                [acc for _, acc in selected],
                self._on_bot_ready,
                self._rejoin_queue,
            ),
            name="joiner",
        )
        self._tasks.append(joiner_task)

        # Bot tasks are created dynamically in _on_bot_ready as joiner assigns games
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            await self.god_cache.close()

        print()
        print(f"{'=' * 60}")
        print(f"  All bots exited.")
        print(f"{'=' * 60}")

    async def _on_bot_ready(self, account: dict, game_id: str, agent_id: str):
        """
        Called by joiner when an account is assigned to a game.
        Injects game_id/agent_id into bot and fires _ready_event.
        If bot task not yet running, creates it now.
        """
        name = account["name"]
        bot = self._bot_map.get(name)
        if bot is None:
            return

        # Inject game assignment
        bot.game_id = game_id
        bot.agent_id = agent_id
        bot._ready_event.set()

        # Launch bot task if not already running
        existing = next((t for t in self._tasks if t.get_name() == f"bot_{name}"), None)
        if existing is None or existing.done():
            task = asyncio.create_task(bot.run(), name=f"bot_{name}")
            self._tasks.append(task)

    def _handle_signal(self):
        """Handle Ctrl+C / SIGTERM."""
        print("\n\nShutdown signal received...")
        for bot in self.bots:
            asyncio.create_task(bot.stop())
        loop = asyncio.get_event_loop()
        loop.create_task(self._force_cancel_after_delay())

    async def _force_cancel_after_delay(self):
        """Force cancel all tasks after 3 seconds if they haven't exited cleanly."""
        await asyncio.sleep(3.0)
        for task in self._tasks:
            if not task.done():
                task.cancel()
