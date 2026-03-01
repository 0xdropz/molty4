"""
strategy.py — Aggressive decision engine with LOCKED TARGET system.
RANK = KILLS > HP. Once a target is locked, bot focuses exclusively on killing it.
"""

from src.state_manager import GameState, EnemyInfo
from src.god_mode import GodModeIntel
from src.combat import select_target, can_kill_in_one_hit
from src.survival import (
    is_critical,
    needs_healing,
    get_heal_action,
    has_heal_option,
    should_rest,
    get_rest_action,
    get_supply_cache,
    get_energy_drink_action,
)
from src.movement import must_flee_death_zone, choose_explore_move, move_toward_target
from src.config import (
    MIN_EP_ATTACK,
    CRITICAL_HP,
    LOW_HP,
    MONSTER_PRIORITY,
    IS_FRIENDLY_REGEX,
)


class TargetLock:
    """Persistent locked target state across turns."""

    def __init__(self):
        self.target_id: str = ""
        self.target_name: str = ""
        self.last_region_id: str = ""  # last known region
        self.chase_turns: int = 0  # how many turns we've been chasing
        self.max_chase_turns: int = 8  # give up after this many turns

    @property
    def is_locked(self) -> bool:
        return bool(self.target_id)

    def lock(self, target_id: str, target_name: str, region_id: str = ""):
        self.target_id = target_id
        self.target_name = target_name
        self.last_region_id = region_id
        self.chase_turns = 0

    def unlock(self):
        self.target_id = ""
        self.target_name = ""
        self.last_region_id = ""
        self.chase_turns = 0

    def increment_chase(self):
        self.chase_turns += 1

    def is_chase_expired(self) -> bool:
        return self.chase_turns >= self.max_chase_turns


def _find_target_in_visible(state: GameState, target_id: str) -> EnemyInfo | None:
    """Find a specific target in visible enemies."""
    for e in state.visible_enemies:
        if e.id == target_id:
            return e
    return None


def _find_target_in_godmode(intel: GodModeIntel, target_id: str) -> dict | None:
    """Find target position via god mode."""
    if not intel or not intel.available:
        return None
    for agent in intel.all_agents:
        if agent.get("id") == target_id:
            if agent.get("isAlive", False):
                return {
                    "id": agent["id"],
                    "name": agent.get("name", ""),
                    "hp": agent.get("hp", 0),
                    "region_id": agent.get("regionId", ""),
                    "region_name": intel.get_region_name(agent.get("regionId", "")),
                }
    return None


def _is_in_attack_range(
    target_region: str,
    my_region: str,
    weapon_range: int,
    state: GameState,
    intel: GodModeIntel = None,
) -> bool:
    """Check if target region is within weapon attack range."""
    if target_region == my_region:
        return True

    # Range 1 check
    if weapon_range >= 1:
        for r in state.connected_regions:
            if r.id == target_region:
                return True

    # Range 2+ check via God Mode
    if weapon_range >= 2 and intel and intel.available:
        dist = intel.calculate_distance(my_region, target_region)
        return dist <= weapon_range

    return False


def _pick_explore_target(state: GameState) -> dict | None:
    """Terrain-based random move (no god mode — that's handled separately)."""
    import random
    from src.config import TERRAIN_EXPLORE_PRIORITY
    from src.movement import _readable_name

    # Avoid pending death zones
    pending_ids = {dz.get("id", "") for dz in state.pending_deathzones}

    candidates = state.safe_connections()
    candidates = [r for r in candidates if r.id not in pending_ids]

    if not candidates:
        candidates = state.safe_connections()

    if not candidates:
        return None

    # Sort by terrain priority, randomize within top tier
    candidates.sort(
        key=lambda r: TERRAIN_EXPLORE_PRIORITY.get(r.terrain, 0), reverse=True
    )
    top_priority = TERRAIN_EXPLORE_PRIORITY.get(candidates[0].terrain, 0)
    top_tier = [
        r
        for r in candidates
        if TERRAIN_EXPLORE_PRIORITY.get(r.terrain, 0) >= top_priority - 1
    ]
    target = random.choice(top_tier)

    return {
        "type": "move",
        "regionId": target.id,
        "_to_name": _readable_name(target),
        "_reason": f"explore {target.terrain}",
    }


def decide_action(
    state: GameState,
    intel: GodModeIntel = None,
    my_bot_ids: set = None,
    target_lock: TargetLock = None,
) -> dict:
    """
    AGGRESSIVE decision engine — per-turn evaluation, no permanent lock.

    Armed: Attack anything in range every turn. No tunnel vision.
    Unarmed: Defensive — find weapon first, only take guaranteed kills.
    """

    needs_weapon = state.weapon.name == "Fist"
    pending_ids = {dz.get("id", "") for dz in state.pending_deathzones}
    in_danger_zone = state.is_death_zone or (state.region_id in pending_ids)

    # ──── P0: Flee death zone (non-negotiable) ────
    flee = must_flee_death_zone(state, intel)
    if flee:
        return flee

    # ──── P0.5: PENDING DZ EVACUATE (leave BEFORE it burns) ────
    # If current region will become DZ next turn, move preemptively
    if state.region_id in pending_ids and state.ep >= 1:
        flee = choose_explore_move(state, intel, my_bot_ids)
        if flee:
            flee["_reason"] = "pending DZ evacuation (leaving before it burns)"
            return flee

    # ──── P1: CRITICAL HP (< 40) — override everything ────
    if is_critical(state):
        heal = get_heal_action(state)  # EP-aware: returns None if EP < 1
        if heal:
            return heal
        # EP = 0: rest to get EP for heal next turn
        if has_heal_option(state):
            return {
                "type": "rest",
                "_reason": f"rest for heal (HP:{state.hp}, EP:{state.ep})",
            }
        # No heal items: flee from enemies if possible
        if state.enemies_in_region():
            flee = choose_explore_move(state, intel, my_bot_ids)
            if flee:
                flee["_reason"] = f"flee (HP:{state.hp}, no heal)"
                return flee

    # ──── P2: LOW HP (< 60) — heal BEFORE combat ────
    if needs_healing(state):
        heal = get_heal_action(state)  # EP-aware
        if heal:
            return heal
        # EP = 0 + have heal items: rest for heal, NOT for attack
        if has_heal_option(state):
            return {
                "type": "rest",
                "_reason": f"rest for heal (HP:{state.hp}, EP:{state.ep})",
            }

    # ──── P2.5: NO MEDS PANIC (HP < 60) ────
    if state.hp < LOW_HP and not has_heal_option(state) and state.enemies_in_region():
        target = select_target(state, intel, my_bot_ids, pending_ids)
        # Check if the enemy is a free kill before running
        reason = target.get("_reason", "").lower() if target else ""
        if "guaranteed" not in reason and "kill-steal" not in reason:
            flee = choose_explore_move(state, intel, my_bot_ids)
            if flee:
                flee["_reason"] = f"flee (HP:{state.hp}, no meds, dangerous enemies)"
                return flee

    # ──── P3: MOSHPIT EVACUATION (dynamic threshold based on alive count) ────
    enemies_here = [
        e
        for e in state.enemies_in_region()
        if e.id not in (my_bot_ids or set()) and not IS_FRIENDLY_REGEX.match(e.name)
    ]
    blob_threshold = intel.get_blob_threshold() if (intel and intel.available) else 3
    if len(enemies_here) >= blob_threshold and state.ep >= 1:
        flee = choose_explore_move(state, intel, my_bot_ids)
        if flee:
            flee["_reason"] = (
                f"moshpit evacuation ({len(enemies_here)}/{blob_threshold} enemies)"
            )
            return flee

    # ──── UNARMED MODE — find weapon first ────
    if needs_weapon and state.ep >= 1:
        # 1. Supply Cache (get weapon)
        if state.current_region.interactables:
            for fac in state.current_region.interactables:
                if fac.type in ("supply_cache", "supply") and not fac.is_used:
                    return {
                        "type": "interact",
                        "interactableId": fac.id,
                        "_name": "Supply Cache",
                        "_reason": "get weapon from cache",
                    }

        # 2. Farm Weak Monsters (Wolf, Bear)
        if state.ep >= MIN_EP_ATTACK:
            monsters = sorted(
                state.monsters_in_region(),
                key=lambda m: -MONSTER_PRIORITY.get(m.name, 0),
            )
            for m in monsters:
                if m.name in ("Wolf", "Bear"):
                    return {
                        "type": "attack",
                        "targetId": m.id,
                        "targetType": "monster",
                        "_name": m.name,
                        "_hp": m.hp,
                        "_reason": f"farm {m.name} (unarmed)",
                    }

    # ──── ARMED MODE — aggressive per-turn evaluation ────

    target = None
    # 3. ATTACK anything in range (guaranteed kill > lowest HP > monster > ranged)
    if state.ep >= MIN_EP_ATTACK:
        target = select_target(state, intel, my_bot_ids, pending_ids)

        if target:
            reason = target.get("_reason", "").lower()
            is_free_kill = "guaranteed" in reason

            # If unarmed, skip ALL attacks here.
            # We rely purely on GodMode hunting or Desperate Melee at the very end.
            if needs_weapon:
                target = None

            # 1 EP Reserve: Casual attacks on healthy targets need an extra EP (+1)
            elif not is_free_kill and state.ep < (MIN_EP_ATTACK + 1):
                target = None

        if target:
            return target

    # ──── EP SAVE: enemies here but we didn't/couldn't attack ────
    enemies_here = [
        e
        for e in state.enemies_in_region()
        if e.id not in (my_bot_ids or set()) and not IS_FRIENDLY_REGEX.match(e.name)
    ]
    # We should only accumulate EP if we actually need more EP to attack
    # If we have 10 EP (max), or we are unarmed (unarmed doesn't attack unless desperate),
    # we shouldn't get stuck resting forever.
    if enemies_here and not target and state.ep < (MIN_EP_ATTACK + 1):
        if needs_weapon and state.ep >= MIN_EP_ATTACK:
            pass  # Unarmed bots with enough EP should proceed to hunt/desperate melee
        else:
            # Accumulate EP to attack next turn
            drink = get_energy_drink_action(state)
            if drink:
                drink["_reason"] = (
                    f"EP boost for combat ({len(enemies_here)} enemies here)"
                )
                return drink
            return {
                "type": "rest",
                "_reason": f"accumulate EP for attack ({len(enemies_here)} enemies, EP:{state.ep})",
            }

    # 4. Heal top-up — ONLY if region is clear and no weak enemy available
    region_clear = not state.enemies_in_region()
    has_urgent_target = bool(
        intel
        and intel.available
        and intel.find_weak_enemies(exclude_ids=my_bot_ids or set())
    )
    if state.hp < 100 and region_clear and not has_urgent_target:
        heal = get_heal_action(state, force=True)
        if heal:
            heal["_reason"] = f"top-up (HP:{state.hp}, region clear)"
            return heal

    # 5. EP Boost — only if HP is safe (not near healing threshold)
    if state.hp >= LOW_HP:
        drink = get_energy_drink_action(state)
        if drink:
            return drink

    # 6. Supply cache — use free loot before moving away
    cache = get_supply_cache(state)
    if cache and state.ep >= 2:
        return {
            "type": "interact",
            "interactableId": cache["id"],
            "_name": "Supply Cache",
            "_reason": "loot supply cache",
        }

    # 7. Rest if no EP (before any move/hunt that costs EP)
    if should_rest(state):
        return get_rest_action()

    # 8. God Mode hunt — move toward weak enemies / weapons / moltz
    if intel and intel.available and state.ep >= 2:
        pending_ids = {dz.get("id", "") for dz in state.pending_deathzones}
        target_info = intel.get_target_region(
            state.region_id,
            my_bot_ids,
            needs_weapon=needs_weapon,
            max_weapon_dist=2,
            pending_dz_ids=pending_ids,
            weapon_range=state.weapon.range,
        )
        if target_info:
            move = move_toward_target(
                state, target_info["region_id"], intel, pending_dz_ids=pending_ids
            )
            if move:
                move["_reason"] = f"hunt: {target_info['reason']}"
                return move

    # 8.5 DESPERATE MELEE (Baku Hantam)
    # If unarmed, couldn't find a weapon nearby, BUT there are enemies here... punch them instead of exploring!
    if needs_weapon and state.ep >= MIN_EP_ATTACK:
        enemies_here = [
            e
            for e in state.enemies_in_region()
            if e.id not in (my_bot_ids or set()) and not IS_FRIENDLY_REGEX.match(e.name)
        ]
        if enemies_here:
            enemies_here.sort(key=lambda e: e.hp)
            target_enemy = enemies_here[0]
            return {
                "type": "attack",
                "targetId": target_enemy.id,
                "targetType": "agent",
                "_name": target_enemy.name,
                "_hp": target_enemy.hp,
                "_reason": f"desperate melee (HP:{target_enemy.hp:.0f}, no weapon)",
            }

    # 9. Move to new area (terrain-based, no duplicate god mode)
    if state.ep >= 2:
        candidates = _pick_explore_target(state)
        if candidates:
            return candidates

    return get_rest_action()
