"""
api_client.py — HTTP wrapper for all Molty Royale API calls.
Retry logic, rate limiting, robust error handling.
"""

import asyncio
import aiohttp
from src.config import BASE_URL, CDN_URL, API_TIMEOUT, API_RETRIES


class ApiClient:
    """Async HTTP client for Molty Royale API."""

    def __init__(self, api_key: str = "", bot_index: int = 0):
        self.api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # We rely on the natural efficiency of aiohttp's default connection pooling.
            # No need for force_close since our Orchestrator's Wave Registration handles the limits natively.
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                connector=aiohttp.TCPConnector(keepalive_timeout=30),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _headers(self, use_key: str = "") -> dict:
        """Build headers. use_key overrides default api_key."""
        key = use_key or self.api_key
        h = {"Content-Type": "application/json"}
        if key:
            h["X-API-Key"] = key
        return h

    async def _request(
        self,
        method: str,
        path: str,
        json: dict = None,
        use_key: str = "",
        retries: int = API_RETRIES,
    ) -> dict:
        """Make HTTP request with retry logic and robust error handling."""
        # ─── Route Segregation (CDN vs API) ───
        # GET requests (State/GodMode) use the fast CDN Edge Cache
        # POST/PUT requests (Action/Register) use the direct API Gateway
        base_domain = CDN_URL if method.upper() == "GET" else BASE_URL
        url = f"{base_domain}{path}"

        session = await self._get_session()

        last_error = ""
        for attempt in range(retries):
            try:
                async with session.request(
                    method, url, json=json, headers=self._headers(use_key)
                ) as resp:
                    # Try to parse JSON response
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        # Non-JSON response — read raw text
                        text = await resp.text()

                        if resp.status == 429:
                            msg = "Rate Limited / Too Many Requests"
                        elif resp.status >= 500:
                            msg = f"Server Error (HTTP {resp.status})"
                        else:
                            msg = f"Non-JSON response (HTTP {resp.status}): {text[:50].strip()}..."

                        return {
                            "success": False,
                            "error": {
                                "message": msg,
                                "code": f"HTTP_{resp.status}",
                            },
                        }

                    # If server returned an error structure, return as-is
                    if isinstance(data, dict):
                        # Force retry on 5xx Server Errors
                        if resp.status >= 500:
                            last_error = f"HTTP {resp.status} - Server Error"
                            if attempt < retries - 1:
                                await asyncio.sleep(min(2**attempt, 2))
                                continue
                            return {
                                "success": False,
                                "error": {
                                    "message": f"Server Error (HTTP {resp.status})",
                                    "code": f"HTTP_{resp.status}",
                                },
                            }

                        # Never retry rate limits — retrying makes it worse
                        err_code = (
                            data.get("error", {}).get("code", "")
                            if data.get("success") is False
                            else ""
                        )
                        if err_code == "RATE_LIMIT_EXCEEDED":
                            return data
                        return data

                    # If data is a list (e.g. find_games returns array)
                    if isinstance(data, list):
                        return {"success": True, "data": data}

                    return {
                        "success": False,
                        "error": {
                            "message": f"Unexpected response: {data}",
                            "code": "PARSE_ERROR",
                        },
                    }

            except asyncio.TimeoutError:
                last_error = f"Timeout after {API_TIMEOUT}s"
                if attempt < retries - 1:
                    await asyncio.sleep(min(2**attempt, 2))

            except aiohttp.ClientError as e:
                last_error = f"{type(e).__name__}: {e}"
                if attempt < retries - 1:
                    await asyncio.sleep(min(2**attempt, 2))

            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                if attempt < retries - 1:
                    await asyncio.sleep(min(2**attempt, 2))

        return {
            "success": False,
            "error": {"message": last_error, "code": "NETWORK_ERROR"},
        }

    # ─── Game APIs ───────────────────────────────────

    async def find_games(self, status: str = "waiting") -> dict:
        """GET /games?status=waiting"""
        return await self._request("GET", f"/games?status={status}")

    async def create_game(self, host_name: str = "BotArena") -> dict:
        """POST /games"""
        return await self._request("POST", "/games", json={"hostName": host_name})

    async def get_game_info(self, game_id: str) -> dict:
        """GET /games/{gameId}"""
        return await self._request("GET", f"/games/{game_id}")

    async def register_agent(self, game_id: str, name: str, api_key: str = "") -> dict:
        """POST /games/{gameId}/agents/register"""
        return await self._request(
            "POST",
            f"/games/{game_id}/agents/register",
            json={"name": name},
            use_key=api_key or self.api_key,
        )

    # ─── Agent APIs ──────────────────────────────────

    async def get_state(
        self, game_id: str, agent_id: str, retries: int = API_RETRIES
    ) -> dict:
        """GET /games/{gameId}/agents/{agentId}/state — normal state."""
        return await self._request(
            "GET", f"/games/{game_id}/agents/{agent_id}/state", retries=retries
        )

    async def do_action(
        self,
        game_id: str,
        agent_id: str,
        action: dict,
        thought: dict = None,
        use_key: str = "",
        retries: int = API_RETRIES,
    ) -> dict:
        """POST /games/{gameId}/agents/{agentId}/action"""
        body = {"action": action}
        if thought:
            body["thought"] = thought
        return await self._request(
            "POST",
            f"/games/{game_id}/agents/{agent_id}/action",
            json=body,
            use_key=use_key,
            retries=retries,
        )

    # ─── God Mode ────────────────────────────────────

    async def get_full_state(self, game_id: str) -> dict:
        """GET /games/{gameId}/state — spectator full map (God Mode)."""
        return await self._request("GET", f"/games/{game_id}/state")

    async def get_ws_endpoint(self, game_id: str) -> dict:
        """GET /games/{gameId}/ws-endpoint — Get WebSocket URL for God Mode V2"""
        return await self._request("GET", f"/games/{game_id}/ws-endpoint")

    # ─── Account APIs ────────────────────────────────

    async def get_account(self, api_key: str = "") -> dict:
        """GET /accounts/me"""
        return await self._request(
            "GET", "/accounts/me", use_key=api_key or self.api_key
        )
