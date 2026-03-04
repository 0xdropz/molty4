"""
state_manager.py — Parse & cache game state into clean dataclasses.
Provides human-readable accessors for all game data.
"""

from dataclasses import dataclass, field


@dataclass
class WeaponInfo:
    id: str = ""
    name: str = "Fist"
    atk_bonus: int = 0
    range: int = 0

    @classmethod
    def from_dict(cls, d: dict | None) -> "WeaponInfo":
        if not d:
            return cls()
        return cls(
            id=d.get("id", ""),
            name=d.get("name", "Fist"),
            atk_bonus=d.get("atkBonus", 0),
            range=d.get("range", 0),
        )


@dataclass
class ItemInfo:
    id: str
    name: str
    category: str  # weapon, recovery, utility, currency
    atk_bonus: int = 0
    range: int = 0
    type_id: str = ""
    amount: int = 1  # stack count (e.g. $Moltz x5)

    @classmethod
    def from_dict(cls, d: dict) -> "ItemInfo":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            category=d.get("category", ""),
            atk_bonus=d.get("atkBonus", 0),
            range=d.get("range", 0),
            type_id=d.get("typeId", ""),
            amount=d.get("amount", d.get("quantity", d.get("count", 1))),
        )


@dataclass
class EnemyInfo:
    id: str
    name: str
    hp: int
    max_hp: int
    atk: int
    defense: int
    region_id: str
    weapon: WeaponInfo = field(default_factory=WeaponInfo)
    is_alive: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "EnemyInfo":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            hp=d.get("hp", 0),
            max_hp=d.get("maxHp", 100),
            atk=d.get("atk", 10),
            defense=d.get("def", 5),
            region_id=d.get("regionId", ""),
            weapon=WeaponInfo.from_dict(d.get("equippedWeapon")),
            is_alive=d.get("isAlive", True),
        )


@dataclass
class MonsterInfo:
    id: str
    name: str
    hp: int
    atk: int
    defense: int
    region_id: str

    @classmethod
    def from_dict(cls, d: dict) -> "MonsterInfo":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            hp=d.get("hp", 0),
            atk=d.get("atk", 0),
            defense=d.get("def", 0),
            region_id=d.get("regionId", ""),
        )


@dataclass
class RegionItem:
    region_id: str
    item: ItemInfo

    @classmethod
    def from_dict(cls, d: dict) -> "RegionItem":
        return cls(
            region_id=d.get("regionId", ""),
            item=ItemInfo.from_dict(d.get("item", {})),
        )


@dataclass
class Interactable:
    id: str
    type: str
    is_used: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "Interactable":
        return cls(
            id=d.get("id", ""),
            type=d.get("type", ""),
            is_used=d.get("isUsed", False),
        )


@dataclass
class RegionInfo:
    id: str
    name: str
    terrain: str = "plains"
    weather: str = "clear"
    vision_modifier: int = 0
    is_death_zone: bool = False
    connections: list[str] = field(default_factory=list)
    interactables: list[Interactable] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | str) -> "RegionInfo":
        """Can be a full region dict or just a region_id string."""
        if isinstance(d, str):
            return cls(id=d, name=d)
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            terrain=d.get("terrain", "plains"),
            weather=d.get("weather", "clear"),
            vision_modifier=d.get("visionModifier", 0),
            is_death_zone=d.get("isDeathZone", False),
            connections=d.get("connections", []),
            interactables=[
                Interactable.from_dict(i) for i in d.get("interactables", [])
            ],
        )


@dataclass
class SelfInfo:
    id: str
    name: str
    hp: int
    max_hp: int
    ep: int
    max_ep: int
    atk: int
    defense: int
    vision: int
    region_id: str
    inventory: list[ItemInfo]
    weapon: WeaponInfo
    is_alive: bool
    kills: int

    @classmethod
    def from_dict(cls, d: dict) -> "SelfInfo":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            hp=d.get("hp", 100),
            max_hp=d.get("maxHp", 100),
            ep=d.get("ep", 10),
            max_ep=d.get("maxEp", 10),
            atk=d.get("atk", 10),
            defense=d.get("def", 5),
            vision=d.get("vision", 1),
            region_id=d.get("regionId", ""),
            inventory=[ItemInfo.from_dict(i) for i in d.get("inventory", [])],
            weapon=WeaponInfo.from_dict(d.get("equippedWeapon")),
            is_alive=d.get("isAlive", True),
            kills=d.get("kills", 0),
        )


@dataclass
class GameState:
    """Parsed game state from API response."""

    self_info: SelfInfo | None = None
    current_region: RegionInfo | None = None
    connected_regions: list[RegionInfo] = field(default_factory=list)
    visible_enemies: list[EnemyInfo] = field(default_factory=list)
    visible_monsters: list[MonsterInfo] = field(default_factory=list)
    visible_items: list[RegionItem] = field(default_factory=list)
    visible_regions: list[RegionInfo] = field(default_factory=list)
    raw_visible_agents: list[dict] = field(
        default_factory=list
    )  # raw API dicts (has inventory)
    pending_deathzones: list[dict] = field(default_factory=list)
    game_status: str = "waiting"  # waiting, running, finished
    result: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict) -> "GameState":
        """
        Parse raw API response into GameState.
        Handles two formats:
          - Wrapped:   {success: true, data: {self, currentRegion, ...}}
          - Unwrapped: {self, currentRegion, ...}       (state endpoint)
          - Error:     {success: false, error: {...}}
        """
        if not data:
            return cls(raw=data or {})

        # Check for error response
        if data.get("success") is False:
            return cls(raw=data)

        # Determine the actual data dict
        # If response has "data" wrapper, use that; otherwise data IS the state
        if "data" in data and isinstance(data["data"], dict):
            d = data["data"]
        elif "self" in data:
            # Unwrapped — the state endpoint returns {self, currentRegion, ...} directly
            d = data
        else:
            # Unknown format: try to use as-is
            d = data

        # Parse connected regions (can be full objects or just strings)
        connected = []
        for r in d.get("connectedRegions", []):
            connected.append(RegionInfo.from_dict(r))

        return cls(
            self_info=SelfInfo.from_dict(d.get("self", {})),
            current_region=RegionInfo.from_dict(d.get("currentRegion", {})),
            connected_regions=connected,
            visible_enemies=[
                EnemyInfo.from_dict(a) for a in d.get("visibleAgents", [])
            ],
            visible_monsters=[
                MonsterInfo.from_dict(m) for m in d.get("visibleMonsters", [])
            ],
            visible_items=[RegionItem.from_dict(i) for i in d.get("visibleItems", [])],
            visible_regions=[
                RegionInfo.from_dict(r) for r in d.get("visibleRegions", [])
            ],
            raw_visible_agents=d.get("visibleAgents", []),
            pending_deathzones=d.get("pendingDeathzones", []),
            game_status=d.get("gameStatus", "waiting"),
            result=d.get("result") or {},
            raw=data,
        )

    # ─── Convenience accessors ───────────────────────

    @property
    def is_alive(self) -> bool:
        return self.self_info.is_alive if self.self_info else False

    @property
    def is_running(self) -> bool:
        return self.game_status == "running"

    @property
    def is_finished(self) -> bool:
        return self.game_status == "finished"

    @property
    def hp(self) -> int:
        return self.self_info.hp if self.self_info else 0

    @property
    def ep(self) -> int:
        return self.self_info.ep if self.self_info else 0

    @property
    def weapon(self) -> WeaponInfo:
        return self.self_info.weapon if self.self_info else WeaponInfo()

    @property
    def has_weapon(self) -> bool:
        return self.weapon.atk_bonus > 0

    @property
    def inventory(self) -> list[ItemInfo]:
        return self.self_info.inventory if self.self_info else []

    @property
    def bag_count(self) -> int:
        return len(self.inventory)

    @property
    def moltz_count(self) -> int:
        """Count Moltz in inventory (sum of amounts for stacked items)."""
        total = 0
        for i in self.inventory:
            if i.type_id in ("rewards", "reward1", "moltz", "currency"):
                total += i.amount
            elif i.category in ("currency", "rewards"):
                total += i.amount
            elif "moltz" in i.name.lower():
                total += i.amount
        return total

    @property
    def region_name(self) -> str:
        return self.current_region.name if self.current_region else "Unknown"

    @property
    def region_id(self) -> str:
        return self.current_region.id if self.current_region else ""

    @property
    def terrain(self) -> str:
        return self.current_region.terrain if self.current_region else "unknown"

    @property
    def weather(self) -> str:
        return self.current_region.weather if self.current_region else "unknown"

    @property
    def is_death_zone(self) -> bool:
        return self.current_region.is_death_zone if self.current_region else False

    @property
    def kills(self) -> int:
        return self.self_info.kills if self.self_info else 0

    def enemies_in_region(self) -> list[EnemyInfo]:
        """Enemies in the same region as the bot."""
        rid = self.region_id
        return [e for e in self.visible_enemies if e.region_id == rid and e.is_alive]

    def monsters_in_region(self) -> list[MonsterInfo]:
        """Monsters in the same region."""
        rid = self.region_id
        return [m for m in self.visible_monsters if m.region_id == rid]

    def items_in_region(self) -> list[RegionItem]:
        """Items on the ground in the same region."""
        rid = self.region_id
        return [i for i in self.visible_items if i.region_id == rid]

    def safe_connections(self) -> list[RegionInfo]:
        """Connected regions that are NOT death zones."""
        return [r for r in self.connected_regions if not r.is_death_zone]

    def usable_facilities(self) -> list[Interactable]:
        """Facilities in current region that haven't been used yet."""
        if not self.current_region:
            return []
        return [f for f in self.current_region.interactables if not f.is_used]

    def weapons_in_inventory(self) -> list[ItemInfo]:
        """Weapons currently in inventory."""
        return [i for i in self.inventory if i.category == "weapon"]

    def recovery_items(self) -> list[ItemInfo]:
        """Recovery items in inventory."""
        return [i for i in self.inventory if i.category == "recovery"]
