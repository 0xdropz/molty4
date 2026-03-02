"""
combat.py — Aggressive damage calculation, target selection, kill-steal logic.
Rank = Kills > HP, so prioritize getting kills over surviving.
"""

from src.state_manager import GameState, EnemyInfo, MonsterInfo, WeaponInfo
from src.god_mode import GodModeIntel
from src.config import (
    KILL_STEAL_HP,
    MONSTER_PRIORITY,
    IS_FRIENDLY_REGEX,
    WEAPON_TYPE_MELEE,
    WEAPON_TYPE_RANGED,
    WEAPON_PRIORITY,
)


def calc_damage(atk: int, weapon_bonus: int, target_def: int) -> float:
    """Calculate expected damage: ATK + weapon_bonus - (DEF * 0.5)"""
    return max(1, atk + weapon_bonus - (target_def * 0.5))


def can_kill_in_one_hit(state: GameState, target_hp: float, target_def: int) -> bool:
    """Check if we can kill the target in one attack."""
    si = state.self_info
    if not si:
        return False
    dmg = calc_damage(si.atk, state.weapon.atk_bonus, target_def)
    return dmg >= target_hp


def estimate_hits_to_kill(state: GameState, target_hp: float, target_def: int) -> int:
    """How many hits needed to kill target."""
    si = state.self_info
    if not si:
        return 999
    dmg = calc_damage(si.atk, state.weapon.atk_bonus, target_def)
    if dmg <= 0:
        return 999
    import math

    return math.ceil(target_hp / dmg)


def select_target(
    state: GameState,
    intel: GodModeIntel = None,
    my_bot_ids: set = None,
    pending_dz_ids: set = None,
    is_purge_time: bool = False,
    priority_target_id: str = None,
) -> dict | None:
    """
    AGGRESSIVE target selection. Returns action dict or None.
    Priority:
    0. KILLER TARGET (Absolute Priority)
    1. One-hit kills (guaranteed kills — always take these)
    2. Kill-steal: enemies HP < KILL_STEAL_HP (lowest HP first)
    3. Enemies in same region (lowest HP first)
    4. Monsters in same region (Wolf > Bear > Bandit)
    5. Ranged: enemies in adjacent regions (if weapon has range)
    """
    si = state.self_info
    if not si:
        return None

    weapon = state.weapon
    my_region = state.region_id
    friendly = my_bot_ids or set()

    forbidden_zones = intel.dz_region_ids.copy() if intel and intel.available else set()
    if pending_dz_ids:
        forbidden_zones.update(pending_dz_ids)
    if state.is_death_zone:
        forbidden_zones.add(state.region_id)

    # Collect potential targets
    # If range >= 2, we can target enemies visible only in God Mode
    potential_enemies = []

    # 1. Visible enemies
    for e in state.visible_enemies:
        if not e.is_alive or e.id == si.id:
            continue

        is_friendly = bool(IS_FRIENDLY_REGEX.match(e.name))
        is_own_bot = e.id in friendly

        if is_purge_time:
            is_friendly = False
            is_own_bot = False  # Betrayal! Attack own bots too!

        if not is_own_bot and not is_friendly:
            if e.region_id not in forbidden_zones or e.id == priority_target_id:
                potential_enemies.append(e)

    # 2. God Mode enemies (for Bow/Pistol/Sniper - Range >= 1)
    if weapon.range >= 1 and intel and intel.available:
        exclude_for_gm = {si.id} if is_purge_time else (friendly | {si.id})
        gm_enemies = intel.find_all_enemies(
            exclude_ids=exclude_for_gm, is_purge_time=is_purge_time
        )
        # Convert dict to simple object or use dict directly
        # Let's verify we don't duplicate visible ones
        visible_ids = {e.id for e in potential_enemies}
        for gem in gm_enemies:
            if gem["id"] not in visible_ids and (
                gem["region_id"] not in forbidden_zones
                or gem["id"] == priority_target_id
            ):
                # Create extensive EnemyInfo from GM data
                # (We don't know exact stats like def/atk, assume defaults)
                e = EnemyInfo(
                    id=gem["id"],
                    name=gem["name"],
                    hp=gem["hp"],
                    max_hp=100,
                    atk=10,
                    defense=5,  # assumptions
                    region_id=gem["region_id"],
                    is_alive=True,
                )
                potential_enemies.append(e)

    # Filter by range and calc damage
    all_targets = []
    for e in potential_enemies:
        dist = _get_distance(state.region_id, e.region_id, state, intel)

        if dist > weapon.range:
            continue

        dmg = calc_damage(si.atk, weapon.atk_bonus, e.defense)
        one_hit = dmg >= e.hp

        all_targets.append(
            {
                "enemy": e,
                "dmg": dmg,
                "one_hit": one_hit,
                "dist": dist,
                "same_region": dist == 0,
            }
        )

    # 0. KILLER PRIORITY — Drop everything and kill the Killer!
    if priority_target_id:
        killer_targets = [t for t in all_targets if t["enemy"].id == priority_target_id]
        if killer_targets:
            best = killer_targets[0]
            return {
                "type": "attack",
                "targetId": best["enemy"].id,
                "targetType": "agent",
                "_name": best["enemy"].name,
                "_hp": best["enemy"].hp,
                "_reason": f"KILLER TARGET (Must Kill!)",
                "_dist": best["dist"],
            }

    # 1. GUARANTEED KILLS — always take these, sorted by lowest HP
    guaranteed = [t for t in all_targets if t["one_hit"]]
    guaranteed.sort(key=lambda t: t["enemy"].hp)

    # 2. KILL-STEAL — low HP enemies (< KILL_STEAL_HP)
    kill_steal = [t for t in all_targets if t["enemy"].hp < KILL_STEAL_HP]
    kill_steal.sort(key=lambda t: t["enemy"].hp)

    # 3. SAME REGION enemies — lowest HP first
    region_enemies = [t for t in all_targets if t["same_region"]]
    region_enemies.sort(key=lambda t: t["enemy"].hp)

    action = None
    target_dist = 0
    if guaranteed:
        best = guaranteed[0]
        target_dist = best["dist"]
        action = {
            "type": "attack",
            "targetId": best["enemy"].id,
            "targetType": "agent",
            "_name": best["enemy"].name,
            "_hp": best["enemy"].hp,
            "_reason": f"guaranteed kill (1-hit, dmg={best['dmg']:.0f})",
        }

    elif kill_steal:
        best = kill_steal[0]
        target_dist = best["dist"]
        action = {
            "type": "attack",
            "targetId": best["enemy"].id,
            "targetType": "agent",
            "_name": best["enemy"].name,
            "_hp": best["enemy"].hp,
            "_reason": f"kill-steal (HP:{best['enemy'].hp:.0f}, dmg={best['dmg']:.0f})",
        }

    elif region_enemies:
        best = region_enemies[0]
        target_dist = best["dist"]
        action = {
            "type": "attack",
            "targetId": best["enemy"].id,
            "targetType": "agent",
            "_name": best["enemy"].name,
            "_hp": best["enemy"].hp,
            "_reason": f"attack enemy (HP:{best['enemy'].hp:.0f})",
        }

    else:
        # 4. MONSTERS — easy kills for loot
        region_monsters = state.monsters_in_region()
        region_monsters = [m for m in region_monsters if m.name != "Bandit"]
        region_monsters.sort(key=lambda m: -MONSTER_PRIORITY.get(m.name, 0))
        if region_monsters:
            best_monster = region_monsters[0]
            target_dist = 0
            action = {
                "type": "attack",
                "targetId": best_monster.id,
                "targetType": "monster",
                "_name": best_monster.name,
                "_hp": best_monster.hp,
                "_reason": f"hunt {best_monster.name}",
            }

        # 5. RANGED — enemies in adjacent regions
        else:
            # 5. RANGED / SNIPER — enemies in distance 1 or 2
            ranged = [t for t in all_targets if t["dist"] > 0]
            ranged.sort(
                key=lambda t: (t["dist"], t["enemy"].hp)
            )  # closer first, then weaker
            if ranged:
                best = ranged[0]
                target_dist = best["dist"]
                action = {
                    "type": "attack",
                    "targetId": best["enemy"].id,
                    "targetType": "agent",
                    "_name": best["enemy"].name,
                    "_hp": best["enemy"].hp,
                    "_reason": f"ranged attack (dist={target_dist}, HP:{best['enemy'].hp:.0f})",
                }

    if action:
        action["_dist"] = target_dist

    # SMART SWAP LOGIC IS MOVED TO bot.py SO IT IS NOT A MAIN ACTION
    return action


def get_smart_swap_action(state: GameState, target_dist: int) -> dict | None:
    """Returns an equip action if we should swap weapon for the given target distance, else None."""
    weapon = state.weapon
    best_melee = None
    best_ranged = None
    for inv_weapon in state.weapons_in_inventory():
        if inv_weapon.name in WEAPON_TYPE_MELEE:
            if not best_melee or WEAPON_PRIORITY.get(
                inv_weapon.name, 0
            ) > WEAPON_PRIORITY.get(best_melee.name, 0):
                best_melee = inv_weapon
        elif inv_weapon.name in WEAPON_TYPE_RANGED:
            if not best_ranged or WEAPON_PRIORITY.get(
                inv_weapon.name, 0
            ) > WEAPON_PRIORITY.get(best_ranged.name, 0):
                best_ranged = inv_weapon

    current_is_ranged = weapon.name in WEAPON_TYPE_RANGED
    current_is_melee = weapon.name in WEAPON_TYPE_MELEE

    if target_dist == 0:
        if current_is_ranged and best_melee:
            return {
                "type": "equip",
                "itemId": best_melee.id,
                "_name": best_melee.name,
                "_reason": f"smart swap to melee for CQC (dist 0)",
            }
    else:  # target_dist > 0
        if current_is_melee and best_ranged and best_ranged.range >= target_dist:
            return {
                "type": "equip",
                "itemId": best_ranged.id,
                "_name": best_ranged.name,
                "_reason": f"smart swap to ranged (dist {target_dist})",
            }

    return None


def _get_distance(
    my_region: str, target_region: str, state: GameState, intel: GodModeIntel = None
) -> int:
    """Calculate hop distance. 0=same, 1=adjacent, 2+=far."""
    if my_region == target_region:
        return 0

    # Check immediate connections (distance 1)
    for r in state.connected_regions:
        if r.id == target_region:
            return 1

    # Check god mode for distance > 1
    if intel and intel.available:
        return intel.calculate_distance(my_region, target_region)

    return 999  # unknown distance
