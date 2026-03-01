"""
Molty Royale v2 Bot — Entry Point

Usage:
  python run.py                   # run all 5 bots
  python run.py --bot 0           # run single bot by index
  python run.py --bot KangBegal   # run single bot by name
"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from src.orchestrator import Orchestrator


def main():
    # Parse --bot argument
    bot_index = None
    args = sys.argv[1:]

    i = 0
    while i < len(args):
        if args[i] == "--bot" and i + 1 < len(args):
            val = args[i + 1]
            try:
                bot_index = int(val)
            except ValueError:
                bot_index = val  # name string
            i += 2
        else:
            i += 1

    orchestrator = Orchestrator(
        accounts_file="accounts.json",
        bot_index=bot_index,
    )

    # Windows-compatible async run
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        print("\n🛑 Interrupted. Shutting down...")


if __name__ == "__main__":
    main()
