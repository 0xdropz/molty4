# Molty Royale Bot Swarm (v2moltroyale)

## Goal

Build an unstoppable Molty Royale bot swarm that dominates the game using:
1. **WebSocket God Mode V2** - Real-time map/revealed enemy data bypassing the blocked API spectator endpoint
2. **Barbarian Strategy** - Aggressive hunting of Killers (>=2 kills), Sultans (>=30 Moltz), and converging at the Safest Region
3. **Doomsday Protocol** - Surviving Death Zone traps by spamming heals/loot instead of futile fleeing
4. **Multi-Railway Deployment** - Running 50 accounts across multiple Railway containers

## Instructions

### Critical Rules
- Bot must work WITHOUT relying on the blocked spectator API (`GET /games/{gameId}/state`)
- Use WebSocket connection to `wss://module-X-production.up.railway.app/ws?gameId={id}` instead
- Respect friendly regex `IS_FRIENDLY_REGEX` for Sultan/Killer hunting
- Priority order: P0 (Doomsday) > P1 (Pending DZ) > P2 (Critical HP) > P3 (Killer) > P4 (Sultan) > P5 (Enemy) > P6 (Low HP) > P7 (Monster) > P8-P14 (Loot/Safe Region/Rest)

### Implementation Specs
- **God Mode**: Use WebSocket to get full state (`state.agents`, `state.regions`, etc.)
- **Navigation**: BFS pathfinding to Safe Center, avoid Death Zones
- **Targeting**: Prioritize Killer > Sultan > Low HP Enemy
- **Doomsday**: If trapped in DZ with no exit → Spam Heal > Spam Drink > Attack > Rest

## Discoveries

1. **Spectator API Blocked**: `GET /games/{gameId}/state` returns `INVALID_ACTION` for running games (only finished games work)
2. **WebSocket Found**: Frontend JS revealed endpoint: `GET /games/{gameId}/ws-endpoint` returns `wss://module-X-production.up.railway.app/ws?gameId={id}`
3. **Data Leak**: WebSocket delivers full 472KB JSON with all agents, regions, items - including enemy inventory and positions
4. **Railway Sniper**: Can detect new games FASTER than API polling by monitoring Smart Contract `ArenaFree` transactions
5. **Map Procedural**: Maps are randomly generated (121-169 regions) - no fixed templates
6. **Sultan/Killer Logic**: Must use `IS_FRIENDLY_REGEX` to exclude friendly bots from being hunted

## Accomplished

### Completed
- **WebSocket God Mode V2 Infrastructure**
  - `src/god_mode_cache.py` - Background WebSocket listener per game_id with auto-cleanup
  - `src/god_mode.py` - Analytics: `find_safest_region()`, `find_sultan()`, `find_killer()`, BFS pathfinding
  - `src/api_client.py` - Added `get_ws_endpoint()` method
  - `requirements.txt` - Added `websockets==12.0`

- **Strategy Rewrite (Priority System)**
  - `src/strategy.py` - Full V6 rewrite with Doomsday, Killer, Sultan, Safe Region logic
  - `src/combat.py` - Priority target handling, distance calculation using God Mode
  - `src/movement.py` - `move_toward_target()` with God Mode pathfinding fallback

- **Infrastructure Cleanup**
  - Removed God Mode files (`god_mode.py`, `god_mode_cache.py` were recreated)
  - Removed `backup/`, `temp/` folders
  - Fixed console logging (removed HP/EP/$ spam, replaced with Game Number)
  - Fixed infinite recovery loop (removed `_recover_existing_game`, added silent polling)
  - Added `aiohttp` to requirements.txt

- **Joiner Enhancements**
  - Implemented Web3 Sniper (monitor Smart Contract for faster game detection)
  - Added robust features: RPC rate-limit protection, UUID extraction fix, memory cleanup

### Pending
- None - All requested features implemented