"""
config.py — Central configuration for Molty Royale v2 Bot.
All constants, thresholds, blacklists in one place.
"""

import re

# ─── Friendly Detection ──────────────────────────────
# Matches qq-series bots only. Prefix pendek (begal, godmode, dll) dihapus — sultan version.
# Rejects: anyone not in qq-series (treated as enemy / sultan target)
IS_FRIENDLY_REGEX = re.compile(
    r"^(godhunt|godsquad|begal|qqrxqqrxqqrxqqrxqqrxqqrxqqrxqq|qqtsqqtsqqtsqqtsqqtsqqtsqqtsqq|qqvtqqvtqqvtqqvtqqvtqqvtqqvtqq|qqxxqqxxqqxxqqxxqqxxqqxxqqxxqq|qqssqqssqqssqqssqqssqqssqqssqq)\d+$",
    re.IGNORECASE,
)

# ─── API ─────────────────────────────────────────────
BASE_URL = "https://cdn.moltyroyale.com/api"
CDN_URL = "https://cdn.moltyroyale.com/api"
TURN_INTERVAL = 61  # seconds between turns (cooldown)
STATE_POLL_INTERVAL = 5  # seconds between polling game status
API_TIMEOUT = 20  # HTTP request timeout seconds (lowered for faster fail)
API_RETRIES = 4  # max retry on failure (increased for laggy server)

# ─── Item Rules ──────────────────────────────────────
ITEM_BLACKLIST = {"Map", "Radio", "Megaphone"}  # never pickup
MAX_WEAPONS_IN_INVENTORY = 4  # Strict cap: 4 weapons
MAX_HEALS_IN_INVENTORY = 5  # Strict cap: 5 healing items
MAX_INVENTORY = 10

# ─── Weapon Priority (higher = better) ───────────────
# Names MUST match server API (GET /items) exactly
WEAPON_PRIORITY = {
    "Katana": 100,  # melee +21
    "Sniper rifle": 95,  # ranged +17 range:2
    "Sword": 80,  # melee +8
    "Pistol": 75,  # ranged +6 range:1
    "Dagger": 65,  # melee +5 (server id: "knife")
    "Bow": 50,  # ranged +3 range:1
    "Fist": 0,  # default
}

# ─── Weapon Types ─────────────────────────────────────
WEAPON_TYPE_MELEE = {"Fist", "Dagger", "Sword", "Katana"}
WEAPON_TYPE_RANGED = {"Bow", "Pistol", "Sniper rifle"}

# --- HP / EP Thresholds ---
LOW_HP = 60  # start healing (lower = more aggressive)
CRITICAL_HP = 40  # emergency heal, override everything
MIN_EP_ATTACK = 2  # need 2 EP to attack
MIN_EP_ACTION = 1  # need 1 EP for any group-1 action

# --- Combat ---
KILL_STEAL_HP = 50  # prioritize targets below this HP — also used as Vulture threshold
SULTAN_THRESHOLD = (
    30  # min Moltz in enemy inventory to be considered a sultan target (reserved)
)
KILLER_THRESHOLD = 2  # min kills to be considered a killer target (reserved)

# ─── Monster Priority (higher = fight first) ─────────
MONSTER_PRIORITY = {
    "Wolf": 3,  # easy kill (HP:5)
    "Bear": 2,  # medium (HP:15)
    "Bandit": 1,  # hard (HP:25)
}

# ─── Heal Item Priority (higher = use first) ─────────
HEAL_PRIORITY = {
    "Medkit": 100,  # +50 HP (Title case — server API name)
    "medkit": 100,  # +50 HP (lowercase alias)
    "Bandage": 80,  # +30 HP
    "bandage": 80,  # lowercase alias
    "Emergency rations": 60,  # +20 HP
    "emergency rations": 60,  # lowercase alias
}

# ─── EP Recovery ─────────────────────────────────────
EP_DRINK_THRESHOLD = 2  # Use Energy Drink when EP ≤ this

# ─── Terrain Preferences ─────────────────────────────
TERRAIN_EXPLORE_PRIORITY = {
    "ruins": 100,  # higher item find rate
    "plains": 60,  # good vision
    "hills": 50,  # best vision
    "forest": 40,  # stealth
    "water": 10,  # avoid
}

# ─── Log Colors (ANSI) ───────────────────────────────
BOT_COLORS = [
    "\033[96m",  # Cyan
    "\033[93m",  # Yellow
    "\033[92m",  # Green
    "\033[95m",  # Magenta
    "\033[91m",  # Red
]
RESET_COLOR = "\033[0m"
DIM_COLOR = "\033[2m"
BOLD_COLOR = "\033[1m"
