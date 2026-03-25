"""
OpenCaselist MCP Server v8

Performance fixes over v7:
  - Search results carry exact file paths → we download ONLY those files, not
    every round for every team.
  - Hard cap: MAX_TEAMS teams processed, MAX_FILES total file downloads.
  - Per-file download timeout (FILE_TIMEOUT seconds); slow files are skipped.
  - In-process LRU-style download cache so the same .docx is never fetched twice
    within a server lifetime.
  - All downloads still run concurrently via asyncio.gather with a semaphore to
    avoid hammering the upstream server.

Flow:
  1. GET /v1/search?q=...&shard=...        → matched paths (team + file)
  2. Download each matched .docx via /v1/download?path=<path>  (authed, cached)
  3. Parse .docx, find card blocks matching query terms
  4. Deduplicate repeated cards across files
  5. Fall back to /cites for teams whose search paths didn't yield a downloadable doc

Required Render environment variables:
  TABROOM_USERNAME   — your Tabroom.com email
  TABROOM_PASSWORD   — your Tabroom.com password
  CASELIST_SHARD     — e.g. "ndtceda25" (default)
"""

import json
import asyncio
import hashlib
import os
import uuid
import io
import httpx
import docx
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ─────────────────────────────────────────────
# Config / tuning knobs
# ─────────────────────────────────────────────

TABROOM_USERNAME = os.environ.get("TABROOM_USERNAME", "")
TABROOM_PASSWORD = os.environ.get("TABROOM_PASSWORD", "")
CASELIST_SHARD   = os.environ.get("CASELIST_SHARD", "ndtceda25")
API_BASE         = "https://api.opencaselist.com/v1"

MAX_TEAMS        = int(os.environ.get("MAX_TEAMS", "20"))   # cap unique teams processed
MAX_FILES        = int(os.environ.get("MAX_FILES", "40"))   # cap total .docx downloads
FILE_TIMEOUT     = float(os.environ.get("FILE_TIMEOUT", "7"))  # seconds per download
CONCURRENCY      = int(os.environ.get("CONCURRENCY", "8"))   # parallel downloads

# In-process download cache: path → bytes (never re-fetched within a process lifetime)
_download_cache: dict[str, bytes] = {}

# Persistent authenticated session
_session: httpx.AsyncClient | None = None
_dl_semaphore: asyncio.Semaphore | None = None  # initialised on first use


def get_semaphore() -> asyncio.Semaphore:
    global _dl_semaphore
    if _dl_semaphore is None:
        _dl_semaphore = asyncio.Semaphore(CONCURRENCY)
    return _dl_semaphore


# ─────────────────────────────────────────────
# Auth / session
# ─────────────────────────────────────────────

async def get_session() -> httpx.AsyncClient | None:
    global _session
    if _session is None:
        _session = await _create_session()
    return _session


async def _create_session() -> httpx.AsyncClient | None:
    if not TABROOM_USERNAME or not TABROOM_PASSWORD:
        return None
    client = httpx.AsyncClient(follow_redirects=True, timeout=20.0)
    resp = await client.post(
        f"{API_BASE}/login",
        json={"username": TABROOM_USERNAME, "password": TABROOM_PASSWORD, "remember": True},
    )
    if resp.status_code in (200, 201) and "caselist_token" in client.cookies:
        return client
    await client.aclose()
    return None


async def authed_get(url: str, params: dict = None) -> httpx.Response:
    """GET with the persistent session; re-authenticates once on 401."""
    global _session
    session = await get_session()
    if session is None:
        raise RuntimeError("Cannot authenticate — check TABROOM_USERNAME/PASSWORD")
    resp = await session.get(url, params=params)
    if resp.status_code == 401:
        await session.aclose()
        _session = await _create_session()
        if _session is None:
            raise RuntimeError("Re-authentication failed")
        resp = await _session.get(url, params=params)
    return resp


# ─────────────────────────────────────────────
# Download (cached, timeout-guarded, semaphored)
# ─────────────────────────────────────────────

async def download_doc(path: str) -> bytes | None:
    """
    Download a .docx via the authenticated /v1/download endpoint.
    Results are cached in _download_cache for the process lifetime.
    Each individual download is capped at FILE_TIMEOUT seconds.
    Concurrent downloads are limited by _dl_semaphore.
    """
    if not path:
        return None

    # Cache hit — free
    if path in _download_cache:
        return _download_cache[path]

    async with get_semaphore():
        # Double-check after acquiring semaphore (another coroutine may have fetched it)
        if path in _download_cache:
            return _download_cache[path]

        try:
            session = await get_session()
            if session is None:
                return None

            # Strategy 1: authenticated /v1/download endpoint
            resp = await asyncio.wait_for(
                session.get(f"{API_BASE}/download", params={"path": path}),
                timeout=FILE_TIMEOUT,
            )
            if resp.status_code == 401:
                # Re-auth once
                global _session
                await session.aclose()
                _session = await _create_session()
                session = _session
                if session is None:
                    return None
                resp = await asyncio.wait_for(
                    session.get(f"{API_BASE}/download", params={"path": path}),
                    timeout=FILE_TIMEOUT,
                )

            if resp.status_code == 200 and len(resp.content) > 500 and resp.content[:2] == b'PK':
                _download_cache[path] = resp.content
                return resp.content

            # Strategy 2: path is already a full URL (older caselist entries)
            if path.startswith("http"):
                resp2 = await asyncio.wait_for(
                    session.get(path),
                    timeout=FILE_TIMEOUT,
                )
                if resp2.status_code == 200 and len(resp2.content) > 500 and resp2.content[:2] == b'PK':
                    _download_cache[path] = resp2.content
                    return resp2.content

        except (asyncio.TimeoutError, Exception):
            pass  # skip this file; don't crash the whole search

    return None


# ─────────────────────────────────────────────
# Docx parsing
# ─────────────────────────────────────────────

def extract_paragraphs(content: bytes) -> list[str]:
    try:
        doc = docx.Document(io.BytesIO(content))
        return [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    except Exception as e:
        return [f"[docx parse error: {e}]"]


def find_card_blocks(paragraphs: list[str], query_terms: list[str], context: int = 20) -> list[str]:
    """Return paragraph windows around query term hits."""
    hits: set[int] = set()
    for i, para in enumerate(paragraphs):
        if any(term in para.lower() for term in query_terms):
            for j in range(max(0, i - 2), min(len(paragraphs), i + context)):
                hits.add(j)

    if not hits:
        return []

    sorted_hits = sorted(hits)
    blocks, start, prev = [], sorted_hits[0], sorted_hits[0]
    for idx in sorted_hits[1:]:
        if idx > prev + 1:
            blocks.append((start, prev))
            start = idx
        prev = idx
    blocks.append((start, prev))
    return ["\n".join(paragraphs[s:e + 1]) for s, e in blocks]


def dedup_cards(cards: list[dict]) -> list[dict]:
    seen, result = set(), []
    for card in cards:
        fp = hashlib.md5(card["text"][:300].strip().encode()).hexdigest()
        if fp not in seen:
            seen.add(fp)
            result.append(card)
    return result


# ─────────────────────────────────────────────
# Parse helpers
# ─────────────────────────────────────────────

def parse_team_from_path(path: str) -> tuple[str, str, str] | None:
    """Extract (caselist, school, team) from a search result path."""
    parts = path.strip("/").split("/")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    return None


def get_file_path_from_search_hit(path: str) -> str | None:
    """
    If the search hit path points directly to a file (has an extension), return it.
    Otherwise return None (caller will need to enumerate rounds).
    """
    if "." in path.split("/")[-1]:
        return path
    return None


# ─────────────────────────────────────────────
# Core search logic
# ─────────────────────────────────────────────

async def process_file(
    file_path: str,
    team_label: str,
    caselist: str, school: str, team: str,
    query_terms: list[str],
) -> list[dict]:
    """Download one .docx and return matching card blocks."""
    content = await download_doc(file_path)
    if content is None:
        return []

    paragraphs = extract_paragraphs(content)
    blocks = find_card_blocks(paragraphs, query_terms)
    return [
        {
            "team": f"{caselist}/{school}/{team}",
            "round": team_label,
            "source_url": file_path,
            "title": f"Block {i + 1} — {team_label}",
            "text": block,
        }
        for i, block in enumerate(blocks)
    ]


async def fetch_rounds_and_search(
    caselist: str, school: str, team: str,
    query_terms: list[str],
    known_file_path: str | None,
    file_budget: asyncio.Semaphore,
) -> list[dict]:
    """
    If we already know the exact file from the search hit, download just that.
    Otherwise fetch the rounds list and download only open-source ones —
    subject to the shared file_budget so we don't exceed MAX_FILES globally.
    """
    team_label = f"{team} ({school})"

    if known_file_path:
        # We know the exact file — grab the budget slot and download
        async with file_budget:
            return await process_file(known_file_path, team_label, caselist, school, team, query_terms)

    # Need to enumerate rounds
    try:
        resp = await asyncio.wait_for(
            authed_get(f"{API_BASE}/caselists/{caselist}/schools/{school}/teams/{team}/rounds"),
            timeout=10.0,
        )
    except Exception:
        return []

    if resp.status_code != 200:
        return []

    rounds = resp.json()
    if not isinstance(rounds, list):
        return []

    rounds_with_docs = [r for r in rounds if r.get("opensource")]
    if not rounds_with_docs:
        return []  # signal: fall back to cites

    async def do_round(rnd: dict) -> list[dict]:
        opensource_path = rnd.get("opensource", "")
        tournament = rnd.get("tournament", "?")
        side = rnd.get("side", "")
        round_num = rnd.get("round", "?")
        label = f"{team} ({school}) — {tournament} Rd {round_num} {side}".strip()

        # Consume one slot from the global file budget
        try:
            await asyncio.wait_for(file_budget.acquire(), timeout=1.0)
        except asyncio.TimeoutError:
            return []  # budget exhausted — skip this file

        try:
            return await process_file(opensource_path, label, caselist, school, team, query_terms)
        finally:
            file_budget.release()

    results = await asyncio.gather(*[do_round(r) for r in rounds_with_docs], return_exceptions=True)
    cards = []
    for r in results:
        if isinstance(r, list):
            cards.extend(r)
    return cards


async def fetch_cites_for_team(
    caselist: str, school: str, team: str,
    query_terms: list[str],
) -> list[dict]:
    try:
        resp = await asyncio.wait_for(
            authed_get(f"{API_BASE}/caselists/{caselist}/schools/{school}/teams/{team}/cites"),
            timeout=10.0,
        )
    except Exception:
        return []

    if resp.status_code != 200:
        return []

    cites = resp.json()
    if not isinstance(cites, list):
        return []

    return [
        {
            "team": f"{caselist}/{school}/{team}",
            "round": "cites",
            "source_url": "",
            "title": cite.get("title", "Untitled"),
            "text": cite.get("cites", ""),
        }
        for cite in cites
        if any(term in (cite.get("title", "") + cite.get("cites", "")).lower()
               for term in query_terms)
    ]


# ─────────────────────────────────────────────
# Main search orchestrator
# ─────────────────────────────────────────────

async def search_opencaselist(query: str, shard: str = None) -> list[dict]:
    target_shard = shard or CASELIST_SHARD
    query_terms  = [t.lower() for t in query.split() if len(t) > 1]

    session = await get_session()
    if session is None:
        return [{"team": "error", "round": "", "source_url": "",
                 "title": "Authentication Error",
                 "text": "Set TABROOM_USERNAME and TABROOM_PASSWORD in Render environment variables."}]

    # ── Step 1: Search index ──────────────────────────────────────────────────
    try:
        resp = await asyncio.wait_for(
            authed_get(f"{API_BASE}/search", params={"q": query, "shard": target_shard}),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        return [{"team": "error", "round": "", "source_url": "",
                 "title": "Search API Timeout", "text": "The search index did not respond in time."}]

    if resp.status_code != 200:
        return [{"team": "error", "round": "", "source_url": "",
                 "title": f"Search API Error {resp.status_code}", "text": resp.text[:300]}]

    data  = resp.json()
    items = data if isinstance(data, list) else data.get("results", [data])

    # ── Step 2: Collect unique teams + their known file paths ────────────────
    # Each search hit may carry a path like:
    #   ndtceda25/SchoolName/TeamName/FileName.docx   ← direct file
    #   ndtceda25/SchoolName/TeamName                 ← team directory
    seen_teams: dict[str, str | None] = {}  # team_key → known_file_path | None

    for item in items:
        path = item.get("path", "")
        parsed = parse_team_from_path(path)
        if not parsed:
            continue
        caselist, school, team = parsed
        key = f"{caselist}/{school}/{team}"
        file_path = get_file_path_from_search_hit(path)
        if key not in seen_teams:
            seen_teams[key] = file_path
        elif file_path and seen_teams[key] is None:
            # Upgrade: now we know a concrete file for this team
            seen_teams[key] = file_path

        if len(seen_teams) >= MAX_TEAMS:
            break  # hard cap

    if not seen_teams:
        return [{"team": "-", "round": "", "source_url": "",
                 "title": "No Results",
                 "text": f"No teams found for '{query}' in '{target_shard}'."}]

    # ── Step 3: Download + search (concurrently, budget-capped) ──────────────
    file_budget = asyncio.Semaphore(MAX_FILES)

    async def handle_team(key: str, known_file: str | None) -> list[dict]:
        parts = key.split("/", 2)
        if len(parts) != 3:
            return []
        caselist, school, team = parts

        cards = await fetch_rounds_and_search(
            caselist, school, team, query_terms, known_file, file_budget
        )

        # Fall back to /cites if no real cards found from docs
        has_real = any(not c["title"].startswith(("⚠️", "📄", "❌")) for c in cards)
        if not has_real:
            cite_cards = await fetch_cites_for_team(caselist, school, team, query_terms)
            if cite_cards:
                return cite_cards

        return cards

    nested = await asyncio.gather(
        *[handle_team(key, fp) for key, fp in seen_teams.items()],
        return_exceptions=True,
    )

    all_cards: list[dict] = []
    for r in nested:
        if isinstance(r, list):
            all_cards.extend(r)

    deduped = dedup_cards(all_cards)

    if not deduped:
        team_names = " | ".join(seen_teams.keys())
        return [{"team": team_names, "round": "", "source_url": "",
                 "title": "Teams found but no cards extracted",
                 "text": f"Searched {len(seen_teams)} team(s): {team_names}"}]

    return deduped


# ─────────────────────────────────────────────
# SSE / MCP boilerplate
# ─────────────────────────────────────────────

_client_queues: dict[str, asyncio.Queue] = {}


async def sse_stream(client_id: str) -> AsyncGenerator[str, None]:
    queue: asyncio.Queue = asyncio.Queue()
    _client_queues[client_id] = queue
    yield f"event: endpoint\ndata: /messages?client_id={client_id}\n\n"
    try:
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield f"data: {json.dumps(message)}\n\n"
            except asyncio.TimeoutError:
                yield ": ping\n\n"
    finally:
        _client_queues.pop(client_id, None)


def make_response(id, result):
    return {"jsonrpc": "2.0", "id": id, "result": result}

def make_error(id, code, message):
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


async def handle_jsonrpc(message: dict) -> dict | None:
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params", {})

    if method == "initialize":
        return make_response(msg_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "opencaselist-mcp", "version": "8.0.0"},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return make_response(msg_id, {
            "tools": [{
                "name": "search_caselist",
                "description": (
                    f"Search OpenCaselist for full debate evidence cards. Default shard: {CASELIST_SHARD}. "
                    "Downloads open-source round docs per team via /v1/download (authenticated), "
                    "searches inside each one, deduplicates repeated cards, and falls back to "
                    "/cites when no open-source docs exist. "
                    f"Caps at {MAX_TEAMS} teams and {MAX_FILES} file downloads per query."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search terms, e.g. 'faciality'"},
                        "shard": {
                            "type": "string",
                            "description": f"Shard (default: {CASELIST_SHARD}). E.g. ndtceda24, ndtceda25, hspolicy25",
                        },
                    },
                    "required": ["query"],
                },
            }]
        })

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if tool_name == "search_caselist":
            query          = arguments.get("query", "")
            shard_override = arguments.get("shard")
            cards = await search_opencaselist(query, shard=shard_override)

            lines = [f"**{len(cards)} result(s) for '{query}'** (shard: {shard_override or CASELIST_SHARD}):\n"]
            for card in cards:
                source = f"\n🔗 {card['source_url']}" if card.get("source_url") else ""
                lines.append(
                    f"---\n📁 **{card['team']}** | {card['round']}{source}\n"
                    f"🏷 {card['title']}\n\n{card['text']}\n"
                )

            return make_response(msg_id, {"content": [{"type": "text", "text": "\n".join(lines)}]})

        return make_error(msg_id, -32601, f"Unknown tool: {tool_name}")

    return make_error(msg_id, -32601, f"Method not found: {method}")


# ─────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────

app = FastAPI(title="OpenCaselist MCP Server")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/sse")
async def sse_endpoint(request: Request):
    client_id = str(uuid.uuid4())
    return StreamingResponse(
        sse_stream(client_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.post("/messages")
async def messages_endpoint(request: Request):
    client_id = request.query_params.get("client_id")
    body = await request.json()
    messages = body if isinstance(body, list) else [body]
    for message in messages:
        response = await handle_jsonrpc(message)
        if response is None:
            continue
        if client_id and client_id in _client_queues:
            await _client_queues[client_id].put(response)
        else:
            return JSONResponse(content=response)
    return JSONResponse(content={"status": "ok"})


@app.get("/.well-known/oauth-protected-resource")
async def oauth_resource():
    return JSONResponse(content={
        "resource": "https://jioajfoeaijfiaeojf-3.onrender.com",
        "authorization_servers": [],
    })

@app.get("/.well-known/oauth-authorization-server")
async def oauth_auth_server():
    return JSONResponse(content={
        "issuer": "https://jioajfoeaijfiaeojf-3.onrender.com",
        "authorization_endpoint": "",
        "token_endpoint": "",
        "response_types_supported": [],
    })

@app.post("/register")
async def register():
    return JSONResponse(status_code=400, content={"error": "registration_not_supported"})

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "8.0.0",
        "shard": CASELIST_SHARD,
        "credentials_set": bool(TABROOM_USERNAME and TABROOM_PASSWORD),
        "limits": {"max_teams": MAX_TEAMS, "max_files": MAX_FILES,
                   "file_timeout_s": FILE_TIMEOUT, "concurrency": CONCURRENCY},
    }

@app.get("/")
async def root():
    return {"name": "OpenCaselist MCP Server", "version": "8.0.0", "shard": CASELIST_SHARD}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 OpenCaselist MCP Server v8 on port {port}")
    print(f"📚 Shard: {CASELIST_SHARD}")
    print(f"🔑 Credentials: {'✅' if TABROOM_USERNAME else '❌ missing'}")
    print(f"⚙️  Limits: {MAX_TEAMS} teams, {MAX_FILES} files, {FILE_TIMEOUT}s/file, {CONCURRENCY} concurrent")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
