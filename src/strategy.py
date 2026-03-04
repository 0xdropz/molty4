"""
strategy.py — V6: Local-state-only priority system (no God Mode).
All decisions are made purely from agent state: self, visible agents/monsters/items,
connected regions, and pending deathzones.
"""

from src.state_manager import GameState
from src.combat import select_target
from src.survival import (
    is_critical,
    needs_healing,
    get_heal_action,
    has_heal_option,
    get_supply_cache,
    get_medical_facility,
    get_energy_drink_action,
    get_rest_action,
)
from src.movement import move_toward_target, get_safest_neighbor
from src.config import (
    MIN_EP_ATTACK,
    LOW_HP,
    MONSTER_PRIORITY,
    IS_FRIENDLY_REGEX,
    SULTAN_THRESHOLD,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _count_moltz_in_inventory(agent_raw: dict) -> int:
    """Count Moltz carried by an agent from raw visibleAgents dict."""
    total = 0
    for item_wrapper in agent_raw.get("inventory", []):
        item = item_wrapper.get("item", item_wrapper)
        if (
            item.get("typeId") in ("rewards", "reward1")
            or item.get("category") in ("currency", "rewards")
            or "moltz" in item.get("name", "").lower()
        ):
            qty = (
                item_wrapper.get("quantity")
                or item_wrapper.get("amount")
                or item_wrapper.get("count")
                or item.get("quantity")
                or item.get("amount")
                or 1
            )
            total += int(qty or 0)
    return total


def _find_sultan_from_visible(
    state: "GameState",
    threshold: int,
    my_id: str,
) -> dict | None:
    """
    Scan visibleAgents (raw) for enemies carrying Moltz >= threshold.
    Returns dict with id, name, moltz, region_id, dist, or None.
    Only sees agents within visible range (same region or adjacent).
    """
    pending_ids = {dz.get("id", "") for dz in state.pending_deathzones}
    candidates = []

    for agent in state.raw_visible_agents:
        aid = agent.get("id", "")
        name = agent.get("name", "")
        rid = agent.get("regionId", "")

        if not agent.get("isAlive", True) or aid == my_id:
            continue
        if IS_FRIENDLY_REGEX.match(name):
            continue
        # Skip sultan in pending DZ unless already in same region
        if rid in pending_ids and rid != state.region_id:
            continue

        moltz = _count_moltz_in_inventory(agent)
        if moltz < threshold:
            continue

        # Distance: 0 same region, 1 adjacent, 999 unknown
        if rid == state.region_id:
            dist = 0
        elif any(r.id == rid for r in state.connected_regions):
            dist = 1
        else:
            dist = 999

        candidates.append(
            {
                "id": aid,
                "name": name,
                "moltz": moltz,
                "region_id": rid,
                "dist": dist,
            }
        )

    if not candidates:
        return None

    # Closest first, richest as tiebreaker
    candidates.sort(key=lambda x: (x["dist"], -x["moltz"]))
    return candidates[0]


def _find_moltz_from_visible(state: "GameState") -> dict | None:
    """
    Scan visibleItems for Moltz on the ground outside current region.
    Returns dict with item_id, region_id, dist, or None.
    """
    pending_ids = {dz.get("id", "") for dz in state.pending_deathzones}
    candidates = []

    for ri in state.visible_items:
        rid = ri.region_id
        item = ri.item
        if rid == state.region_id:
            continue  # already handled by loot.py pickup
        if rid in pending_ids:
            continue
        is_moltz = (
            item.type_id in ("rewards", "reward1")
            or item.category in ("currency", "rewards")
            or "moltz" in item.name.lower()
        )
        if not is_moltz:
            continue

        if any(r.id == rid for r in state.connected_regions):
            dist = 1
        else:
            dist = 2  # visible but not adjacent — assume 2 hops

        candidates.append({"item_id": item.id, "region_id": rid, "dist": dist})

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["dist"])
    return candidates[0]


# ── Main Decision Engine ──────────────────────────────────────────────────────


def decide_action(
    state: GameState,
    my_bot_ids: set = None,
) -> dict:
    """
    V6: Local-state-only priority system.

    P0  — In DZ: heal → EP drink (to unblock heal) → flee if safe exit exists
            else fall through (no trapped combat, no rest — cascade handles it)
    P1  — In pending DZ + EP >= 1: flee via get_safest_neighbor
    P2  — Critical HP (< 40): heal → EP drink
    P3  — Sultan visible + EP >= 2 + in range: attack; EP >= 1: move toward
    P4  — Enemy in region + EP >= 2: full aggro
    P5  — HP < 60: heal → EP drink
    P6  — Monster in region + EP >= 2: attack Wolf/Bear (Bandit always skipped)
    P7  — Sultan visible but EP < 2: EP drink → move toward
    P8  — Supply cache in region + EP >= 1: interact
    P9  — Medical facility in region + HP < 100: interact
    P10 — Moltz on ground in other region + EP >= 1: move toward
    P11 — EP >= 1: move to best terrain neighbor
    P12 — EP = 0: rest
    P13 — None (fallback)
    """

    pending_ids = {dz.get("id", "") for dz in state.pending_deathzones}
    my_id = state.self_info.id

    def is_enemy(agent_id: str, agent_name: str) -> bool:
        if agent_id == my_id:
            return False
        if IS_FRIENDLY_REGEX.match(agent_name):
            return False
        return True

    enemies_here = [e for e in state.enemies_in_region() if is_enemy(e.id, e.name)]

    # Sultan scan — visible range only (no God Mode)
    sultan = _find_sultan_from_visible(state, threshold=SULTAN_THRESHOLD, my_id=my_id)

    # ── P0: DEATH ZONE ─────────────────────────────────────────────────────
    if state.is_death_zone:
        # A. Heal if possible
        heal = get_heal_action(state)
        if heal:
            return heal

        # B. EP drink to unblock a heal we couldn't afford
        if has_heal_option(state):
            drink = get_energy_drink_action(state)
            if drink:
                drink["_reason"] = "EP boost to heal in DZ"
                return drink

        # C. Flee if there's a safe exit and we have EP
        if state.ep >= 1:
            flee = get_safest_neighbor(state)
            # Only flee if the chosen neighbor is not also a death zone
            if flee and not _is_dz_region(flee["regionId"], state):
                flee["_reason"] = "flee DZ → safest neighbor"
                return flee

        # D. No safe exit or no EP — fall through to P2+
        # (rest at P12 will handle EP=0, cascade handles everything else)

    # ── P1: PENDING DZ — evacuate ──────────────────────────────────────────
    if state.region_id in pending_ids and state.ep >= 1:
        flee = get_safest_neighbor(state)
        if flee:
            flee["_reason"] = "pending DZ evacuation → safest neighbor"
            return flee

    # ── P2: CRITICAL HP (< 40) ─────────────────────────────────────────────
    if is_critical(state):
        heal = get_heal_action(state)
        if heal:
            return heal
        if has_heal_option(state):
            drink = get_energy_drink_action(state)
            if drink:
                drink["_reason"] = f"EP boost to heal (HP:{state.hp:.0f})"
                return drink

    # ── P3: SULTAN VISIBLE + EP >= 2 ───────────────────────────────────────
    if sultan and state.ep >= MIN_EP_ATTACK:
        dist = sultan.get("dist", 999)
        in_range = state.weapon.range >= dist

        if in_range:
            # Attack sultan directly if in same region
            if sultan["region_id"] == state.region_id:
                target = select_target(
                    state,
                    None,
                    my_bot_ids or set(),
                    pending_ids,
                    is_purge_time=False,
                    priority_target_id=sultan["id"],
                )
                if target:
                    target["_reason"] = (
                        f"SULTAN HUNT: {sultan['name']} ({sultan['moltz']} Moltz)"
                    )
                    return target
            # Ranged: sultan adjacent, weapon range covers it
            else:
                return {
                    "type": "attack",
                    "targetId": sultan["id"],
                    "targetType": "agent",
                    "_name": sultan["name"],
                    "_hp": None,
                    "_reason": (
                        f"SULTAN RANGED: {sultan['name']} "
                        f"({sultan['moltz']} Moltz, dist:{dist})"
                    ),
                }

        # Sultan not in range — move toward (adjacent check only, no God Mode)
        if state.ep >= 1:
            move = move_toward_target(state, sultan["region_id"])
            if not move:
                move = get_safest_neighbor(state)
            if move:
                move["_reason"] = (
                    f"SULTAN CHASE: {sultan['name']} "
                    f"({sultan['moltz']} Moltz, dist:{dist})"
                )
                return move

    # ── P4: ENEMY IN REGION + EP >= 2 ──────────────────────────────────────
    if enemies_here and state.ep >= MIN_EP_ATTACK:
        sultan_here_id = (
            sultan["id"]
            if (sultan and sultan["region_id"] == state.region_id)
            else None
        )
        target = select_target(
            state,
            None,
            my_bot_ids or set(),
            pending_ids,
            is_purge_time=False,
            priority_target_id=sultan_here_id,
        )
        if target:
            target["_reason"] = (
                f"AGGRO: {target.get('_name', '')} (HP:{target.get('_hp', '?')})"
            )
            return target
        # Fallback: hit lowest HP enemy
        target_enemy = min(enemies_here, key=lambda e: e.hp)
        return {
            "type": "attack",
            "targetId": target_enemy.id,
            "targetType": "agent",
            "_name": target_enemy.name,
            "_hp": target_enemy.hp,
            "_reason": f"AGGRO fallback: {target_enemy.name} (HP:{target_enemy.hp:.0f})",
        }

    # ── P5: LOW HP (< 60) ──────────────────────────────────────────────────
    if needs_healing(state):
        heal = get_heal_action(state)
        if heal:
            return heal
        if has_heal_option(state):
            drink = get_energy_drink_action(state)
            if drink:
                drink["_reason"] = f"EP boost to heal (HP:{state.hp:.0f})"
                return drink

    # ── P6: MONSTER IN REGION + EP >= 2 (skip Bandit) ──────────────────────
    if state.ep >= MIN_EP_ATTACK:
        monsters = [m for m in state.monsters_in_region() if m.name in ("Wolf", "Bear")]
        if monsters:
            monsters.sort(key=lambda m: -MONSTER_PRIORITY.get(m.name, 0))
            m = monsters[0]
            return {
                "type": "attack",
                "targetId": m.id,
                "targetType": "monster",
                "_name": m.name,
                "_hp": m.hp,
                "_reason": f"MONSTER: {m.name} (HP:{m.hp:.0f})",
            }

    # ── P7: SULTAN VISIBLE BUT EP < 2 ──────────────────────────────────────
    if sultan and state.ep < MIN_EP_ATTACK:
        drink = get_energy_drink_action(state)
        if drink:
            drink["_reason"] = f"EP boost for sultan hunt ({sultan['name']})"
            return drink
        # Move closer while EP is 1
        if state.ep >= 1:
            move = move_toward_target(state, sultan["region_id"])
            if not move:
                move = get_safest_neighbor(state)
            if move:
                move["_reason"] = (
                    f"SULTAN APPROACH (EP<2): {sultan['name']} "
                    f"({sultan['moltz']} Moltz)"
                )
                return move

    # ── P8: SUPPLY CACHE IN REGION + EP >= 1 ───────────────────────────────
    if state.ep >= 1:
        cache = get_supply_cache(state)
        if cache:
            return {
                "type": "interact",
                "interactableId": cache["id"],
                "_name": "Supply Cache",
                "_reason": "loot supply cache",
            }

    # ── P9: MEDICAL FACILITY IN REGION + HP < 100 ──────────────────────────
    if state.hp < 100:
        facility = get_medical_facility(state)
        if facility:
            return {
                "type": "interact",
                "interactableId": facility["id"],
                "_name": "Medical Facility",
                "_reason": f"heal at facility (HP:{state.hp:.0f})",
            }

    # ── P10: MOLTZ ON GROUND IN OTHER REGION + EP >= 1 ─────────────────────
    if state.ep >= 1:
        moltz_target = _find_moltz_from_visible(state)
        if moltz_target:
            move = move_toward_target(state, moltz_target["region_id"])
            if not move:
                move = get_safest_neighbor(state)
            if move:
                move["_reason"] = f"MOLTZ GRAB (dist:{moltz_target['dist']})"
                return move

    # ── P11: MOVE TO BEST TERRAIN NEIGHBOR + EP >= 1 ───────────────────────
    if state.ep >= 1:
        move = get_safest_neighbor(state)
        if move:
            move["_reason"] = f"roam → {move.get('_to_name', '?')}"
            return move

    # ── P12: REST (EP = 0) ──────────────────────────────────────────────────
    if state.ep == 0:
        return get_rest_action()

    # ── P13: FALLBACK ───────────────────────────────────────────────────────
    return None


# ── Internal helpers ──────────────────────────────────────────────────────────


def _is_dz_region(region_id: str, state: GameState) -> bool:
    """Check if a region ID is an active death zone based on connected region info."""
    for r in state.connected_regions:
        if r.id == region_id:
            return r.is_death_zone
    return False
