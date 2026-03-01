"""
god_mode_cache.py — Shared god mode state cache.
Only ONE fetch per game per turn interval, shared across all bots.
Reduces redundant god mode API calls to 1.
"""

import asyncio
import time
from src.api_client import ApiClient


class GodModeCache:
    """
    Shared cache for god mode (full game state).
    First bot to request triggers the fetch; others reuse the cached result.

    Two TTLs:
    - Success: cached for `ttl` seconds (30s) — fresh data each turn
    - Failure: cached for `fail_ttl` seconds (5s) — prevents lock serialization
    """

    def __init__(self, ttl: float = 30.0, fail_ttl: float = 5.0):
        self.ttl = ttl
        self.fail_ttl = fail_ttl
        self._cache: dict[str, dict] = {}       # game_id -> response
        self._timestamps: dict[str, float] = {}  # game_id -> fetch time
        self._valid: dict[str, bool] = {}        # game_id -> was result valid?
        self._locks: dict[str, asyncio.Lock] = {}  # game_id -> lock
        self._api = ApiClient("")  # keyless — god mode is public

    def _get_lock(self, game_id: str) -> asyncio.Lock:
        if game_id not in self._locks:
            self._locks[game_id] = asyncio.Lock()
        return self._locks[game_id]

    def _is_fresh(self, game_id: str) -> bool:
        """Check if cached result is still within its TTL."""
        if game_id not in self._cache:
            return False
        age = time.monotonic() - self._timestamps.get(game_id, 0)
        # Use shorter TTL for failures so we retry sooner
        max_age = self.ttl if self._valid.get(game_id, False) else self.fail_ttl
        return age < max_age

    async def get_full_state(self, game_id: str) -> dict:
        """
        Get god mode state. Uses cache if fresh, otherwise fetches once.
        Multiple concurrent callers for the same game_id will wait on the lock
        and all get the same result (even if it's an error).
        """
        # Fast path: cache hit
        if self._is_fresh(game_id):
            return self._cache[game_id]

        # Slow path: fetch with lock (only one fetch at a time per game)
        lock = self._get_lock(game_id)
        async with lock:
            # Double-check after acquiring lock
            if self._is_fresh(game_id):
                return self._cache[game_id]

            # Actually fetch
            result = await self._api.get_full_state(game_id)
            valid = self._is_valid(result)

            # Always cache — success for 30s, failure for 5s
            self._cache[game_id] = result
            self._timestamps[game_id] = time.monotonic()
            self._valid[game_id] = valid

            return result

    @staticmethod
    def _is_valid(result: dict) -> bool:
        """Check if god mode response has actual data (not an error)."""
        if not isinstance(result, dict):
            return False
        if result.get("success") is False:
            return False
        if "data" in result:
            return isinstance(result["data"], dict)
        return "agents" in result or "regions" in result

    def invalidate(self, game_id: str):
        """Force next call to re-fetch."""
        self._cache.pop(game_id, None)
        self._timestamps.pop(game_id, None)
        self._valid.pop(game_id, None)

    async def close(self):
        await self._api.close()
