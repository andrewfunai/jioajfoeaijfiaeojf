"""
OpenCaselist MCP Server v6

Flow:
  1. GET /v1/search?q=...&shard=...              → get matching team paths (deduplicated to team level)
  2. GET /caselists/{c}/schools/{s}/teams/{t}/rounds  → get ALL rounds for each team
  3. For each round with an opensource URL, download the .docx and extract text
  4. Search all paragraphs for query terms, grab surrounding card blocks
  5. Deduplicate card blocks across rounds (same card often appears in every round file)
  6. Fall back to /cites if a team has no open-source rounds

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

_auth_cookie: str | None = None


# ─────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────

async def login() -> str | None:
    if not TABROOM_USERNAME or not TABROOM_PASSWORD:
        return None
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{API_BASE}/login",
            json={"username": TABROOM_USERNAME, "password": TABROOM_PASSWORD},
            follow_redirects=True,
        )
        if resp.status_code in (200, 201):
            return resp.cookies.get("caselist_token")
    return None


async def get_auth_cookie() -> str | None:
    global _auth_cookie
    if not _auth_cookie:
        _auth_cookie = await login()
    return _auth_cookie


async def authed_get(client: httpx.AsyncClient, url: str, params: dict = None) -> httpx.Response:
    global _auth_cookie
    cookie = await get_auth_cookie()
    resp = await client.get(url, params=params, cookies={"caselist_token": cookie}, timeout=20.0)
    if resp.status_code == 401:
        _auth_cookie = await login()
        resp = await client.get(url, params=params, cookies={"caselist_token": _auth_cookie}, timeout=20.0)
    return resp


# ─────────────────────────────────────────────
# Docx helpers
# ─────────────────────────────────────────────

async def download_docx(client: httpx.AsyncClient, url: str) -> bytes | None:
    """Download a .docx, trying with auth cookie first then without."""
    cookie = await get_auth_cookie()
    for cookies in [{"caselist_token": cookie} if cookie else {}, {}]:
        try:
            resp = await client.get(url, timeout=25.0, follow_redirects=True, cookies=cookies)
            if resp.status_code == 200 and len(resp.content) > 500:
                # Make sure it looks like a zip/docx and not an HTML error page
                if resp.content[:2] == b'PK':
                    return resp.content
        except Exception:
            pass
    return None


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


def dedup_blocks(cards: list[dict]) -> list[dict]:
    """
    Remove duplicate card blocks across rounds.
    Uses a hash of the first 300 chars of text — same card appearing in
    multiple round files will be deduplicated, keeping the first occurrence.
    """
    seen = set()
    result = []
    for card in cards:
        fingerprint = hashlib.md5(card["text"][:300].encode()).hexdigest()
        if fingerprint not in seen:
            seen.add(fingerprint)
            result.append(card)
    return result


# ─────────────────────────────────────────────
# Core: fetch all rounds for a team + search inside each doc
# ─────────────────────────────────────────────

async def fetch_all_rounds_for_team(
    client: httpx.AsyncClient,
    caselist: str, school: str, team: str,
    query_terms: list[str],
) -> list[dict]:
    """
    Download ALL open-source round docs for a team and search each one.
    Returns deduplicated matching card blocks.
    """
    url  = f"{API_BASE}/caselists/{caselist}/schools/{school}/teams/{team}/rounds"
    resp = await authed_get(client, url)

    if resp.status_code != 200:
        return [{"team": f"{caselist}/{school}/{team}", "round": "error", "source_url": "",
                 "title": f"Could not fetch rounds list (HTTP {resp.status_code})",
                 "text": resp.text[:300]}]

    rounds = resp.json()
    if not isinstance(rounds, list) or not rounds:
        return []

    rounds_with_docs = [r for r in rounds if r.get("opensource")]
    if not rounds_with_docs:
        return []  # caller will fall back to cites

    # Download all docs concurrently
    async def process_round(rnd: dict) -> list[dict]:
        opensource_url = rnd.get("opensource", "")
        tournament     = rnd.get("tournament", "?")
        side           = rnd.get("side", "")
        round_num      = rnd.get("round", "?")
        label          = f"{team} ({school}) — {tournament} Rd {round_num} {side}".strip()

        content = await download_docx(client, opensource_url)
        if content is None:
            # Return a diagnostic so we know the doc was attempted
            return [{"team": f"{caselist}/{school}/{team}", "round": label,
                     "source_url": opensource_url,
                     "title": f"⚠️ Could not download — {label}",
                     "text": f"URL attempted: {opensource_url}\n(File may require direct login or is unavailable)"}]

        paragraphs = extract_paragraphs(content)
        blocks     = find_card_blocks(paragraphs, query_terms)

        if blocks:
            return [{"team": f"{caselist}/{school}/{team}", "round": label,
                     "source_url": opensource_url,
                     "title": f"Block {i+1} — {label}", "text": block}
                    for i, block in enumerate(blocks)]

        # Doc downloaded OK but no match — useful diagnostic
        return [{"team": f"{caselist}/{school}/{team}", "round": label,
                 "source_url": opensource_url,
                 "title": f"📄 Doc OK ({len(paragraphs)} ¶) — no match — {label}",
                 "text": (f"Downloaded successfully, {len(paragraphs)} paragraphs, "
                          f"but none contained: {query_terms}\n"
                          f"Sample: {' | '.join(paragraphs[:3])}")}]

    results = await asyncio.gather(*[process_round(r) for r in rounds_with_docs],
                                   return_exceptions=True)

    all_cards = []
    for r in results:
        if isinstance(r, list):
            all_cards.extend(r)

    return all_cards


async def fetch_cites_for_team(
    client: httpx.AsyncClient,
    caselist: str, school: str, team: str,
    query_terms: list[str],
) -> list[dict]:
    url  = f"{API_BASE}/caselists/{caselist}/schools/{school}/teams/{team}/cites"
    resp = await authed_get(client, url)
    if resp.status_code != 200:
        return []

    cites = resp.json()
    if not isinstance(cites, list):
        return []

    results = []
    for cite in cites:
        title = (cite.get("title") or "").lower()
        text  = (cite.get("cites") or "").lower()
        if any(term in title + " " + text for term in query_terms):
            results.append({
                "team":       f"{caselist}/{school}/{team}",
                "round":      "cites",
                "source_url": "",
                "title":      cite.get("title", "Untitled"),
                "text":       cite.get("cites", ""),
            })
    return results


# ─────────────────────────────────────────────
# Main search orchestrator
# ─────────────────────────────────────────────

def parse_team_from_path(path: str) -> tuple[str, str, str] | None:
    """Extract (caselist, school, team) from any path shape."""
    parts = path.strip("/").split("/")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    return None


async def search_opencaselist(query: str, shard: str = None) -> list[dict]:
    target_shard = shard or CASELIST_SHARD
    query_terms  = [t.lower() for t in query.split() if len(t) > 1]

    cookie = await get_auth_cookie()
    if not cookie:
        return [{
            "team": "error", "round": "", "source_url": "",
            "title": "Authentication Error",
            "text": (
                "No Tabroom credentials configured.\n"
                "Go to Render → Environment and add:\n"
                "  TABROOM_USERNAME = your Tabroom email\n"
                "  TABROOM_PASSWORD = your Tabroom password"
            ),
        }]

    async with httpx.AsyncClient() as client:
        resp = await authed_get(client, f"{API_BASE}/search",
                                params={"q": query, "shard": target_shard})
        if resp.status_code != 200:
            return [{"team": "error", "round": "", "source_url": "",
                     "title": f"Search API Error {resp.status_code}", "text": resp.text[:300]}]

        data  = resp.json()
        items = data if isinstance(data, list) else data.get("results", [data])

        # Deduplicate to unique teams (ignore round/cite suffix in path)
        seen_teams, teams = set(), []
        for item in items:
            parsed = parse_team_from_path(item.get("path", ""))
            if parsed:
                key = "/".join(parsed)
                if key not in seen_teams:
                    seen_teams.add(key)
                    teams.append(parsed)

        if not teams:
            return [{"team": "-", "round": "", "source_url": "",
                     "title": "No Results",
                     "text": f"No teams found for '{query}' in shard '{target_shard}'."}]

        # For each unique team: try all rounds, fall back to cites
        async def fetch_team(caselist, school, team):
            cards = await fetch_all_rounds_for_team(client, caselist, school, team, query_terms)
            # Only fall back to cites if rounds returned nothing OR no docs existed
            has_real_cards = any(
                not c["title"].startswith(("⚠️", "📄"))
                for c in cards
            )
            if not has_real_cards:
                cite_cards = await fetch_cites_for_team(client, caselist, school, team, query_terms)
                if cite_cards:
                    return cite_cards
            return cards

        nested = await asyncio.gather(*[fetch_team(c, s, t) for c, s, t in teams],
                                      return_exceptions=True)

        all_cards = []
        for r in nested:
            if isinstance(r, list):
                all_cards.extend(r)

        # Deduplicate repeated cards across rounds/teams
        deduped = dedup_blocks(all_cards)

        if not deduped:
            return [{"team": " | ".join(f"{s}/{t}" for _, s, t in teams),
                     "round": "", "source_url": "",
                     "title": "Teams found but no card text extracted",
                     "text": ("Teams: " + ", ".join(f"{s}/{t}" for _, s, t in teams))}]

        return deduped


# ─────────────────────────────────────────────
# SSE connection manager
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


# ─────────────────────────────────────────────
# MCP JSON-RPC handler
# ─────────────────────────────────────────────

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
            "serverInfo": {"name": "opencaselist-mcp", "version": "6.0.0"},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return make_response(msg_id, {
            "tools": [{
                "name": "search_caselist",
                "description": (
                    f"Search the OpenCaselist wiki for full debate evidence cards. "
                    f"Default shard: {CASELIST_SHARD}. "
                    "Downloads ALL open-source round documents for each matched team, "
                    "searches inside every one, and deduplicates repeated cards. "
                    "Falls back to the cites endpoint if no open-source docs exist."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search terms, e.g. 'faciality' or 'regulation K cancer'",
                        },
                        "shard": {
                            "type": "string",
                            "description": f"Caselist shard (default: {CASELIST_SHARD}). E.g. ndtceda24, ndtceda25, hspolicy24, hspolicy25",
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
                    f"---\n"
                    f"📁 **{card['team']}** | {card['round']}{source}\n"
                    f"🏷 {card['title']}\n\n"
                    f"{card['text']}\n"
                )

            return make_response(msg_id, {
                "content": [{"type": "text", "text": "\n".join(lines)}]
            })

        return make_error(msg_id, -32601, f"Unknown tool: {tool_name}")

    return make_error(msg_id, -32601, f"Method not found: {method}")


# ─────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────

app = FastAPI(title="OpenCaselist MCP Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    return JSONResponse(content={"resource": "https://jioajfoeaijfiaeojf-3.onrender.com", "authorization_servers": []})

@app.get("/.well-known/oauth-authorization-server")
async def oauth_auth_server():
    return JSONResponse(content={"issuer": "https://jioajfoeaijfiaeojf-3.onrender.com", "authorization_endpoint": "", "token_endpoint": "", "response_types_supported": []})

@app.post("/register")
async def register():
    return JSONResponse(status_code=400, content={"error": "registration_not_supported"})

@app.get("/health")
async def health():
    return {"status": "ok", "version": "6.0.0", "shard": CASELIST_SHARD,
            "credentials_set": bool(TABROOM_USERNAME and TABROOM_PASSWORD)}

@app.get("/")
async def root():
    return {"name": "OpenCaselist MCP Server", "version": "6.0.0", "shard": CASELIST_SHARD}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 OpenCaselist MCP Server v6 on port {port}")
    print(f"📚 Shard: {CASELIST_SHARD}")
    print(f"🔑 Credentials: {'✅' if TABROOM_USERNAME else '❌ missing'}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
