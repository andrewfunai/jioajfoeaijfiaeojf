"""
OpenCaselist MCP Server v7

Key fix: uses GET /v1/download?path=<opensource> with auth cookie to download
Word docs, rather than fetching the opensource URL directly. The API spec
confirms /v1/download is the correct authenticated download endpoint.

Flow:
  1. GET /v1/search?q=...&shard=...                        → matched team paths
  2. GET /v1/caselists/{c}/schools/{s}/teams/{t}/rounds    → all rounds per team
  3. GET /v1/download?path=<round.opensource> (authed)     → download the .docx bytes
  4. Parse .docx, find card blocks matching query terms
  5. Deduplicate repeated cards across rounds
  6. Fall back to /cites if team has no open-source rounds

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
# Config
# ─────────────────────────────────────────────

TABROOM_USERNAME = os.environ.get("TABROOM_USERNAME", "")
TABROOM_PASSWORD = os.environ.get("TABROOM_PASSWORD", "")
CASELIST_SHARD   = os.environ.get("CASELIST_SHARD", "ndtceda25")
API_BASE         = "https://api.opencaselist.com/v1"

# Persistent authenticated session (like requests.Session)
_session: httpx.AsyncClient | None = None


async def get_session() -> httpx.AsyncClient | None:
    """
    Return a logged-in httpx.AsyncClient whose cookie jar holds caselist_token.
    Re-logs in if the session is None or if a request returns 401.
    """
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
        return client  # cookie jar is populated automatically
    await client.aclose()
    return None


async def authed_get(client_ignored, url: str, params: dict = None) -> httpx.Response:
    """Get using the persistent session. Re-authenticates once on 401."""
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
# Download via /v1/download?path=
# ─────────────────────────────────────────────

async def download_opensource_doc(client_ignored, opensource_path: str) -> bytes | None:
    """
    Use the authenticated /v1/download?path= endpoint to fetch a .docx file.
    The session cookie is carried automatically by the persistent httpx session.
    Falls back to treating opensource as a direct URL for older entries.
    """
    if not opensource_path:
        return None

    # Strategy 1: official authenticated download endpoint
    try:
        resp = await authed_get(None, f"{API_BASE}/download", params={"path": opensource_path})
        if resp.status_code == 200 and len(resp.content) > 500 and resp.content[:2] == b'PK':
            return resp.content
    except Exception:
        pass

    # Strategy 2: opensource is already a full URL (older caselist entries)
    if opensource_path.startswith("http"):
        try:
            session = await get_session()
            if session:
                resp = await session.get(opensource_path, timeout=25.0)
                if resp.status_code == 200 and len(resp.content) > 500 and resp.content[:2] == b'PK':
                    return resp.content
        except Exception:
            pass

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
    hits = set()
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
    """Deduplicate cards by fingerprinting the first 300 chars of text."""
    seen, result = set(), []
    for card in cards:
        fp = hashlib.md5(card["text"][:300].strip().encode()).hexdigest()
        if fp not in seen:
            seen.add(fp)
            result.append(card)
    return result


# ─────────────────────────────────────────────
# Per-team fetching
# ─────────────────────────────────────────────

async def fetch_all_rounds_for_team(
    client_ignored,
    caselist: str, school: str, team: str,
    query_terms: list[str],
) -> list[dict]:
    """Download ALL open-source round docs for a team and search each one."""
    url  = f"{API_BASE}/caselists/{caselist}/schools/{school}/teams/{team}/rounds"
    resp = await authed_get(None, url)

    if resp.status_code != 200:
        return [{"team": f"{caselist}/{school}/{team}", "round": "error", "source_url": "",
                 "title": f"❌ Could not fetch rounds (HTTP {resp.status_code})",
                 "text": resp.text[:300]}]

    rounds = resp.json()
    if not isinstance(rounds, list) or not rounds:
        return []

    rounds_with_docs = [r for r in rounds if r.get("opensource")]
    if not rounds_with_docs:
        return []  # signal to caller: fall back to cites

    async def process_round(rnd: dict) -> list[dict]:
        opensource_path = rnd.get("opensource", "")
        tournament      = rnd.get("tournament", "?")
        side            = rnd.get("side", "")
        round_num       = rnd.get("round", "?")
        label           = f"{team} ({school}) — {tournament} Rd {round_num} {side}".strip()

        content = await download_opensource_doc(None, opensource_path)

        if content is None:
            return [{"team": f"{caselist}/{school}/{team}", "round": label,
                     "source_url": opensource_path,
                     "title": f"⚠️ Download failed — {label}",
                     "text": f"Path attempted: {opensource_path}"}]

        paragraphs = extract_paragraphs(content)
        blocks     = find_card_blocks(paragraphs, query_terms)

        if blocks:
            return [{"team": f"{caselist}/{school}/{team}", "round": label,
                     "source_url": opensource_path,
                     "title": f"Block {i+1} — {label}", "text": block}
                    for i, block in enumerate(blocks)]

        return [{"team": f"{caselist}/{school}/{team}", "round": label,
                 "source_url": opensource_path,
                 "title": f"📄 No match in doc ({len(paragraphs)} ¶) — {label}",
                 "text": (f"Doc downloaded OK, {len(paragraphs)} paragraphs, "
                          f"but none matched: {query_terms}\n"
                          f"Sample paragraphs: {' | '.join(paragraphs[:4])}")}]

    results = await asyncio.gather(*[process_round(r) for r in rounds_with_docs],
                                   return_exceptions=True)
    cards = []
    for r in results:
        if isinstance(r, list):
            cards.extend(r)
    return cards


async def fetch_cites_for_team(
    client_ignored,
    caselist: str, school: str, team: str,
    query_terms: list[str],
) -> list[dict]:
    url  = f"{API_BASE}/caselists/{caselist}/schools/{school}/teams/{team}/cites"
    resp = await authed_get(None, url)
    if resp.status_code != 200:
        return []

    cites = resp.json()
    if not isinstance(cites, list):
        return []

    return [
        {"team": f"{caselist}/{school}/{team}", "round": "cites", "source_url": "",
         "title": cite.get("title", "Untitled"), "text": cite.get("cites", "")}
        for cite in cites
        if any(term in (cite.get("title", "") + cite.get("cites", "")).lower()
               for term in query_terms)
    ]


# ─────────────────────────────────────────────
# Main search orchestrator
# ─────────────────────────────────────────────

def parse_team(path: str) -> tuple[str, str, str] | None:
    parts = path.strip("/").split("/")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    return None


async def search_opencaselist(query: str, shard: str = None) -> list[dict]:
    target_shard = shard or CASELIST_SHARD
    query_terms  = [t.lower() for t in query.split() if len(t) > 1]

    session = await get_session()
    if session is None:
        return [{"team": "error", "round": "", "source_url": "",
                 "title": "Authentication Error",
                 "text": "Set TABROOM_USERNAME and TABROOM_PASSWORD in Render environment variables."}]

    resp = await authed_get(None, f"{API_BASE}/search",
                            params={"q": query, "shard": target_shard})
    if resp.status_code != 200:
        return [{"team": "error", "round": "", "source_url": "",
                 "title": f"Search API Error {resp.status_code}", "text": resp.text[:300]}]

    data  = resp.json()
    items = data if isinstance(data, list) else data.get("results", [data])

    seen_teams, teams = set(), []
    for item in items:
        parsed = parse_team(item.get("path", ""))
        if parsed:
            key = "/".join(parsed)
            if key not in seen_teams:
                seen_teams.add(key)
                teams.append(parsed)

    if not teams:
        return [{"team": "-", "round": "", "source_url": "",
                 "title": "No Results",
                 "text": f"No teams found for '{query}' in '{target_shard}'."}]

    async def fetch_team(caselist, school, team):
        cards = await fetch_all_rounds_for_team(None, caselist, school, team, query_terms)
        has_real = any(not c["title"].startswith(("⚠️", "📄", "❌")) for c in cards)
        if not has_real:
            cite_cards = await fetch_cites_for_team(None, caselist, school, team, query_terms)
            if cite_cards:
                return cite_cards
        return cards

    nested = await asyncio.gather(*[fetch_team(c, s, t) for c, s, t in teams],
                                  return_exceptions=True)
    all_cards = []
    for r in nested:
        if isinstance(r, list):
            all_cards.extend(r)

    deduped = dedup_cards(all_cards)

    if not deduped:
        return [{"team": " | ".join(f"{s}/{t}" for _, s, t in teams),
                 "round": "", "source_url": "",
                 "title": "Teams found but no cards extracted",
                 "text": "Teams: " + ", ".join(f"{s}/{t}" for _, s, t in teams)}]

    return deduped


# ─────────────────────────────────────────────
# SSE / MCP boilerplate
# ─────────────────────────────────────────────

_client_queues: dict[str, asyncio.Queue] = {}


async def sse_stream(client_id: str) -> AsyncGenerator[str, None]:
    queue = asyncio.Queue()
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
            "serverInfo": {"name": "opencaselist-mcp", "version": "7.0.0"},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return make_response(msg_id, {
            "tools": [{
                "name": "search_caselist",
                "description": (
                    f"Search OpenCaselist for full debate evidence cards. Default shard: {CASELIST_SHARD}. "
                    "Downloads all open-source round docs per team via the authenticated /v1/download endpoint, "
                    "searches inside each one, and deduplicates repeated cards. "
                    "Falls back to /cites if no open-source docs exist."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search terms, e.g. 'faciality'"},
                        "shard": {"type": "string",
                                  "description": f"Shard (default: {CASELIST_SHARD}). E.g. ndtceda24, ndtceda25, hspolicy25"},
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
    return StreamingResponse(sse_stream(client_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


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
    return JSONResponse(content={"resource": "https://jioajfoeaijfiaeojf-3.onrender.com", "authorization_servers": []})

@app.get("/.well-known/oauth-authorization-server")
async def oauth_auth_server():
    return JSONResponse(content={"issuer": "https://jioajfoeaijfiaeojf-3.onrender.com", "authorization_endpoint": "", "token_endpoint": "", "response_types_supported": []})

@app.post("/register")
async def register():
    return JSONResponse(status_code=400, content={"error": "registration_not_supported"})

@app.get("/health")
async def health():
    return {"status": "ok", "version": "7.0.0", "shard": CASELIST_SHARD,
            "credentials_set": bool(TABROOM_USERNAME and TABROOM_PASSWORD)}

@app.get("/")
async def root():
    return {"name": "OpenCaselist MCP Server", "version": "7.0.0", "shard": CASELIST_SHARD}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 OpenCaselist MCP Server v7 on port {port}")
    print(f"📚 Shard: {CASELIST_SHARD}")
    print(f"🔑 Credentials: {'✅' if TABROOM_USERNAME else '❌ missing'}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
