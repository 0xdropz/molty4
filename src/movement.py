"""
movement.py — Pathfinding, death zone avoidance, targeted movement.
"""

import re
from src.state_manager import GameState, RegionInfo
from src.god_mode import GodModeIntel
from src.config import TERRAIN_EXPLORE_PRIORITY

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-")


def _readable_name(region: RegionInfo) -> str:
    """Return human-readable name. If name is a UUID, use terrain or short ID."""
    if _UUID_RE.match(region.name):
        # Name is a UUID — try terrain, otherwise show short ID
        if region.terrain and region.terrain != "plains":
            return f"[{region.terrain} #{region.id[:4]}]"
        return f"[region #{region.id[:4]}]"
    return region.name


def must_flee_death_zone(
    state: GameState, intel: GodModeIntel = None, is_purge_time: bool = False
) -> dict | None:
    """If in death zone, return move action to safest region.

    3-layer escape logic:
      1. Filter pending DZ from safe options
      2. Godmode BFS to safest region (farthest from DZ edge)
      3. Fallback: any non-DZ adjacent, then any adjacent
    """
    if not state.is_death_zone:
        return None

    # Collect IDs to avoid: current DZ + pending DZ
    pending_ids = {dz.get("id", "") for dz in state.pending_deathzones}

    # Layer 2: Godmode — find safest region and route there
    if intel and intel.available:
        safest = intel.find_safest_region(pending_dz_ids=pending_ids)
        if safest and safest != state.region_id:
            next_step = intel.find_path_next_step(
                state.region_id,
                safest,
                avoid_dz=True,
                pending_dz=pending_ids,
                avoid_blobs=False,
                is_purge_time=is_purge_time,
            )
            if next_step:
                name = intel.get_region_name(next_step)
                return {
                    "type": "move",
                    "regionId": next_step,
                    "_to_name": name,
                    "_reason": f"flee DZ → safe center ({intel.get_region_name(safest)})",
                }

    # Layer 1: Adjacent safe regions, excluding pending DZ
    safe = [r for r in state.safe_connections() if r.id not in pending_ids]
    if safe:
        safe.sort(
            key=lambda r: TERRAIN_EXPLORE_PRIORITY.get(r.terrain, 0), reverse=True
        )
        target = safe[0]
        return {
            "type": "move",
            "regionId": target.id,
            "_to_name": _readable_name(target),
            "_reason": "flee DZ (safe adjacent)",
        }

    # Layer 3 fallback: any non-DZ connection (even pending — better than staying)
    any_safe = state.safe_connections()
    if any_safe:
        target = any_safe[0]
        return {
            "type": "move",
            "regionId": target.id,
            "_to_name": _readable_name(target),
            "_reason": "flee DZ (pending risk)",
        }

    # Last resort: any connection at all (all neighbors are DZ)
    if state.connected_regions:
        target = state.connected_regions[0]
        return {
            "type": "move",
            "regionId": target.id,
            "_to_name": _readable_name(target),
            "_reason": "flee DZ (all DZ — moving anyway)",
        }

    return None


def move_toward_target(
    state: GameState,
    target_region_id: str,
    intel: GodModeIntel = None,
    pending_dz_ids: set = None,
    avoid_blobs: bool = True,
    is_purge_time: bool = False,
) -> dict | None:
    """
    Move one step toward a target region.
    Uses God Mode BFS if available, otherwise simple adjacency check.
    """
    if not target_region_id or target_region_id == state.region_id:
        return None

    # Check if target is directly adjacent
    for r in state.connected_regions:
        if r.id == target_region_id:
            return {
                "type": "move",
                "regionId": r.id,
                "_to_name": _readable_name(r),
                "_reason": "direct adjacent",
            }

    # Use God Mode pathfinding
    if intel and intel.available:
        next_step = intel.find_path_next_step(
            state.region_id,
            target_region_id,
            avoid_dz=True,
            pending_dz=pending_dz_ids,
            avoid_blobs=avoid_blobs,
            is_purge_time=is_purge_time,
        )
        if next_step:
            region_name = intel.get_region_name(next_step)
            return {
                "type": "move",
                "regionId": next_step,
                "_to_name": region_name,
                "_reason": "god mode pathfinding",
            }

    return None


def choose_explore_move(
    state: GameState,
    intel: GodModeIntel = None,
    my_bot_ids: set = None,
    is_purge_time: bool = False,
) -> dict | None:
    """
    Choose best region to move to for exploration.
    Uses God Mode intel for targeted movement, otherwise terrain-based.
    Adds diversity: randomize among equal candidates to avoid spin loops.
    """
    import random

    # Collect pending death zones early
    pending_ids = set()
    for dz in state.pending_deathzones:
        pending_ids.add(dz.get("id", ""))

    # 1. God Mode targeted pathfinding (DZ-aware)
    if intel and intel.available:
        exclude_for_gm = (
            {state.self_info.id}
            if is_purge_time
            else (my_bot_ids or set()) | {state.self_info.id}
        )
        target = intel.get_target_region(
            state.region_id,
            exclude_for_gm,
            pending_dz_ids=pending_ids,
            is_purge_time=is_purge_time,
        )
        if target:
            move = move_toward_target(
                state,
                target["region_id"],
                intel,
                pending_dz_ids=pending_ids,
                avoid_blobs=True,
                is_purge_time=is_purge_time,
            )
            if move:
                move["_reason"] = f"target: {target['reason']}"
                return move

    # 3. Pick adjacent region — with diversity
    candidates = state.safe_connections()
    # Filter out pending DZ AND blob regions (dynamic threshold based on alive count)
    blob_limit = intel.get_blob_threshold() if (intel and intel.available) else 3
    candidates = [
        r
        for r in candidates
        if r.id not in pending_ids
        and (
            not intel
            or not intel.available
            or intel.get_region_enemy_count(
                r.id, is_purge_time, exclude_ids=my_bot_ids or set()
            )
            < blob_limit
        )
    ]

    if not candidates:
        # fallback layer 1: ignore blob rules if trapped, but STILL avoid pending DZ
        candidates = [r for r in state.safe_connections() if r.id not in pending_ids]

    if not candidates:
        # fallback layer 2: absolutely trapped, ignore pending DZ to escape current death
        candidates = state.safe_connections()

    if not candidates:
        # fallback layer 3: all connections are closed/death zones. Pick any to survive 1 more turn.
        candidates = state.connected_regions

    if not candidates:
        return None

    # Sort by terrain priority, then randomize within same priority tier
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
