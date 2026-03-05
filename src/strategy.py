"""
strategy.py — V6: Local-state-only priority system (no God Mode).
All decisions are made purely from agent state: self, visible agents/monsters/items,
connected regions, and pending deathzones.
"""

from src.state_manager import GameState
from src.god_mode import GodModeIntel
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
    KILLER_THRESHOLD,
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
    intel: GodModeIntel = None,
    my_bot_ids: set = None,
) -> dict:
    """
    V6: Local-state-only priority system.

    P0  — In DZ: heal → EP drink → flee if safe exit exists → else DOOMSDAY PROTOCOL (Spam Heal, Spam Drink, Attack, Rest)
    P1  — In pending DZ + EP >= 1: flee via get_safest_neighbor
    P2  — Critical HP (< 40): heal → EP drink
    P3  — Killer Active (>= 2 kills) + EP >= 1: Move/Attack
    P4  — Sultan Active (>= 30 Moltz) + EP >= 1: Move/Attack
    P5  — Enemy in region + EP >= 2: full aggro
    P6  — HP < 60: heal → EP drink
    P7  — Monster in region + EP >= 2: attack Wolf/Bear (Bandit always skipped)
    P8  — Supply cache in region + EP >= 1: interact
    P9  — Medical facility in region + HP < 100: interact
    P10 — Moltz on ground in other region + EP >= 1: move toward
    P11 — God Mode available:
              P11a dist>4 dari safe center → approach safe center
              P11b dalam zona + ada musuh dist<=4 → zone hunt (move toward)
              P11c dalam zona + tidak ada musuh → rest (tunggu musuh masuk zona)
    P12 — Move to best terrain neighbor (if no god mode)
    P13 — Rest if EP = 0
    P14 — None (fallback)
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

    # Sultan and Killer scans
    sultan = (
        intel.find_sultan(SULTAN_THRESHOLD)
        if (intel and intel.available)
        else _find_sultan_from_visible(state, SULTAN_THRESHOLD, my_id)
    )
    killer = (
        intel.find_killer(KILLER_THRESHOLD) if (intel and intel.available) else None
    )

    # ── P0: DEATH ZONE ─────────────────────────────────────────────────────
    if state.is_death_zone:
        # A. Flee FIRST if there's a safe exit and we have EP — don't waste resources
        if state.ep >= 1:
            flee = get_safest_neighbor(state)
            # Only flee if the chosen neighbor is not also a death zone
            if flee and not _is_dz_region(flee["regionId"], state):
                flee["_reason"] = "flee DZ → safest neighbor"
                return flee

        # B. Heal if possible (no safe exit, or no EP to flee)
        heal = get_heal_action(state)
        if heal:
            return heal

        # C. EP drink to unblock a heal we couldn't afford
        if has_heal_option(state):
            drink = get_energy_drink_action(state)
            if drink:
                drink["_reason"] = "EP boost to heal in DZ"
                return drink

        # D. DOOMSDAY PROTOCOL: No safe exit — survive as long as possible
        # NOTE: use_item costs 1 EP, so we can only act if ep >= 1
        if state.ep >= MIN_EP_ATTACK:
            # 1. Last Stand Attack (Hit anyone trapped here to get their drops)
            if enemies_here:
                target_enemy = min(enemies_here, key=lambda e: e.hp)
                return {
                    "type": "attack",
                    "targetId": target_enemy.id,
                    "targetType": "agent",
                    "_name": target_enemy.name,
                    "_hp": target_enemy.hp,
                    "_reason": f"DOOMSDAY AGGRO: {target_enemy.name} (HP:{target_enemy.hp:.0f})",
                }
            # 2. Hit monsters
            monsters = state.monsters_in_region()
            if monsters:
                m = monsters[0]
                return {
                    "type": "attack",
                    "targetId": m.id,
                    "targetType": "monster",
                    "_name": m.name,
                    "_hp": m.hp,
                    "_reason": f"DOOMSDAY MONSTER: {m.name} (HP:{m.hp:.0f})",
                }

        # 5. Rest / Wait — regenerate 1 EP per turn
        return get_rest_action()

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

    # ── P3: KILLER HUNTER ──────────────────────────────────────────────────
    if killer and state.ep >= 1:
        dist = (
            intel.calculate_distance(state.region_id, killer["region_id"], max_dist=6)
            if intel
            else 999
        )
        in_range = state.weapon.range >= dist

        if in_range and state.ep >= MIN_EP_ATTACK:
            # Ranged: killer adjacent, weapon range covers it
            if dist > 0:
                return {
                    "type": "attack",
                    "targetId": killer["id"],
                    "targetType": "agent",
                    "_name": killer["name"],
                    "_hp": None,
                    "_reason": f"KILLER RANGED: {killer['name']} ({killer['kills']} Kills, dist:{dist})",
                }
            # Same region attack handled by P5 (AGGRO) with priority_target_id

        # Killer not in range — move toward
        if not in_range and state.ep >= 1:
            move = move_toward_target(state, killer["region_id"], intel, pending_ids)
            if move:
                move["_reason"] = (
                    f"KILLER CHASE: {killer['name']} ({killer['kills']} Kills, dist:{dist})"
                )
                return move

    # ── P4: SULTAN HUNTER ──────────────────────────────────────────────────
    if sultan and state.ep >= 1:
        dist = (
            intel.calculate_distance(state.region_id, sultan["region_id"], max_dist=6)
            if intel
            else sultan.get("dist", 999)
        )
        in_range = state.weapon.range >= dist

        if in_range and state.ep >= MIN_EP_ATTACK:
            if dist > 0:
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

        # Sultan not in range — move toward
        if not in_range and state.ep >= 1:
            move = move_toward_target(state, sultan["region_id"], intel, pending_ids)
            if move:
                move["_reason"] = (
                    f"SULTAN CHASE: {sultan['name']} "
                    f"({sultan['moltz']} Moltz, dist:{dist})"
                )
                return move

    # ── P5: ENEMY IN REGION + EP >= 2 ──────────────────────────────────────
    if enemies_here and state.ep >= MIN_EP_ATTACK:
        sultan_here_id = (
            sultan["id"]
            if (sultan and sultan["region_id"] == state.region_id)
            else None
        )
        killer_here_id = (
            killer["id"]
            if (killer and killer["region_id"] == state.region_id)
            else None
        )

        priority_id = killer_here_id or sultan_here_id

        target = select_target(
            state,
            intel,
            my_bot_ids or set(),
            pending_ids,
            is_purge_time=False,
            priority_target_id=priority_id,
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

    # ── P6: LOW HP (< 60) ──────────────────────────────────────────────────
    if needs_healing(state):
        heal = get_heal_action(state)
        if heal:
            return heal
        if has_heal_option(state):
            drink = get_energy_drink_action(state)
            if drink:
                drink["_reason"] = f"EP boost to heal (HP:{state.hp:.0f})"
                return drink

    # ── P7: MONSTER IN REGION + EP >= 2 (skip Bandit) ──────────────────────
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
            move = move_toward_target(
                state, moltz_target["region_id"], intel, pending_ids
            )
            if not move:
                move = get_safest_neighbor(state)
            if move:
                move["_reason"] = f"MOLTZ GRAB (dist:{moltz_target['dist']})"
                return move

    # ── P11: ZONING (God Mode) ───────────────────────────────────────────────
    if state.ep >= 1 and intel and intel.available:
        safe_center_id = intel.find_safest_region(pending_dz_ids=pending_ids)

        if safe_center_id:
            dist_to_center = intel.calculate_distance(
                state.region_id, safe_center_id, max_dist=20
            )

            # P11a — belum dalam zona (> 4 hop dari safe center) → mendekati safe center
            if dist_to_center > 4:
                move = move_toward_target(state, safe_center_id, intel, pending_ids)
                if move:
                    move["_reason"] = (
                        f"approach zone → {intel.get_region_name(safe_center_id)} "
                        f"(dist:{dist_to_center})"
                    )
                    return move

            # P11b — sudah dalam zona → cari musuh terdekat dalam radius 4 hop
            nearest = intel.find_nearest_enemy(
                current_id=state.region_id,
                max_dist=4,
                pending_dz_ids=pending_ids,
                my_bot_ids=my_bot_ids,
            )
            if nearest:
                move = move_toward_target(
                    state, nearest["region_id"], intel, pending_ids
                )
                if move:
                    move["_reason"] = (
                        f"ZONE HUNT: {nearest['name']} "
                        f"(dist:{nearest['dist']} HP:{nearest['hp']:.0f})"
                    )
                    return move

            # P11c — dalam zona, tidak ada musuh → rest (hemat EP, tunggu musuh masuk)
            return get_rest_action()

    # ── P12: MOVE TO BEST TERRAIN NEIGHBOR (No God Mode Fallback) ───────────
    if state.ep >= 1:
        move = get_safest_neighbor(state)
        if move:
            move["_reason"] = f"roam → {move.get('_to_name', '?')}"
            return move

    # ── P13: REST (EP = 0) ──────────────────────────────────────────────────
    if state.ep == 0:
        return get_rest_action()

    # ── P14: FALLBACK ───────────────────────────────────────────────────────
    return None


# ── Internal helpers ──────────────────────────────────────────────────────────


def _is_dz_region(region_id: str, state: GameState) -> bool:
    """Check if a region ID is an active death zone based on connected region info."""
    for r in state.connected_regions:
        if r.id == region_id:
            return r.is_death_zone
    return False
