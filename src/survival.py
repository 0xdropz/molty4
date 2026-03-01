"""
survival.py — HP/EP management, healing, resting, facility usage.
"""

from src.state_manager import GameState, ItemInfo
from src.config import (
    LOW_HP,
    CRITICAL_HP,
    MIN_EP_ATTACK,
    MIN_EP_ACTION,
    HEAL_PRIORITY,
    EP_DRINK_THRESHOLD,
)


def get_energy_drink_action(state: GameState) -> dict | None:
    """Use Energy Drink if EP is low. Returns use_item action or None."""
    # Never drink for EP when HP is critical — healing is more urgent
    if state.hp < CRITICAL_HP:
        return None
    if state.ep > EP_DRINK_THRESHOLD:
        return None

    # use_item costs 1 EP — can't drink with 0 EP
    if state.ep < 1:
        return None

    for item in state.inventory:
        if item.name == "Energy drink":
            return {
                "type": "use_item",
                "itemId": item.id,
                "_name": "Energy drink",
                "_reason": f"EP boost (EP:{state.ep}→{state.ep + 5})",
            }
    return None


def needs_healing(state: GameState) -> bool:
    """Check if bot needs healing (HP < LOW_HP)."""
    return state.hp < LOW_HP


def is_critical(state: GameState) -> bool:
    """Check if HP is critical (< CRITICAL_HP) — override everything."""
    return state.hp < CRITICAL_HP


def best_heal_item(state: GameState, force: bool = False) -> ItemInfo | None:
    """Find best recovery item in inventory based on missing HP to avoid waste."""
    recovery = state.recovery_items()
    if not recovery:
        return None

    missing_hp = 100 - state.hp

    # Filter items that are "allowed" based on missing HP
    # Medkit (+50) only if missing >= 30
    # Bandage (+30) only if missing >= 10
    # Otherwise, pick whatever we have
    allowed = []
    for item in recovery:
        name = item.name.lower()
        if "medkit" in name:
            if missing_hp >= 30:
                allowed.append(item)
        elif "bandage" in name:
            if missing_hp >= 10:
                allowed.append(item)
        else:  # Emergency Food (+20) or others
            allowed.append(item)

    # If our filters excluded everything (e.g. missing 5 HP and only have Medkit)
    # If not forced, we don't heal. If forced, we just use what we have
    if not allowed:
        if not force:
            return None
        allowed = recovery

    allowed.sort(key=lambda i: HEAL_PRIORITY.get(i.name, 0), reverse=True)
    return allowed[0]


def has_heal_option(state: GameState) -> bool:
    """Check if bot has ANY heal option (item or facility), regardless of EP."""
    if best_heal_item(state, force=True):
        return True
    if get_medical_facility(state):
        return True
    return False


def get_heal_action(state: GameState, force: bool = False) -> dict | None:
    """Return heal action if healing is needed, we have items, AND EP >= 1."""
    if not force and not needs_healing(state):
        return None

    # EP check — use_item and interact both cost 1 EP
    if state.ep < 1:
        return None

    item = best_heal_item(state, force)
    if not item:
        # Check for medical facility
        facility = get_medical_facility(state)
        if facility:
            return {
                "type": "interact",
                "interactableId": facility["id"],
                "_name": "Medical Facility",
                "_reason": "heal at facility",
            }
        return None

    return {
        "type": "use_item",
        "itemId": item.id,
        "_name": item.name,
        "_reason": "heal",
        "_hp_before": state.hp,
    }


def should_rest(state: GameState) -> bool:
    """Check if should rest (EP too low for any useful action)."""
    return state.ep < MIN_EP_ACTION


def should_rest_for_attack(state: GameState) -> bool:
    """Check if should rest specifically to save EP for attack."""
    return state.ep < MIN_EP_ATTACK


def get_rest_action() -> dict:
    return {"type": "rest", "_reason": "recover EP"}


def get_medical_facility(state: GameState) -> dict | None:
    """Find usable medical facility in current region."""
    for f in state.usable_facilities():
        if f.type in ("medical_facility", "medical"):
            return {"id": f.id, "type": f.type}
    return None


def get_supply_cache(state: GameState) -> dict | None:
    """Find usable supply cache in current region."""
    for f in state.usable_facilities():
        if f.type in ("supply_cache", "supply"):
            return {"id": f.id, "type": f.type}
    return None


def assess_situation(state: GameState) -> str:
    """Quick situation assessment for logging."""
    if is_critical(state):
        return "CRITICAL"
    if needs_healing(state):
        return "LOW_HP"
    if state.is_death_zone:
        return "DEATH_ZONE"
    if state.ep < MIN_EP_ATTACK:
        return "LOW_EP"
    return "OK"
