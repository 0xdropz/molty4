"""
god_mode.py — God Mode V2 (WebSocket Adapter)
Membaca data dari shared GodModeCache dan menyediakan utilitas untuk bot.
"""

from typing import List, Dict, Any, Set
from collections import deque
import re
from src.config import IS_FRIENDLY_REGEX


class GodModeIntel:
    def __init__(self, cache, game_id: str):
        self.cache = cache
        self.game_id = game_id

    @property
    def available(self) -> bool:
        return self.cache is not None and self.cache.get_state(self.game_id) is not None

    @property
    def raw_state(self) -> dict | None:
        if not self.cache:
            return None
        return self.cache.get_state(self.game_id)

    @property
    def all_agents(self) -> List[Dict[str, Any]]:
        state = self.raw_state
        return state.get("agents", []) if state else []

    @property
    def all_regions(self) -> List[Dict[str, Any]]:
        state = self.raw_state
        return state.get("regions", []) if state else []

    @property
    def all_items(self) -> List[Dict[str, Any]]:
        state = self.raw_state
        return state.get("items", []) if state else []

    @property
    def all_monsters(self) -> List[Dict[str, Any]]:
        state = self.raw_state
        return state.get("monsters", []) if state else []

    # --- Analytics & Search ---

    def _get_connections(self, region: dict) -> List[str]:
        """Get connected region IDs — server uses 'connections' key (not 'connectedRegionIds')."""
        return (
            region.get("connections")
            or region.get("connectedRegionIds")
            or region.get("connectedRegions")
            or []
        )

    def _build_graph(self) -> Dict[str, List[str]]:
        graph = {}
        for r in self.all_regions:
            graph[r["id"]] = self._get_connections(r)
        return graph

    @property
    def game_status(self) -> str:
        """Get game status from WS state. Server stores it in state['room']['status']."""
        state = self.raw_state
        if not state:
            return "unknown"
        # Try root level first, then inside 'room' object
        return (
            state.get("gameStatus")
            or state.get("status")
            or state.get("room", {}).get("status")
            or "unknown"
        )

    def get_region_name(self, region_id: str) -> str:
        for r in self.all_regions:
            if r["id"] == region_id:
                # Use terrain if name is just UUID
                name = r.get("name", "")
                if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", name):
                    return f"[{r.get('terrain', 'unknown')} #{region_id[:4]}]"
                return name
        return region_id

    def calculate_distance(self, start_id: str, end_id: str, max_dist: int = 6) -> int:
        if not self.available or start_id == end_id:
            return 0

        graph = self._build_graph()
        if start_id not in graph or end_id not in graph:
            return 999

        queue = deque([(start_id, 0)])
        visited = {start_id}

        while queue:
            curr, dist = queue.popleft()
            if curr == end_id:
                return dist
            if dist >= max_dist:
                continue

            for neighbor in graph.get(curr, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, dist + 1))

        return 999

    def find_safest_region(self, pending_dz_ids: set = None) -> str | None:
        """BFS from all death zones to find the furthest safe region."""
        if not self.available:
            return None

        regions = self.all_regions
        if not regions:
            return None

        graph = self._build_graph()
        pending = pending_dz_ids or set()

        # Build set of all danger (DZ) region IDs
        danger_set = set()
        danger_nodes = []
        for r in regions:
            rid = r["id"]
            if r.get("isDeathZone", False) or rid in pending:
                danger_set.add(rid)
                danger_nodes.append(rid)

        if not danger_nodes:
            # No danger yet, pick center or any high-value terrain
            for r in regions:
                if r.get("terrain") == "ruins":
                    return r["id"]
            return regions[0]["id"]

        # Multi-source BFS from all danger nodes
        queue = deque([(node, 0) for node in danger_nodes])
        distances = {node: 0 for node in danger_nodes}

        furthest_node = None
        max_dist = -1

        while queue:
            curr, dist = queue.popleft()

            # Only track safe (non-DZ) nodes as candidates
            if curr not in danger_set and dist > max_dist:
                max_dist = dist
                furthest_node = curr

            for neighbor in graph.get(curr, []):
                if neighbor not in distances:
                    distances[neighbor] = dist + 1
                    queue.append((neighbor, dist + 1))

        # Fallback: if all regions are DZ, return the least-bad one
        if furthest_node is None and regions:
            return regions[0]["id"]

        return furthest_node

    def find_path_next_step(
        self,
        start_id: str,
        target_id: str,
        avoid_dz: bool = True,
        pending_dz: set = None,
    ) -> str | None:
        """Find the next immediate region ID to move to in order to reach target_id."""
        if not self.available or start_id == target_id:
            return None

        graph = self._build_graph()
        if start_id not in graph or target_id not in graph:
            return None

        pending = pending_dz or set()
        unsafe = set()
        if avoid_dz:
            for r in self.all_regions:
                if r.get("isDeathZone", False) or r["id"] in pending:
                    unsafe.add(r["id"])

        # Allow walking into unsafe if target itself is inside unsafe (like hunting sultan in DZ)
        if target_id in unsafe:
            unsafe.remove(target_id)

        # BFS
        queue = deque([(start_id, [])])
        visited = {start_id}

        while queue:
            curr, path = queue.popleft()

            if curr == target_id:
                return path[0] if path else None

            for neighbor in graph.get(curr, []):
                if neighbor not in visited and neighbor not in unsafe:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))

        return None

    def _count_moltz(self, agent: dict) -> int:
        """Count Moltz in agent inventory. WS state uses flat inventory items with 'quantity' field."""
        total = 0
        for item in agent.get("inventory", []):
            # WS state: flat format {id, typeId, name, category, quantity, ...}
            # No nested {item: {...}} wrapper in WS state
            inner = item.get("item", item)  # handle both flat and nested just in case
            is_moltz = (
                inner.get("category") in ("currency", "rewards")
                or inner.get("typeId") in ("rewards", "reward1", "moltz", "currency")
                or "moltz" in inner.get("name", "").lower()
            )
            if is_moltz:
                qty = (
                    item.get("quantity")
                    or item.get("amount")
                    or item.get("count")
                    or inner.get("quantity")
                    or inner.get("amount")
                    or 1
                )
                total += int(qty or 0)
        # NOTE: agent.rewards field is a server-side mirror of inventory Moltz qty.
        # Adding it would double-count. Inventory scan above is the single source of truth.
        return total

    def find_nearest_enemy(
        self,
        current_id: str,
        max_dist: int = 4,
        pending_dz_ids: set = None,
        my_bot_ids: set = None,
    ) -> dict | None:
        """Find the nearest non-friendly alive agent within max_dist hops.

        Used for zoning: when bot is already in safe zone and wants to engage
        the closest threat rather than patrolling blindly.

        Returns dict with id, name, region_id, dist, hp, kills — or None.
        """
        if not self.available:
            return None

        pending = pending_dz_ids or set()
        friendly = my_bot_ids or set()

        # Build danger set to avoid routing through DZ
        danger_set = set()
        for r in self.all_regions:
            if r.get("isDeathZone", False) or r["id"] in pending:
                danger_set.add(r["id"])

        # BFS from current position up to max_dist hops
        graph = self._build_graph()
        queue = deque([(current_id, 0)])
        visited = {current_id}
        reachable = {}  # region_id → dist

        while queue:
            curr, dist = queue.popleft()
            if dist > 0:
                reachable[curr] = dist
            if dist >= max_dist:
                continue
            for nb in graph.get(curr, []):
                if nb not in visited and nb not in danger_set:
                    visited.add(nb)
                    queue.append((nb, dist + 1))

        if not reachable:
            return None

        # Find closest non-friendly alive agent in reachable regions
        best = None
        best_dist = 999

        for a in self.all_agents:
            if not a.get("isAlive", False):
                continue
            name = a.get("name", "")
            if IS_FRIENDLY_REGEX.match(name):
                continue
            if name in friendly:
                continue
            rid = a.get("regionId", "")
            if rid not in reachable:
                continue
            dist = reachable[rid]
            if dist < best_dist:
                best_dist = dist
                best = {
                    "id": a["id"],
                    "name": name,
                    "region_id": rid,
                    "dist": dist,
                    "hp": a.get("hp", 0),
                    "kills": a.get("kills", 0),
                }

        return best

    def find_sultan(self, threshold: int = 30) -> dict | None:
        """Find the non-friendly agent with the most Moltz."""
        if not self.available:
            return None

        best_agent = None
        max_moltz = -1

        for a in self.all_agents:
            if not a.get("isAlive", False):
                continue
            name = a.get("name", "")
            if IS_FRIENDLY_REGEX.match(name):
                continue

            moltz = self._count_moltz(a)
            if moltz >= threshold and moltz > max_moltz:
                max_moltz = moltz
                best_agent = {
                    "id": a["id"],
                    "name": name,
                    "region_id": a["regionId"],
                    "moltz": moltz,
                }

        return best_agent

    def find_killer(self, threshold: int = 2) -> dict | None:
        """Find the non-friendly agent with the most kills."""
        if not self.available:
            return None

        best_agent = None
        max_kills = -1

        for a in self.all_agents:
            if not a.get("isAlive", False):
                continue
            name = a.get("name", "")
            if IS_FRIENDLY_REGEX.match(name):
                continue

            kills = a.get("kills", 0)
            if kills >= threshold and kills > max_kills:
                max_kills = kills
                best_agent = {
                    "id": a["id"],
                    "name": name,
                    "region_id": a["regionId"],
                    "kills": kills,
                }

        return best_agent
