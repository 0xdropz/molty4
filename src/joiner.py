"""
joiner.py — Persistent dynamic mass-join for Molty Royale.

Max 5 accounts per room per IP (TOO_MANY_AGENTS_PER_IP limit).
Only 1 free room exists at a time — after filling 5 slots, waits for a NEW
room to appear before sending the next batch.

Runs persistently (never stops). Handles:
  - Initial join for all accounts
  - Reincarnation: accounts re-added via rejoin_queue after game ends

Usage (standalone test):
    python -m src.joiner

Programmatic:
    from src.joiner import run_joiner
    await run_joiner(accounts, on_ready_callback, rejoin_queue)
"""

import asyncio
import re
import json
import os
import time
from collections import deque
from typing import Callable, Awaitable

import aiohttp

from src.config import CDN_URL, BASE_URL, API_TIMEOUT

# Max agents per IP per game (hard API limit)
MAX_PER_ROOM = 5
# Poll interval for Web2 API
POLL_INTERVAL = 0.5
# Poll interval for Web3 RPC (must be slower to avoid IP Ban, block time is ~3s anyway)
WEB3_POLL_INTERVAL = 2.0
# Log "waiting for room" at most once every N seconds to avoid spam
LOG_THROTTLE = 10

# ── Web3 Sniper Config ────────────────────────────────────────────────────────
RPC_URL = "https://mainnet.crosstoken.io:22001"
ARENA_FREE = "0xAbC98bBe54e5bc495D97E6A9c51eEf14fd34e77D"
EVENT_TOPIC = "0xd5195d721a86abeca98ed69ce3a94d30db00e153c904de359625e0dc8d2c77ae"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_game_id_from_msg(msg: str) -> str:
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", msg)
    return m.group(0) if m else ""


def _log(msg: str):
    print(f"[joiner] {msg}", flush=True)


def _log_account(name: str, status: str, detail: str = ""):
    print(f"  [{name:<16}] {status:<22} {detail}", flush=True)


# ── HTTP helpers ──────────────────────────────────────────────────────────────


async def _get(session: aiohttp.ClientSession, path: str) -> dict:
    url = f"{CDN_URL}{path}"
    try:
        async with session.get(url) as resp:
            try:
                return await resp.json(content_type=None)
            except Exception:
                return {
                    "success": False,
                    "error": {
                        "message": await resp.text(),
                        "code": f"HTTP_{resp.status}",
                    },
                }
    except Exception as e:
        return {"success": False, "error": {"message": str(e), "code": "NETWORK_ERROR"}}


async def _post(
    session: aiohttp.ClientSession, path: str, body: dict, api_key: str
) -> dict:
    url = f"{BASE_URL}{path}"
    headers = {"Content-Type": "application/json", "X-API-Key": api_key}
    try:
        async with session.post(url, json=body, headers=headers) as resp:
            try:
                return await resp.json(content_type=None)
            except Exception:
                return {
                    "success": False,
                    "error": {
                        "message": await resp.text(),
                        "code": f"HTTP_{resp.status}",
                    },
                }
    except Exception as e:
        return {"success": False, "error": {"message": str(e), "code": "NETWORK_ERROR"}}


# ── Core ──────────────────────────────────────────────────────────────────────


async def _fetch_waiting_rooms(session: aiohttp.ClientSession) -> list[dict]:
    resp = await _get(session, "/games?status=waiting")
    if not resp.get("success"):
        return []
    games = resp.get("data", [])
    return [
        g
        for g in games
        if g.get("entryType") == "free"
        and g.get("agentCount", 0) < g.get("maxAgents", 100)
    ]


async def _get_latest_block(session: aiohttp.ClientSession) -> int:
    """Ambil block terbaru dari RPC untuk Sniper."""
    payload = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
    try:
        async with session.post(RPC_URL, json=payload, ssl=False) as resp:
            res = await resp.json()
            return int(res.get("result", "0"), 16)
    except Exception:
        return 0


async def _fetch_web3_rooms(
    session: aiohttp.ClientSession, from_block: int, to_block: int
) -> list[dict]:
    """
    Snipe Game ID dari Smart Contract ArenaFree sebelum API/CDN menampilkannya.
    Mengekstrak Topic 1 (UUID) dari log event transaksi.
    """
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getLogs",
        "params": [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": ARENA_FREE,
                "topics": [EVENT_TOPIC],
            }
        ],
        "id": 1,
    }
    try:
        async with session.post(RPC_URL, json=payload, ssl=False) as resp:
            res = await resp.json()
            logs = res.get("result", [])
            games = []
            if not logs:
                return []

            for log in logs:
                topics = log.get("topics", [])
                # Topic 1 berisi GameID dalam format Hex, selalu 64 chars (32 bytes EVM word)
                if len(topics) >= 2:
                    # EVM padding ditaruh di kiri. Kita ambil 32 karakter (16 bytes) paling kanan.
                    raw_hex = topics[1].replace("0x", "")[-32:]
                    # Pastikan panjangnya valid (32 chars) sebelum diformat
                    if len(raw_hex) == 32:
                        game_id = f"{raw_hex[:8]}-{raw_hex[8:12]}-{raw_hex[12:16]}-{raw_hex[16:20]}-{raw_hex[20:32]}"
                        games.append(
                            {
                                "id": game_id,
                                "name": f"[Web3 Snipe {game_id[:4]}]",
                                "agentCount": 0,  # Asumsikan masih kosong karena sangat baru
                                "maxAgents": 100,
                                "entryType": "free",
                            }
                        )
            return games
    except Exception:
        return []


async def _register_one(
    session: aiohttp.ClientSession,
    account: dict,
    game_id: str,
) -> tuple[dict, str, str, str]:
    """
    Returns (account, game_id, agent_id, status)
    status: "ok" | "already" | "full" | "retry"
    """
    resp = await _post(
        session,
        f"/games/{game_id}/agents/register",
        {"name": account["name"]},
        account["apiKey"],
    )

    if resp.get("success"):
        data = resp.get("data", {})
        return account, game_id, data.get("id", ""), "ok"

    error = resp.get("error", {})
    code = error.get("code", "UNKNOWN")
    msg = error.get("message", "")

    if code in ("ACCOUNT_ALREADY_IN_GAME", "ONE_AGENT_PER_API_KEY"):
        extracted_gid = _extract_game_id_from_msg(msg)
        return account, extracted_gid or game_id, "", "already"

    if code in (
        "MAX_AGENTS_REACHED",
        "GAME_ALREADY_STARTED",
        "GAME_NOT_FOUND",
        "TOO_MANY_AGENTS_PER_IP",
    ):
        return account, game_id, "", "full"

    return account, game_id, "", "retry"


async def _join_batch(
    session: aiohttp.ClientSession,
    batch: list[dict],
    game_id: str,
    on_ready: Callable[[dict, str, str], Awaitable[None]],
    verbose: bool = False,
) -> list[dict]:
    """
    Register batch in parallel. Calls on_ready(account, game_id, agent_id)
    immediately for each success. Returns list of accounts to retry.
    """
    tasks = [_register_one(session, acc, game_id) for acc in batch]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    retry = []
    for res in results:
        if isinstance(res, Exception):
            if verbose:
                _log(f"Unexpected error: {res}")
            continue

        account, gid, agent_id, status = res
        name = account["name"]

        if status == "ok":
            if verbose:
                _log_account(name, "OK", f"game:{gid[:8]}.. agent:{agent_id[:12]}..")
            await on_ready(account, gid, agent_id)

        elif status == "already":
            if verbose:
                _log_account(name, "ALREADY_IN_GAME", "skip — bot will recover")
            await on_ready(account, gid, "")

        elif status == "full":
            if verbose:
                _log_account(name, "ROOM_FULL", "back to queue")
            retry.append(account)

        elif status == "retry":
            if verbose:
                _log_account(name, "ERROR/TIMEOUT", "back to queue")
            retry.append(account)

    return retry


async def run_joiner(
    accounts: list[dict],
    on_ready: Callable[[dict, str, str], Awaitable[None]],
    rejoin_queue: asyncio.Queue,
    verbose: bool = False,
):
    """
    Persistent joiner loop. Never stops — runs until cancelled.

    Handles:
      - Initial join for all accounts in `accounts`
      - Reincarnation: accounts re-added via `rejoin_queue` after game ends

    Fires on_ready(account, game_id, agent_id) as soon as each account is placed.
    Polls every POLL_INTERVAL sec when no new room is available.
    Log spam throttled via LOG_THROTTLE.
    """
    queue: deque[dict] = deque(accounts)
    filled_rooms: set[str] = set()
    last_log_time = 0.0
    last_scanned_block = 0
    last_web3_time = 0.0

    if verbose:
        _log(
            f"Starting persistent joiner (Web2 & Web3 Sniper) for {len(accounts)} accounts (max {MAX_PER_ROOM}/room)..."
        )

    connector = aiohttp.TCPConnector(limit=100, keepalive_timeout=30)
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        while True:
            now = time.monotonic()

            # ── Garbage Collection: Prevent memory leak if running for months ──
            if len(filled_rooms) > 1000:
                # Sisakan 100 room terakhir (anggap set is un-ordered, clear setengahnya aja)
                filled_rooms.clear()

            # ── Drain rejoin_queue: accounts that finished a game and need a new one ──
            while not rejoin_queue.empty():
                try:
                    acc = rejoin_queue.get_nowait()
                    queue.append(acc)
                    if verbose:
                        _log(f"Requeue: {acc['name']} needs a new game")
                except asyncio.QueueEmpty:
                    break

            if not queue:
                # Nothing to do — sleep briefly and check rejoin_queue again
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # ── Web3 Sniper Polling (Throttled to WEB3_POLL_INTERVAL) ──
            web3_rooms = []
            if now - last_web3_time >= WEB3_POLL_INTERVAL:
                last_web3_time = now
                current_block = await _get_latest_block(session)
                if current_block > 0:
                    if last_scanned_block == 0:
                        last_scanned_block = current_block  # start fresh
                    elif current_block >= last_scanned_block:
                        web3_rooms = await _fetch_web3_rooms(
                            session, last_scanned_block, current_block
                        )
                        if web3_rooms and verbose:
                            _log(
                                f"🔥 BINGO! Web3 Sniper mendeteksi {len(web3_rooms)} game baru di blockchain!"
                            )
                        last_scanned_block = current_block + 1

            # ── Web2 API Polling (CDN/Fallback) ──
            web2_rooms = await _fetch_waiting_rooms(session)

            # Gabungkan dan hilangkan duplikasi (ID sama)
            all_rooms_map = {}
            for r in web3_rooms + web2_rooms:
                all_rooms_map[r["id"]] = r

            fresh_rooms = [
                r for r in all_rooms_map.values() if r["id"] not in filled_rooms
            ]

            if not fresh_rooms:
                if verbose:
                    now = time.monotonic()
                    if now - last_log_time >= LOG_THROTTLE:
                        filled_str = (
                            f"{len(filled_rooms)} filled"
                            if filled_rooms
                            else "none yet"
                        )
                        _log(
                            f"Waiting for new room... "
                            f"(rooms filled: {filled_str}, queue: {len(queue)})"
                        )
                        last_log_time = now
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # Pick room closest to full — helps it start sooner
            fresh_rooms.sort(key=lambda g: g.get("agentCount", 0), reverse=True)
            room = fresh_rooms[0]
            room_id = room["id"]
            room_name = room.get("name", room_id)[:40]
            agent_count = room.get("agentCount", 0)
            max_agents = room.get("maxAgents", 100)

            # Take up to MAX_PER_ROOM from front of queue
            batch = []
            for _ in range(MAX_PER_ROOM):
                if not queue:
                    break
                batch.append(queue.popleft())

            if verbose:
                _log(
                    f'Room "{room_name}" ({agent_count}/{max_agents}) '
                    f"— sending {len(batch)} accounts..."
                )

            retry = await _join_batch(session, batch, room_id, on_ready, verbose)

            # Mark room as filled — never touch it again from our side
            filled_rooms.add(room_id)

            # Return failed accounts to front of queue
            for acc in reversed(retry):
                queue.appendleft(acc)

            if verbose and retry:
                _log(f"{len(retry)} accounts back in queue")


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    accounts_file = sys.argv[1] if len(sys.argv) > 1 else "accounts.json"
    abs_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), accounts_file)
    if not os.path.exists(abs_path):
        abs_path = accounts_file

    with open(abs_path) as f:
        accounts = json.load(f)

    results = {}

    async def _on_ready(account: dict, game_id: str, agent_id: str):
        results[account["name"]] = {"game_id": game_id, "agent_id": agent_id}
        print(
            f"  [{account['name']:<16}] ready — game:{game_id[:8]}.. agent:{agent_id[:12] if agent_id else 'RECOVER'}.."
        )

    async def _main():
        rq = asyncio.Queue()
        # Run for a limited time in standalone mode
        try:
            await asyncio.wait_for(
                run_joiner(accounts, _on_ready, rq, verbose=True),
                timeout=120,
            )
        except asyncio.TimeoutError:
            pass
        ok = sum(1 for v in results.values() if v["agent_id"])
        already = sum(1 for v in results.values() if not v["agent_id"])
        print(f"\n=== Summary ===")
        print(f"  Joined:          {ok}")
        print(f"  Already in game: {already}")
        print(f"  Total:           {len(results)}")

    asyncio.run(_main())
