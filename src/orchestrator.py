"""
orchestrator.py — Multi-bot async launcher with graceful shutdown.
Staggered registration, shared state, reincarnation.
"""

import asyncio
import signal
import json
import os
import traceback
import sys

from src.bot import MoltyBot
from src.logger import BotLogger
from src.god_mode_cache import GodModeCache


class Orchestrator:
    """Launch and manage multiple bots concurrently."""

    def __init__(self, accounts_file: str = "accounts.json", bot_index=None):
        self.accounts = self._load_accounts(accounts_file)
        self.bot_index = bot_index  # None = all bots
        self.bots: list[MoltyBot] = []
        self.shared_bot_ids: set = set()
        self.god_cache = GodModeCache(ttl=30.0)  # shared across all bots
        self._tasks: list[asyncio.Task] = []

    def _load_accounts(self, path: str) -> list[dict]:
        """Load accounts from JSON file."""
        abs_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), path)
        if not os.path.exists(abs_path):
            abs_path = path

        with open(abs_path, "r") as f:
            return json.load(f)

    async def run(self):
        """Run bots with staggered registration and graceful shutdown."""
        # Determine which accounts to use
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

        # Build friendly bot names set (prevent friendly fire)
        self.shared_bot_ids = {acc["name"] for acc in self.accounts}

        # Create bots
        for idx, account in selected:
            bot = MoltyBot(
                account=account,
                bot_index=idx,
                all_api_keys=all_keys,
                all_bot_ids=self.shared_bot_ids,
                god_cache=self.god_cache,
            )
            self.bots.append(bot)

        # Setup signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_signal)
            except NotImplementedError:
                pass  # Windows

        # Launch all bots immediately — no stagger delay
        self._tasks = []
        for i, bot in enumerate(self.bots):
            task = asyncio.create_task(bot.run(), name=f"bot_{bot.name}")
            self._tasks.append(task)

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

    def _handle_signal(self):
        """Handle Ctrl+C / SIGTERM."""
        print("\n\nShutdown signal received...")

        # 1. Tell all bots to stop their internal loops
        for bot in self.bots:
            asyncio.create_task(bot.stop())

        # 2. Give the loops a moment to finish gracefully and run finally blocks
        # We don't instantly cancel tasks, let them exit naturally first
        loop = asyncio.get_event_loop()
        loop.create_task(self._force_cancel_after_delay())

    async def _force_cancel_after_delay(self):
        """If bots don't exit cleanly within 3 seconds, forcefully cancel them."""
        await asyncio.sleep(3.0)
        for task in self._tasks:
            if not task.done():
                task.cancel()
