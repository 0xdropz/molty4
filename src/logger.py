"""
logger.py — Per-bot colored console + file logging.
Human-readable names, stats bar, action details.

Format: [BotName] [HP:xx EP:xx $:xx] [ACTION] detail
"""

import os
import sys
import logging
from datetime import datetime
from src.config import BOT_COLORS, RESET_COLOR, DIM_COLOR, BOLD_COLOR


class BotLogger:
    """Logger for a single bot with colored console + file output."""

    def __init__(self, bot_name: str, bot_index: int = 0, log_dir: str = "logs"):
        self.bot_name = bot_name
        self.color = BOT_COLORS[bot_index % len(BOT_COLORS)]
        self.log_dir = log_dir

        # File logger disabled to save disk I/O and CPU resources for large scale bots
        self._file_logger = None

        # Current stats for the stats bar
        self._hp = 0
        self._ep = 0
        self._moltz = 0
        self._bag = 0

    def update_stats(self, hp: int, ep: int, moltz: int, bag_count: int):
        """Update stats bar values from game state."""
        self._hp = hp
        self._ep = ep
        self._moltz = moltz
        self._bag = bag_count

    def _stats_bar(self) -> str:
        """Format: [HP:100 EP:10 $:50]"""
        hp = int(round(self._hp)) if isinstance(self._hp, float) else self._hp
        ep = int(round(self._ep)) if isinstance(self._ep, float) else self._ep
        return f"[HP:{hp:<3} EP:{ep:<2} $:{self._moltz:<4}]"

    def _format(self, tag: str, message: str) -> str:
        """Format a log line with bot name, stats, tag, and message."""
        return f"[{self.bot_name}] {self._stats_bar()} [{tag}] {message}"

    def _print(self, tag: str, message: str, tag_color: str = ""):
        """Print to console with color and log to file."""
        line = self._format(tag, message)

        # Console with color
        colored_tag = f"{tag_color}{tag}{RESET_COLOR}" if tag_color else tag
        console_line = (
            f"{self.color}[{self.bot_name}]{RESET_COLOR} "
            f"{DIM_COLOR}{self._stats_bar()}{RESET_COLOR} "
            f"[{colored_tag}] {message}"
        )

        # Use sys.stdout directly for Unicode support
        try:
            sys.stdout.write(console_line + "\n")
            sys.stdout.flush()
        except UnicodeEncodeError:
            # Fallback: strip emoji for terminals that can't handle it
            safe_line = line.encode("ascii", errors="replace").decode("ascii")
            print(safe_line)

        # File log (no color codes) - Disabled
        if self._file_logger:
            self._file_logger.info(line)

    # ─── Action-specific log methods ─────────────────

    def attack(self, target_name: str, region_name: str, damage: str = ""):
        detail = f" → {damage}" if damage else ""
        self._print(
            "ATTACK", f'Hit "{target_name}" at "{region_name}"{detail}', "\033[91m"
        )

    def pickup(self, item_name: str, region_name: str):
        self._print("PICKUP", f'Got "{item_name}" at "{region_name}"', "\033[92m")

    def equip(self, weapon_name: str):
        self._print("EQUIP", f'Equipped "{weapon_name}"', "\033[93m")

    def heal(self, item_name: str, hp_before: int, hp_after: int):
        self._print(
            "HEAL", f'Used "{item_name}" → HP {hp_before}→{hp_after}', "\033[92m"
        )

    def move(self, from_region: str, to_region: str, reason: str = ""):
        detail = f" ({reason})" if reason else ""
        self._print("MOVE", f'"{from_region}" → "{to_region}"{detail}', "\033[94m")

    def explore(self, region_name: str):
        self._print("EXPLORE", f'Searching "{region_name}"', "\033[96m")

    def rest(self):
        self._print("REST", "Resting to recover EP", "\033[2m")

    def interact(self, facility_name: str, region_name: str):
        self._print(
            "INTERACT", f'Used "{facility_name}" at "{region_name}"', "\033[95m"
        )

    def flee(self, from_region: str, to_region: str):
        self._print(
            "FLEE", f'Escaping death zone "{from_region}" → "{to_region}"', "\033[91m"
        )

    def kill(self, target_name: str, region_name: str):
        self._print("KILL", f'KILLED "{target_name}" at "{region_name}"', "\033[91m")

    def death(self, killer_name: str = ""):
        killer = f' by "{killer_name}"' if killer_name else ""
        self._print("DEATH", f"Bot died{killer}", "\033[91m")

    def godmode(self, message: str):
        self._print("GODMODE", message, "\033[35m")

    def decision(self, message: str):
        self._print("DECIDE", message, "\033[33m")

    def info(self, message: str):
        self._print("INFO", message, "")

    def warn(self, message: str):
        self._print("WARN", message, "\033[93m")

    def error(self, message: str):
        self._print("ERROR", message, "\033[91m")

    def debug(self, message: str):
        """Debug log (dimmed)."""
        self._print("DEBUG", message, "\033[2m")

    def state_summary(
        self,
        region_name: str,
        terrain: str,
        weather: str,
        weapon_name: str,
        enemies: int,
        monsters: int,
        items: int,
        kills: int,
        is_death_zone: bool,
    ):
        """Log a turn state summary."""
        dz = " DZ!" if is_death_zone else ""
        self._print(
            "STATE",
            f'@"{region_name}" ({terrain}/{weather}{dz}) '
            f'W:"{weapon_name}" K:{kills} '
            f"E:{enemies} M:{monsters} I:{items}",
            "\033[36m",
        )

    def shutdown(self):
        self._print("SHUTDOWN", "Exiting gracefully...", "\033[2m")

    def startup(self, game_id: str, agent_id: str):
        self._print(
            "START", f'Joined game "{game_id}" as agent "{agent_id}"', "\033[92m"
        )
