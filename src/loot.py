"""
loot.py — Item pickup, equip, blacklist filtering, max 3 weapons rule.
"""

from src.api_client import ApiClient
from src.combat import select_target
from src.state_manager import GameState, ItemInfo, RegionItem
from src.logger import BotLogger
from src.config import (
    ITEM_BLACKLIST,
    MAX_WEAPONS_IN_INVENTORY,
    MAX_HEALS_IN_INVENTORY,
    WEAPON_PRIORITY,
    MAX_INVENTORY,
    WEAPON_TYPE_RANGED,
    WEAPON_TYPE_MELEE,
    LOOT_TIER_S,
)


def should_pickup(item: ItemInfo, state: GameState, logger: BotLogger = None) -> bool:
    """
    Decide if we should pickup this item.
    Strict limits: 4 Weapons, 1 slot for Moltz (stackable), 5 Heal items.
    Rejects all utility items (radios, maps, binoculars).
    """
    # 1. Always pickup Moltz / currency (stacks infinitely, takes 1 physical slot max)
    if item.category == "currency" or item.type_id == "rewards" or "Moltz" in item.name:
        return True

    # Validation: Inventory full (non-stackable items)
    if state.bag_count >= MAX_INVENTORY:
        return False

    # 2. Weapon Logic
    if item.category == "weapon":
        current_weapons = state.weapons_in_inventory()
        new_priority = WEAPON_PRIORITY.get(item.name, 0)

        # Count types
        has_melee = any(w.name in WEAPON_TYPE_MELEE for w in current_weapons)
        has_ranged = any(w.name in WEAPON_TYPE_RANGED for w in current_weapons)

        is_melee = item.name in WEAPON_TYPE_MELEE
        is_ranged = item.name in WEAPON_TYPE_RANGED

        # Always pick up S-tier upgrades if we have ANY space at all
        best_existing_overall = max(
            (WEAPON_PRIORITY.get(w.name, 0) for w in current_weapons), default=0
        )
        if (
            new_priority > best_existing_overall and new_priority >= 80
        ):  # Sniper or Katana
            return True

        if len(current_weapons) >= MAX_WEAPONS_IN_INVENTORY:
            # If we hit our soft cap, ONLY pick up if it's strictly better than the BEST we have
            # of the same type.
            same_type = [
                w
                for w in current_weapons
                if (is_melee and w.name in WEAPON_TYPE_MELEE)
                or (is_ranged and w.name in WEAPON_TYPE_RANGED)
            ]
            if same_type:
                best_existing = max(WEAPON_PRIORITY.get(w.name, 0) for w in same_type)
                if new_priority > best_existing:
                    return True
            return False

        # 1. Fill empty type slots first (ensure minimum 1 melee, 1 ranged)
        if is_melee and not has_melee:
            return True
        if is_ranged and not has_ranged:
            return True

        if not is_melee and not is_ranged:
            if logger:
                logger.warn(f"UNKNOWN WEAPON FOUND: {item.name}. Add to config!")
            return False

        # 2. Upgrade existing or take backup slots
        same_type = [
            w
            for w in current_weapons
            if (is_melee and w.name in WEAPON_TYPE_MELEE)
            or (is_ranged and w.name in WEAPON_TYPE_RANGED)
        ]
        if same_type:
            best_existing = max(WEAPON_PRIORITY.get(w.name, 0) for w in same_type)
            new_priority = WEAPON_PRIORITY.get(item.name, 0)

            # Strict upgrade: only pick if strictly better
            if new_priority > best_existing:
                return True

            # If we have space (e.g. going for 3rd or 4th slot), maybe take backup?
            if (
                len(current_weapons) < MAX_WEAPONS_IN_INVENTORY
                and new_priority >= best_existing
            ):
                return True

        # 3. Wildcard Slot (empty slots available)
        if len(current_weapons) < MAX_WEAPONS_IN_INVENTORY:
            if WEAPON_PRIORITY.get(item.name, 0) >= 50:  # Bow or better
                return True

        return False

    # 3. Heal/Recovery Logic (Strict cap: 5)
    if item.category == "recovery":
        current_heals = [
            i for i in state.self_info.inventory if i.category == "recovery"
        ]
        if len(current_heals) >= MAX_HEALS_IN_INVENTORY:
            return False
        return True

    # 4. Outright REJECT everything else (Utility, Map, Radio, Binoculars)
    return False


def get_best_weapon_in_inventory(state: GameState) -> ItemInfo | None:
    """Find the best weapon in inventory by priority."""
    weapons = state.weapons_in_inventory()
    if not weapons:
        return None
    weapons.sort(key=lambda w: WEAPON_PRIORITY.get(w.name, 0), reverse=True)
    return weapons[0]


def should_equip(state: GameState) -> ItemInfo | None:
    """Check if we should equip a different weapon. Returns item to equip or None."""
    best = get_best_weapon_in_inventory(state)
    if not best:
        return None

    current = state.weapon
    current_priority = WEAPON_PRIORITY.get(current.name, 0)
    best_priority = WEAPON_PRIORITY.get(best.name, 0)

    if best_priority > current_priority:
        return best
    return None


def get_pickup_priority(item: ItemInfo) -> int:
    """Priority score for picking up an item. Higher = pick first."""
    # Currency (Moltz) = highest priority
    if item.type_id == "rewards" or item.category == "currency":
        return 1000

    # Weapons by tier
    if item.category == "weapon":
        return WEAPON_PRIORITY.get(item.name, 0)

    # Recovery items
    if item.category == "recovery":
        recovery_values = {
            "medkit": 200,
            "Bandage": 150,
            "Energy drink": 180,
            "Emergency rations": 100,
        }
        return recovery_values.get(item.name, 50)

    # Utility
    if item.category == "utility":
        if item.name == "Binoculars":
            return 160
        return 10  # low priority for others

    return 0


async def pickup_all_valuable(
    state: GameState,
    api: ApiClient,
    game_id: str,
    agent_id: str,
    logger: BotLogger,
    retries: int = 1,
) -> int:
    """
    Pickup all valuable items in current region. FREE action.
    Returns count of items picked up.
    """
    region_items = state.items_in_region()
    if not region_items:
        return 0

    # Sort by priority (highest first)
    # Sort by priority (highest first)
    region_items.sort(key=lambda ri: get_pickup_priority(ri.item), reverse=True)

    picked = 0
    for ri in region_items:
        try:
            if not should_pickup(ri.item, state, logger):
                continue

            if logger:
                logger.info(f'Attempting pickup: "{ri.item.name}"...')

            result = await api.do_action(
                game_id,
                agent_id,
                {"type": "pickup", "itemId": ri.item.id},
                retries=retries,
            )

            if result.get("success"):
                region_name = state.region_name
                logger.pickup(ri.item.name, region_name)
                picked += 1
                # Update local state (simulate pickup)
                state.self_info.inventory.append(ri.item)
            else:
                error = result.get("error", {})
                msg = error.get("message", "Unknown error")
                code = error.get("code", "UNKNOWN")
                if logger:
                    logger.warn(f'Failed to pickup "{ri.item.name}": {msg} ({code})')
        except Exception as e:
            if logger:
                logger.error(f"Error picking up {ri.item.name}: {e}")

    return picked


async def equip_best(
    state: GameState,
    api: ApiClient,
    game_id: str,
    agent_id: str,
    logger: BotLogger,
    retries: int = 1,
) -> bool:
    """Equip best weapon if upgrade available. FREE action. Returns True if equipped."""
    new_weapon = should_equip(state)
    if not new_weapon:
        return False

    result = await api.do_action(
        game_id, agent_id, {"type": "equip", "itemId": new_weapon.id}, retries=retries
    )

    if result.get("success"):
        logger.equip(new_weapon.name)
        # Update local state
        state.self_info.weapon = type(state.self_info.weapon)(
            id=new_weapon.id,
            name=new_weapon.name,
            atk_bonus=new_weapon.atk_bonus,
            range=new_weapon.range,
        )
        return True
    return False
