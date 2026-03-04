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


def get_safest_neighbor(state: GameState) -> dict | None:
    """
    Pick the safest adjacent region without God Mode.
    Fallback navigation: flee DZ, avoid pending DZ, prefer terrain.
    Returns a move action dict or None if nowhere to go.
    """
    pending_ids = {dz.get("id", "") for dz in state.pending_deathzones}

    # Layer 1: non-DZ, non-pending neighbors — sorted by terrain score
    safe = [
        r
        for r in state.connected_regions
        if not r.is_death_zone and r.id not in pending_ids
    ]
    if safe:
        safe.sort(
            key=lambda r: TERRAIN_EXPLORE_PRIORITY.get(r.terrain, 0), reverse=True
        )
        target = safe[0]
        return {
            "type": "move",
            "regionId": target.id,
            "_to_name": _readable_name(target),
            "_reason": f"safest neighbor ({target.terrain})",
        }

    # Layer 2: non-DZ neighbors (may include pending)
    non_dz = state.safe_connections()
    if non_dz:
        non_dz.sort(
            key=lambda r: TERRAIN_EXPLORE_PRIORITY.get(r.terrain, 0), reverse=True
        )
        target = non_dz[0]
        return {
            "type": "move",
            "regionId": target.id,
            "_to_name": _readable_name(target),
            "_reason": "safest neighbor (pending DZ risk)",
        }

    # Layer 3: any connection (all neighbors are DZ)
    if state.connected_regions:
        target = state.connected_regions[0]
        return {
            "type": "move",
            "regionId": target.id,
            "_to_name": _readable_name(target),
            "_reason": "safest neighbor (all DZ — escaping)",
        }

    return None


def move_toward_target(
    state: GameState,
    target_region_id: str,
    intel: GodModeIntel = None,
    pending_dz_ids: set = None,
) -> dict | None:
    """
    Move one step toward a target region.
    Uses simple adjacency check, then falls back to God Mode pathfinding if available.
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
