"""
strategy.py — V5: CARNAL MODE
Filosofi: Ngecamp di safest region (pusat peta).
- Ada musuh non-prefix dist <= 3 dari safest → kejar & bunuh, lalu balik.
- Tidak ada musuh non-prefix → bantai bot friendly (Carnal penuh).
- Moltz adalah prioritas utama setelah selamatkan nyawa.
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
    get_energy_drink_action,
)
from src.movement import choose_explore_move, move_toward_target
from src.config import (
    MIN_EP_ATTACK,
    LOW_HP,
    MONSTER_PRIORITY,
    IS_FRIENDLY_REGEX,
)


def _find_public_enemy_nearby(
    intel: GodModeIntel,
    safest_region_id: str,
    self_id: str,
    pending_ids: set,
    max_dist: int = 3,
) -> dict | None:
    """
    Scan musuh non-prefix dalam dist <= max_dist dari safest region.
    Return kandidat terdekat dari safest (HP terendah sebagai tiebreaker), atau None.
    """
    if not intel or not intel.available or not safest_region_id:
        return None

    forbidden = intel.dz_region_ids | pending_ids
    candidates = []

    for agent in intel.all_agents:
        if not agent.get("isAlive"):
            continue
        aid = agent.get("id", "")
        aname = agent.get("name", "")
        rid = agent.get("regionId", "")

        if aid == self_id:
            continue
        if IS_FRIENDLY_REGEX.match(aname):
            continue
        if rid in forbidden:
            continue

        dist_from_safest = intel.calculate_distance(
            safest_region_id,
            rid,
            avoid_dz=True,
            pending_dz=pending_ids,
            max_dist=max_dist + 1,
        )
        if dist_from_safest > max_dist:
            continue

        candidates.append(
            {
                "id": aid,
                "name": aname,
                "region_id": rid,
                "region_name": intel.get_region_name(rid),
                "dist_from_safest": dist_from_safest,
                "hp": agent.get("hp", 100),
            }
        )

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x["dist_from_safest"], x["hp"]))
    return candidates[0]


def decide_action(
    state: GameState,
    intel: GodModeIntel = None,
    my_bot_ids: set = None,
) -> dict:
    """
    V5: CARNAL MODE decision engine.

    Fase TRANSIT (dist > 1 dari safest):
    - Gerak ke safest, heal, cari senjata, skip friendly prefix.

    Fase CARNAL (dist <= 1 dari safest):
    - Ada musuh non-prefix dist <= 3 dari safest:
        → Prioritaskan serang musuh non-prefix (IS_FRIENDLY_REGEX aktif).
        → Ranged: tembak dari posisi. Melee: maju ke musuh.
    - Tidak ada musuh non-prefix:
        → Full Carnal: serang siapapun termasuk friendly prefix.
    """

    # Guard: self_info None → rest saja
    if not state.self_info:
        return {"type": "rest", "_reason": "self_info guard fallback"}

    needs_weapon = state.weapon.name == "Fist"
    pending_ids = {dz.get("id", "") for dz in state.pending_deathzones}

    # Hitung safest region sekali di awal
    safest_region_id = None
    dist_to_safest = 0
    if intel and intel.available:
        safest_region_id = intel.find_safest_region(pending_dz_ids=pending_ids)
        if safest_region_id:
            dist_to_safest = intel.calculate_distance(
                state.region_id,
                safest_region_id,
                avoid_dz=True,
                pending_dz=pending_ids,
                max_dist=10,
            )

    # ──── Carnal Mode: cek dinamis setiap turn ────
    is_carnal_mode = (
        intel is not None
        and intel.available
        and safest_region_id is not None
        and dist_to_safest <= 1
    )

    # ──── MODE SELECTION ────
    if is_carnal_mode:
        public_enemy = _find_public_enemy_nearby(
            intel, safest_region_id, state.self_info.id, pending_ids, max_dist=3
        )

        if public_enemy:
            # CARNAL + MUSUH PUBLIK: prioritaskan non-prefix, skip friendly
            is_purge_time = False

            def is_enemy(agent_id: str, agent_name: str) -> bool:
                if agent_id == state.self_info.id:
                    return False
                if IS_FRIENDLY_REGEX.match(agent_name):
                    return False
                return True

            enemies_here = [
                e for e in state.enemies_in_region() if is_enemy(e.id, e.name)
            ]
            exclude_for_gm = (my_bot_ids or set()) | {state.self_info.id}

        else:
            # CARNAL PENUH: bantai siapapun
            is_purge_time = True
            public_enemy = None

            def is_enemy(agent_id: str, agent_name: str) -> bool:
                return agent_id != state.self_info.id

            enemies_here = [
                e for e in state.enemies_in_region() if is_enemy(e.id, e.name)
            ]
            exclude_for_gm = {state.self_info.id}

    else:
        # TRANSIT: skip friendly prefix
        is_purge_time = False
        public_enemy = None
        if intel and intel.available and len(intel.all_agents) > 0:
            _own_ids = (my_bot_ids or set()) | {state.self_info.id}
            _public = [
                a
                for a in intel.all_agents
                if a.get("isAlive")
                and a.get("id") not in _own_ids
                and not IS_FRIENDLY_REGEX.match(a.get("name", ""))
            ]
            if len(_public) == 0:
                is_purge_time = True

        def is_enemy(agent_id: str, agent_name: str) -> bool:
            if agent_id == state.self_info.id:
                return False
            if is_purge_time:
                return True
            if agent_id in (my_bot_ids or set()):
                return False
            if IS_FRIENDLY_REGEX.match(agent_name):
                return False
            return True

        enemies_here = [e for e in state.enemies_in_region() if is_enemy(e.id, e.name)]
        exclude_for_gm = (
            {state.self_info.id}
            if is_purge_time
            else (my_bot_ids or set()) | {state.self_info.id}
        )

    # ──── P0: DEATH ZONE — heal + attack + rest. TIDAK ADA MOVE. ────
    # DZ damage: 1.34 HP/sec = ~80 HP/turn. Bertahan di tempat.
    if state.is_death_zone:
        # A. Heal
        heal = get_heal_action(state)
        if heal:
            return heal
        # B. Energy drink jika ada obat tapi EP kurang
        if has_heal_option(state):
            drink = get_energy_drink_action(state)
            if drink:
                drink["_reason"] = "EP boost untuk heal di DZ"
                return drink
        # C. Attack musuh di petak
        if state.ep >= MIN_EP_ATTACK and enemies_here:
            target = select_target(
                state, intel, exclude_for_gm, pending_ids, is_purge_time
            )
            if target:
                target["_reason"] = (
                    f"DZ attack: {target.get('_name', '')} (HP:{target.get('_hp', '?')})"
                )
                return target
            target_enemy = min(enemies_here, key=lambda e: e.hp)
            return {
                "type": "attack",
                "targetId": target_enemy.id,
                "targetType": "agent",
                "_name": target_enemy.name,
                "_hp": target_enemy.hp,
                "_reason": f"DZ melee: {target_enemy.name} (HP:{target_enemy.hp:.0f})",
            }
        # D. Rest — bonus +1 EP
        return {"type": "rest", "_reason": "DZ: tunggu EP/heal (rest +1 EP)"}

    # ──── P0.5: PENDING DZ EVACUATE ────
    if state.region_id in pending_ids and state.ep >= 1:
        if any(r.id not in pending_ids for r in state.safe_connections()):
            flee = choose_explore_move(state, intel, my_bot_ids, is_purge_time)
            if flee:
                flee["_reason"] = "pending DZ evacuation (leaving before it burns)"
                return flee

    # ──── P1: CRITICAL HP (< 40) ────
    if is_critical(state):
        heal = get_heal_action(state)
        if heal:
            return heal
        if has_heal_option(state):
            drink = get_energy_drink_action(state)
            if drink:
                drink["_reason"] = f"EP boost untuk heal (HP:{state.hp})"
                return drink
        return {
            "type": "rest",
            "_reason": f"HP kritis ({state.hp}), tunggu EP untuk heal",
        }

    # ──── P1.5: MOLTZ GRAB — kejar Moltz via God Mode ────
    # Kejar Moltz dalam dist <= 3 dari safest region, tanpa syarat lain.
    if intel and intel.available and safest_region_id and state.ep >= 1:
        forbidden = intel.dz_region_ids | pending_ids
        best_moltz = None
        best_dist_from_bot = 999
        best_dist_from_safest = 999

        for m in intel.find_moltz_locations():
            rid = m["region_id"]
            if rid in forbidden or rid == state.region_id:
                continue

            dist_from_safest = intel.calculate_distance(
                safest_region_id,
                rid,
                avoid_dz=True,
                pending_dz=pending_ids,
                max_dist=10,
            )
            if dist_from_safest > 3:
                continue

            dist_from_bot = intel.calculate_distance(
                state.region_id,
                rid,
                avoid_dz=True,
                pending_dz=pending_ids,
                max_dist=20,
            )
            if dist_from_bot < best_dist_from_bot:
                best_dist_from_bot = dist_from_bot
                best_dist_from_safest = dist_from_safest
                best_moltz = m

        if best_moltz:
            move = move_toward_target(
                state,
                best_moltz["region_id"],
                intel,
                pending_dz_ids=pending_ids,
                avoid_blobs=False,
                is_purge_time=is_purge_time,
            )
            if move:
                move["_reason"] = (
                    f"MOLTZ GRAB: {best_moltz['region_name']} "
                    f"(dist_bot:{best_dist_from_bot}, dist_safest:{best_dist_from_safest})"
                )
                return move

    # ──── P2: LOW HP (< 60) ────
    if needs_healing(state):
        heal = get_heal_action(state)
        if heal:
            return heal
        if has_heal_option(state):
            drink = get_energy_drink_action(state)
            if drink:
                drink["_reason"] = f"EP boost untuk heal (HP:{state.hp})"
                return drink

    # ──── P2.5: NO MEDS PANIC (Transit only) ────
    if (
        not is_carnal_mode
        and state.hp < LOW_HP
        and not has_heal_option(state)
        and enemies_here
    ):
        target = select_target(state, intel, exclude_for_gm, pending_ids, is_purge_time)
        reason = target.get("_reason", "").lower() if target else ""
        if "guaranteed" not in reason and "kill-steal" not in reason:
            if any(r.id not in pending_ids for r in state.safe_connections()):
                flee = choose_explore_move(state, intel, my_bot_ids, is_purge_time)
                if flee:
                    flee["_reason"] = f"flee (HP:{state.hp}, no meds)"
                    return flee

    # ──── P3: UNARMED MODE (Transit only) ────
    if needs_weapon and state.ep >= 1 and not is_carnal_mode:
        if state.current_region and state.current_region.interactables:
            for fac in state.current_region.interactables:
                if fac.type in ("supply_cache", "supply") and not fac.is_used:
                    return {
                        "type": "interact",
                        "interactableId": fac.id,
                        "_name": "Supply Cache",
                        "_reason": "get weapon from cache",
                    }

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

    # ──── P4: ATTACK ────
    target = None
    if state.ep >= MIN_EP_ATTACK:
        if is_carnal_mode and public_enemy:
            # Carnal + musuh publik: ranged tembak dari sini, melee chase
            dist_to_pub = intel.calculate_distance(
                state.region_id,
                public_enemy["region_id"],
                avoid_dz=True,
                pending_dz=pending_ids,
                max_dist=10,
            )
            in_range = state.weapon.range >= dist_to_pub
            if not in_range:
                for inv_wep in state.weapons_in_inventory():
                    if inv_wep.range >= dist_to_pub:
                        in_range = True
                        break

            if in_range:
                target = select_target(
                    state,
                    intel,
                    exclude_for_gm,
                    pending_ids,
                    is_purge_time=False,
                    priority_target_id=public_enemy["id"],
                )
            elif public_enemy["dist_from_safest"] <= 3 and state.ep >= 1:
                move = move_toward_target(
                    state,
                    public_enemy["region_id"],
                    intel,
                    pending_dz_ids=pending_ids,
                    avoid_blobs=False,
                    is_purge_time=False,
                )
                if move:
                    move["_reason"] = (
                        f"CHASE PUBLIC ENEMY: {public_enemy['name']} "
                        f"(dist:{dist_to_pub}, dist_safest:{public_enemy['dist_from_safest']})"
                    )
                    return move
        else:
            # Transit atau Carnal penuh
            target = select_target(
                state, intel, exclude_for_gm, pending_ids, is_purge_time
            )

            if target:
                reason = target.get("_reason", "").lower()
                is_free_kill = "guaranteed" in reason or "kill-steal" in reason

                if needs_weapon and not is_free_kill and not is_carnal_mode:
                    target = None
                elif (
                    not is_free_kill
                    and not is_carnal_mode
                    and state.ep < (MIN_EP_ATTACK + 1)
                ):
                    target = None

        if target:
            return target

    # ──── P4.5: EP SAVE — tabung EP jika ada musuh di petak ────
    if enemies_here and state.ep < MIN_EP_ATTACK:
        drink = get_energy_drink_action(state)
        if drink:
            drink["_reason"] = f"EP boost for combat ({len(enemies_here)} enemies)"
            return drink
        # Tidak ada energy drink → rest untuk bonus EP
        return {
            "type": "rest",
            "_reason": f"EP save: tunggu EP untuk attack ({len(enemies_here)} enemies)",
        }

    # ──── P5: HEAL TOP-UP & LOOT (region clear) ────
    if not enemies_here:
        if state.hp < 100:
            heal = get_heal_action(state, force=True)
            if heal:
                heal["_reason"] = f"top-up (HP:{state.hp})"
                return heal

        if state.hp >= LOW_HP:
            drink = get_energy_drink_action(state)
            if drink:
                return drink

        cache = get_supply_cache(state)
        if cache and state.ep >= 1:
            return {
                "type": "interact",
                "interactableId": cache["id"],
                "_name": "Supply Cache",
                "_reason": "loot supply cache",
            }

    # ──── P7.5: DESPERATE MELEE ────
    if state.ep >= MIN_EP_ATTACK and enemies_here and (needs_weapon or is_carnal_mode):
        target_enemy = min(enemies_here, key=lambda e: e.hp)
        return {
            "type": "attack",
            "targetId": target_enemy.id,
            "targetType": "agent",
            "_name": target_enemy.name,
            "_hp": target_enemy.hp,
            "_reason": f"desperate melee: {target_enemy.name} (HP:{target_enemy.hp:.0f})",
        }

    # ──── P8: BALIK KE SAFEST REGION ────
    if (
        intel
        and intel.available
        and safest_region_id
        and safest_region_id != state.region_id
        and state.ep >= 1
    ):
        move = move_toward_target(
            state,
            safest_region_id,
            intel,
            pending_dz_ids=pending_ids,
            avoid_blobs=False,
            is_purge_time=is_purge_time,
        )
        if move:
            move["_reason"] = f"balik ke safest region (dist:{dist_to_safest})"
            return move

    # ──── P9: FALLBACK ────
    return {"type": "rest", "_reason": "idle at safest"}
