"""
god_mode.py — Full map visibility via spectator endpoint.
Always-on, fallback to normal state if fails.
Provides targeted pathfinding to weapons, enemies, monsters.
"""

from src.state_manager import EnemyInfo, MonsterInfo, RegionItem, ItemInfo, RegionInfo
from src.config import WEAPON_PRIORITY, KILL_STEAL_HP, IS_FRIENDLY_REGEX


class GodModeIntel:
    """Parsed intel from God Mode full state scan."""

    def __init__(self):
        self.available = False
        self.all_agents: list[dict] = []
        self.all_items: list[dict] = []
        self.all_monsters: list[dict] = []
        self.all_regions: list[dict] = []
        self.region_map: dict[str, dict] = {}  # region_id -> region data

    def update(self, full_state: dict):
        """Update from GET /games/{gameId}/state response."""
        if not full_state:
            self.available = False
            return

        # Check for explicit error
        if full_state.get("success") is False:
            self.available = False
            return

        # Handle wrapped {success, data} or unwrapped {agents, regions, ...}
        if "data" in full_state and isinstance(full_state["data"], dict):
            data = full_state["data"]
        elif "agents" in full_state or "regions" in full_state:
            data = full_state
        else:
            self.available = False
            return

        self.available = True
        self.all_agents = data.get("agents", [])
        self.all_items = data.get("items", [])
        self.all_monsters = data.get("monsters", [])
        self.all_regions = data.get("regions", [])

        # Build region lookup
        self.region_map = {}
        self._dz_ids: set[str] = set()
        for r in self.all_regions:
            rid = r.get("id", "")
            if rid:
                self.region_map[rid] = r
                if r.get("isDeathZone"):
                    self._dz_ids.add(rid)

    @property
    def dz_region_ids(self) -> set[str]:
        """Set of region IDs currently in death zone."""
        return self._dz_ids if hasattr(self, "_dz_ids") else set()

    def get_region_name(self, region_id: str) -> str:
        """Get human-readable region name."""
        r = self.region_map.get(region_id, {})
        return r.get("name", region_id)

    def find_high_value_weapons(self, avoid_dz: bool = True) -> list[dict]:
        """Find all weapons on the ground, sorted by priority."""
        dz = self.dz_region_ids if avoid_dz else set()
        weapons = []
        for item_data in self.all_items:
            rid = item_data.get("regionId", "")
            if avoid_dz and rid in dz:
                continue
            item = item_data.get("item", item_data)
            name = item.get("name", "")
            if name in WEAPON_PRIORITY and WEAPON_PRIORITY[name] > 0:
                weapons.append(
                    {
                        "item": item,
                        "region_id": rid,
                        "region_name": self.get_region_name(rid),
                        "priority": WEAPON_PRIORITY.get(name, 0),
                    }
                )
        weapons.sort(key=lambda x: x["priority"], reverse=True)
        return weapons

    def find_moltz_locations(self, avoid_dz: bool = True) -> list[dict]:
        """Find all Moltz/rewards on the ground (skip DZ regions)."""
        dz = self.dz_region_ids if avoid_dz else set()
        moltz = []
        for item_data in self.all_items:
            rid = item_data.get("regionId", "")
            if avoid_dz and rid in dz:
                continue
            item = item_data.get("item", item_data)
            if item.get("typeId") == "rewards" or item.get("category") == "currency":
                moltz.append(
                    {
                        "item": item,
                        "region_id": rid,
                        "region_name": self.get_region_name(rid),
                    }
                )
        return moltz

    def find_weak_enemies(
        self, exclude_ids: set = None, avoid_dz: bool = True
    ) -> list[dict]:
        """Find enemies with low HP (kill-steal targets). Skip enemies in DZ."""
        exclude = exclude_ids or set()
        dz = self.dz_region_ids if avoid_dz else set()
        weak = []
        for agent in self.all_agents:
            name = agent.get("name", "")
            aid = agent.get("id", "")
            rid = agent.get("regionId", "")
            if (
                agent.get("isAlive")
                and agent.get("hp", 100) < KILL_STEAL_HP
                and aid not in exclude
                and not IS_FRIENDLY_REGEX.match(name)
                and rid not in dz
            ):
                weak.append(
                    {
                        "id": aid,
                        "name": name,
                        "hp": agent.get("hp", 0),
                        "region_id": rid,
                        "region_name": self.get_region_name(rid),
                    }
                )
        weak.sort(key=lambda x: x["hp"])
        return weak

    def find_all_enemies(
        self, exclude_ids: set = None, avoid_dz: bool = True
    ) -> list[dict]:
        """Find all living enemies, excluding our bots. Skip enemies in DZ."""
        exclude = exclude_ids or set()
        dz = self.dz_region_ids if avoid_dz else set()
        enemies = []
        for agent in self.all_agents:
            rid = agent.get("regionId", "")
            if (
                agent.get("isAlive")
                and agent.get("id", "") not in exclude
                and not IS_FRIENDLY_REGEX.match(agent.get("name", ""))
                and rid not in dz
            ):
                enemies.append(
                    {
                        "id": agent.get("id", ""),
                        "name": agent.get("name", ""),
                        "hp": agent.get("hp", 100),
                        "region_id": rid,
                        "region_name": self.get_region_name(rid),
                    }
                )
        return enemies

    def get_region_enemy_count(self, region_id: str) -> int:
        """Count how many non-friendly living enemies are in a region."""
        if not self.available or not self.all_agents:
            return 0
        count = 0
        for a in self.all_agents:
            if (
                a.get("regionId") == region_id
                and a.get("isAlive")
                and not IS_FRIENDLY_REGEX.match(a.get("name", ""))
            ):
                count += 1
        return count

    def get_blob_threshold(self) -> int:
        """Dynamic blob threshold based on map density.

        Late game = more tolerant of crowds (DZ forces everyone together).
        We calculate this based on the number of safe regions left.
        """
        if not self.available or not self.all_regions:
            return 3

        dz_count = len(self.dz_region_ids)
        safe_regions = len(self.all_regions) - dz_count

        if safe_regions <= 20:
            return 999  # Moshpit mode: ignore crowds, map is too small
        if safe_regions <= 40:
            return 8

        # Fallback to alive agents if map is still large
        alive = sum(1 for a in self.all_agents if a.get("isAlive"))
        if alive <= 15:
            return 6
        if alive <= 30:
            return 4
        return 3

    def find_safest_region(self) -> str | None:
        """Find region with max BFS distance from all DZ edges (safe center compass)."""
        if not self.available or not self.all_regions:
            return None

        dz_ids = set()
        safe_ids = []
        for r in self.all_regions:
            rid = r.get("id", "")
            if r.get("isDeathZone"):
                dz_ids.add(rid)
            else:
                safe_ids.append(rid)

        if not safe_ids:
            return None
        if not dz_ids:
            return None  # no DZ yet — let terrain scoring decide

        # BFS simultaneously from ALL DZ edges to find max-distance region
        visited = {}
        queue = [(dz_id, 0) for dz_id in dz_ids]
        for dz_id in dz_ids:
            visited[dz_id] = 0

        while queue:
            current, dist = queue.pop(0)
            for neighbor in self.find_region_connections(current):
                if neighbor not in visited:
                    visited[neighbor] = dist + 1
                    queue.append((neighbor, dist + 1))

        best_id, best_dist = None, -1
        for rid in safe_ids:
            d = visited.get(rid, 0)
            if d > best_dist:
                best_dist = d
                best_id = rid

        return best_id

    def get_target_region(
        self,
        my_region_id: str,
        my_bot_ids: set = None,
        needs_weapon: bool = False,
        max_weapon_dist: int = 3,
        pending_dz_ids: set = None,
        weapon_range: int = 0,
    ) -> dict | None:
        """
        Targeted pathfinding: find best region to move toward.
        Uses score = value / distance to pick the most efficient target.
        All targets capped at max distance to prevent long wild goose chases.
        """
        exclude = my_bot_ids or set()
        MAX_HUNT_DIST = 4  # max distance for enemy hunting
        MAX_MOLTZ_DIST = 4  # 3 unarmed, 4 armed
        MAX_WEAPON_DIST = max_weapon_dist  # 3 unarmed, 4 armed

        candidates = []  # list of (score, target_dict)

        # Collect forbidden zones to prevent suicide runs into Death Zones
        forbidden_zones = self.dz_region_ids.copy()
        if pending_dz_ids:
            forbidden_zones.update(pending_dz_ids)

        # ── Weapons ──
        blob_threshold = self.get_blob_threshold()
        weapons = self.find_high_value_weapons()
        for w in weapons:
            if w["region_id"] == my_region_id or w["region_id"] in forbidden_zones:
                continue
            # Skip blob regions
            if self.get_region_enemy_count(w["region_id"]) >= blob_threshold:
                continue
            dist = self.calculate_distance(
                my_region_id,
                w["region_id"],
                max_dist=MAX_WEAPON_DIST,
                avoid_dz=True,
                pending_dz=pending_dz_ids,
            )
            if dist > MAX_WEAPON_DIST:
                continue
            # Unarmed: high urgency. Armed: lower (upgrade only)
            base_value = 50 if needs_weapon else 15
            score = base_value / max(dist, 1)
            candidates.append(
                (
                    score,
                    {
                        "region_id": w["region_id"],
                        "region_name": w["region_name"],
                        "reason": f'{"NEED WEAPON" if needs_weapon else "upgrade"} (dist {dist}): pickup "{w["item"].get("name", "")}"',
                    },
                )
            )

        # ── Weak enemies (kill-steal, HP < KILL_STEAL_HP) ──
        # Only prioritize kill-steals heavily if we are armed.
        # If unarmed, we handle desperate hunting separately below.
        if not needs_weapon:
            weak = self.find_weak_enemies(exclude_ids=exclude)
            for w in weak:
                if w["region_id"] == my_region_id or w["region_id"] in forbidden_zones:
                    continue
                # Skip blob regions
                if self.get_region_enemy_count(w["region_id"]) >= blob_threshold:
                    continue
                dist = self.calculate_distance(
                    my_region_id,
                    w["region_id"],
                    max_dist=MAX_HUNT_DIST,
                    avoid_dz=True,
                    pending_dz=pending_dz_ids,
                )
                if dist > MAX_HUNT_DIST:
                    continue

                # Anti-Suicide Ranged Logic
                if weapon_range > 0 and dist <= weapon_range:
                    continue

                # Lower HP = higher value (easier kill)
                base_value = max(1, 40 - w["hp"] * 0.5)  # HP:10 → 35, HP:39 → 20.5
                score = base_value / max(dist, 1)
                candidates.append(
                    (
                        score,
                        {
                            "region_id": w["region_id"],
                            "region_name": w["region_name"],
                            "reason": f'kill-steal "{w["name"]}" HP:{w["hp"]} (dist:{dist})',
                        },
                    )
                )

        # ── Moltz on ground ──
        moltz = self.find_moltz_locations()
        for m in moltz:
            if m["region_id"] == my_region_id or m["region_id"] in forbidden_zones:
                continue
            # Skip blob regions
            if self.get_region_enemy_count(m["region_id"]) >= blob_threshold:
                continue
            dist = self.calculate_distance(
                my_region_id,
                m["region_id"],
                max_dist=MAX_MOLTZ_DIST,
                avoid_dz=True,
                pending_dz=pending_dz_ids,
            )
            if dist > MAX_MOLTZ_DIST:
                continue
            score = 30 / max(
                dist, 1
            )  # priority 2: beats weapon upgrade, loses to kills
            candidates.append(
                (
                    score,
                    {
                        "region_id": m["region_id"],
                        "region_name": m["region_name"],
                        "reason": f"collect Moltz (dist:{dist})",
                    },
                )
            )

        # ── Hunt ANY enemy ──
        # If armed, this is our main hunt. If unarmed, this is a desperate hunt (low score)
        enemies = self.find_all_enemies(exclude_ids=exclude)
        enemies = [
            e
            for e in enemies
            if not IS_FRIENDLY_REGEX.match(e["name"])
            and e["region_id"] != my_region_id
            and e["region_id"] not in forbidden_zones
        ]
        for e in enemies:
            # Skip blob regions
            if self.get_region_enemy_count(e["region_id"]) >= blob_threshold:
                continue
            dist = self.calculate_distance(
                my_region_id,
                e["region_id"],
                max_dist=MAX_HUNT_DIST,
                avoid_dz=True,
                pending_dz=pending_dz_ids,
            )
            if dist > MAX_HUNT_DIST:
                continue

            # Anti-Suicide Ranged Logic:
            # If we have a ranged weapon (range >= 1), and the enemy is already within our shooting range,
            # DO NOT move closer to them! Doing so would just put us in melee danger.
            # We should stay put (which will result in a REST action later if we don't have enough EP to shoot).
            if not needs_weapon and weapon_range > 0 and dist <= weapon_range:
                continue

            # Prefer weaker enemies.
            # If unarmed, give it a very low score so it only triggers if no weapons/moltz/kills are around
            base_value = (
                5 if needs_weapon else max(1, 40 - e["hp"] * 0.1)
            )  # HP:50 → 15, HP:100 → 10
            score = base_value / max(dist, 1)
            reason = (
                f'desperate hunt "{e["name"]}" HP:{e["hp"]} (dist:{dist})'
                if needs_weapon
                else f'hunt "{e["name"]}" HP:{e["hp"]} (dist:{dist})'
            )
            candidates.append(
                (
                    score,
                    {
                        "region_id": e["region_id"],
                        "region_name": e["region_name"],
                        "reason": reason,
                    },
                )
            )

        # ── Safe center fallback (DZ-push: move toward safest region) ──
        # Score scales with DZ count: more DZ = more urgent = higher score
        safest = self.find_safest_region()  # returns None if no DZ
        if safest and safest != my_region_id:
            # Don't target safest if it's itself a blob
            if self.get_region_enemy_count(safest) < blob_threshold:
                dist = self.calculate_distance(
                    my_region_id,
                    safest,
                    max_dist=8,
                    avoid_dz=True,
                    pending_dz=pending_dz_ids,
                )
                if dist <= 8:
                    # Dynamic score: 0 DZ=5, 5 DZ=20, 20 DZ=65 (Capped at 15 to prevent overriding combat)
                    dz_count = sum(1 for r in self.all_regions if r.get("isDeathZone"))
                    center_score = min(15, (5 + dz_count * 3) / max(dist, 1))
                    candidates.append(
                        (
                            center_score,
                            {
                                "region_id": safest,
                                "region_name": self.get_region_name(safest),
                                "reason": f"safe zone center (dist {dist}, {dz_count} DZ active)",
                            },
                        )
                    )

        if not candidates:
            return None

        # Pick highest score
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def find_region_connections(self, region_id: str) -> list[str]:
        """Get connections for a region from the full map."""
        if self.region_map and region_id in self.region_map:
            return self.region_map[region_id].get("connections", [])
        return []

    def calculate_distance(
        self,
        from_region: str,
        to_region: str,
        max_dist: int = 3,
        avoid_dz: bool = False,
        pending_dz: set = None,
        avoid_blobs: bool = False,
    ) -> int:
        """
        Calculate hop distance between regions using BFS.
        If avoid_dz, route around death zone regions.
        If avoid_blobs, route around regions with >= blob_threshold enemies.
        Returns distance (int) or 999 if unreachable/unknown.
        """
        if from_region == to_region:
            return 0
        if not self.available or not self.region_map:
            return 999

        dz = set()
        if avoid_dz:
            dz = self.dz_region_ids.copy()
            if pending_dz:
                dz.update(pending_dz)

        blob_limit = self.get_blob_threshold() if avoid_blobs else 999

        # BFS
        queue = [(from_region, 0)]
        visited = {from_region}

        while queue:
            current, dist = queue.pop(0)
            if dist >= max_dist:
                continue

            for neighbor in self.find_region_connections(current):
                if neighbor == to_region:
                    return dist + 1
                if neighbor not in visited and neighbor not in dz:
                    # Treat Moshpits as walls if avoid_blobs is active
                    if (
                        avoid_blobs
                        and self.get_region_enemy_count(neighbor) >= blob_limit
                    ):
                        continue

                    visited.add(neighbor)
                    queue.append((neighbor, dist + 1))

        return 999

    def find_path_next_step(
        self,
        from_region: str,
        to_region: str,
        max_depth: int = 6,
        avoid_dz: bool = False,
        pending_dz: set = None,
        avoid_blobs: bool = False,
    ) -> str | None:
        """
        BFS pathfinding: find next region to move to on the path from -> to.
        If avoid_dz, skip DZ regions in routing (but allow to_region even if DZ).
        If avoid_blobs, skip regions with >= blob_threshold enemies.
        Returns the next region_id to move to, or None if unreachable.
        """
        if from_region == to_region:
            return None

        dz = set()
        if avoid_dz:
            dz = self.dz_region_ids.copy()
            if pending_dz:
                dz.update(pending_dz)

        blob_limit = self.get_blob_threshold() if avoid_blobs else 999

        # BFS
        visited = {from_region}
        queue = [(from_region, [])]  # (current, path)

        while queue:
            current, path = queue.pop(0)
            connections = self.find_region_connections(current)

            for next_id in connections:
                if next_id == to_region:
                    # Found! Return first step
                    full_path = path + [next_id]
                    return full_path[0] if full_path else None

                if (
                    next_id not in visited
                    and len(path) < max_depth
                    and next_id not in dz
                ):
                    # Treat Moshpits as walls if avoid_blobs is active
                    if (
                        avoid_blobs
                        and self.get_region_enemy_count(next_id) >= blob_limit
                    ):
                        continue

                    visited.add(next_id)
                    queue.append((next_id, path + [next_id]))

        return None
