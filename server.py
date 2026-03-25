"""
OpenCaselist MCP Server v5 — smart routing based on full search result paths

The /v1/search endpoint returns a 'path' field that can look like:
  ndtceda25/Emory/TaTa/rounds/5        → go directly to that round, get opensource URL, search docx
  ndtceda25/Emory/TaTa/cites/12        → go directly to that cite, return text
  ndtceda25/Emory/TaTa                 → team-only (no resource), try rounds first then cites

This server preserves the FULL path and routes accordingly so we always
fetch exactly the round or cite the search matched, not all rounds for the team.

Required Render environment variables:
  TABROOM_USERNAME   — your Tabroom.com email
  TABROOM_PASSWORD   — your Tabroom.com password
  CASELIST_SHARD     — e.g. "ndtceda25" (default)
"""

import json
import asyncio
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
# Path parsing
# ─────────────────────────────────────────────

def parse_search_path(path: str) -> dict:
    """
    Parse a full search result path into its components.

    Examples:
      'ndtceda25/Emory/TaTa'              → {type: 'team', caselist, school, team}
      'ndtceda25/Emory/TaTa/rounds/5'     → {type: 'round', caselist, school, team, round_id: '5'}
      'ndtceda25/Emory/TaTa/cites/12'     → {type: 'cite',  caselist, school, team, cite_id: '12'}
    """
    parts = path.strip("/").split("/")
    if len(parts) < 3:
        return {"type": "unknown", "raw": path}

    base = {
        "caselist": parts[0],
        "school":   parts[1],
        "team":     parts[2],
        "raw":      path,
    }

    if len(parts) >= 5:
        resource_type = parts[3].lower()
        resource_id   = parts[4]
        if resource_type == "rounds":
            return {**base, "type": "round", "round_id": resource_id}
        elif resource_type == "cites":
            return {**base, "type": "cite", "cite_id": resource_id}

    return {**base, "type": "team"}


# ─────────────────────────────────────────────
# Docx helpers
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


# ─────────────────────────────────────────────
# Fetchers
# ─────────────────────────────────────────────

async def fetch_round_card(
    client: httpx.AsyncClient,
    caselist: str, school: str, team: str, round_id: str,
    query_terms: list[str],
) -> list[dict]:
    """Fetch a specific round, download its opensource .docx, search inside."""
    url  = f"{API_BASE}/caselists/{caselist}/schools/{school}/teams/{team}/rounds/{round_id}"
    resp = await authed_get(client, url)
    if resp.status_code != 200:
        return []

    rnd            = resp.json()
    opensource_url = rnd.get("opensource", "")
    tournament     = rnd.get("tournament", "?")
    side           = rnd.get("side", "")
    round_num      = rnd.get("round", round_id)
    label          = f"{team} ({school}) — {tournament} Rd {round_num} {side}".strip()

    if not opensource_url:
        # Round exists but no doc — fall back to cites
        return []

    try:
        doc_resp = await client.get(opensource_url, timeout=25.0, follow_redirects=True)
        if doc_resp.status_code != 200:
            return []
        paragraphs = extract_paragraphs(doc_resp.content)
        blocks     = find_card_blocks(paragraphs, query_terms)
        return [
            {
                "team":       f"{caselist}/{school}/{team}",
                "round":      label,
                "source_url": opensource_url,
                "title":      f"Block {i + 1} — {label}",
                "text":       block,
            }
            for i, block in enumerate(blocks)
        ] or [{
            "team":       f"{caselist}/{school}/{team}",
            "round":      label,
            "source_url": opensource_url,
            "title":      f"Doc found but no text matched — {label}",
            "text":       f"The Word doc at {opensource_url} didn't contain '{' '.join(query_terms)}'. "
                          f"The search matched this round's metadata. Try opening the doc directly.",
        }]
    except Exception as e:
        return [{"team": f"{caselist}/{school}/{team}", "round": label,
                 "source_url": opensource_url, "title": "Error reading doc", "text": str(e)}]


async def fetch_cite_card(
    client: httpx.AsyncClient,
    caselist: str, school: str, team: str, cite_id: str,
) -> list[dict]:
    """Fetch a specific cite by ID."""
    # The API doesn't have GET /cites/{id}, so fetch all and filter
    url  = f"{API_BASE}/caselists/{caselist}/schools/{school}/teams/{team}/cites"
    resp = await authed_get(client, url)
    if resp.status_code != 200:
        return []

    cites = resp.json()
    if not isinstance(cites, list):
        return []

    for cite in cites:
        if str(cite.get("cite_id", "")) == str(cite_id):
            return [{
                "team":       f"{caselist}/{school}/{team}",
                "round":      "cites",
                "source_url": "",
                "title":      cite.get("title", "Untitled"),
                "text":       cite.get("cites", ""),
            }]
    return []


async def fetch_team_rounds_all(
    client: httpx.AsyncClient,
    caselist: str, school: str, team: str,
    query_terms: list[str],
) -> list[dict]:
    """Fallback: search ALL rounds for a team when path had no specific round/cite."""
    url  = f"{API_BASE}/caselists/{caselist}/schools/{school}/teams/{team}/rounds"
    resp = await authed_get(client, url)
    if resp.status_code != 200:
        return []

    rounds = resp.json()
    if not isinstance(rounds, list):
        return []

    cards = []
    for rnd in rounds:
        opensource_url = rnd.get("opensource", "")
        if not opensource_url:
            continue
        tournament = rnd.get("tournament", "?")
        side       = rnd.get("side", "")
        round_num  = rnd.get("round", "?")
        label      = f"{team} ({school}) — {tournament} Rd {round_num} {side}".strip()
        try:
            doc_resp = await client.get(opensource_url, timeout=25.0, follow_redirects=True)
            if doc_resp.status_code != 200:
                continue
            paragraphs = extract_paragraphs(doc_resp.content)
            blocks     = find_card_blocks(paragraphs, query_terms)
            for i, block in enumerate(blocks):
                cards.append({
                    "team":       f"{caselist}/{school}/{team}",
                    "round":      label,
                    "source_url": opensource_url,
                    "title":      f"Block {i + 1} — {label}",
                    "text":       block,
                })
        except Exception:
            continue
    return cards


async def fetch_team_cites_all(
    client: httpx.AsyncClient,
    caselist: str, school: str, team: str,
    query_terms: list[str],
) -> list[dict]:
    """Fallback: search all cites for a team."""
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
        # Step 1: search — preserve full raw paths
        resp = await authed_get(client, f"{API_BASE}/search",
                                params={"q": query, "shard": target_shard})
        if resp.status_code != 200:
            return [{"team": "error", "round": "", "source_url": "",
                     "title": f"Search API Error {resp.status_code}", "text": resp.text[:300]}]

        data  = resp.json()
        items = data if isinstance(data, list) else data.get("results", [data])

        # Parse and deduplicate full paths
        seen, parsed_paths = set(), []
        for item in items:
            path = item.get("path", "")
            if path and path not in seen:
                seen.add(path)
                parsed_paths.append(parse_search_path(path))

        if not parsed_paths:
            return [{"team": "-", "round": "", "source_url": "",
                     "title": "No Results",
                     "text": f"No results found for '{query}' in shard '{target_shard}'."}]

        # Step 2: fetch cards based on path type
        async def fetch_for_path(p: dict) -> list[dict]:
            c, s, t = p["caselist"], p["school"], p["team"]

            if p["type"] == "round":
                # Specific round from search — go straight to it
                cards = await fetch_round_card(client, c, s, t, p["round_id"], query_terms)
                if not cards:
                    # Round had no doc, fall back to all cites
                    cards = await fetch_team_cites_all(client, c, s, t, query_terms)
                return cards

            elif p["type"] == "cite":
                # Specific cite from search — fetch it directly
                return await fetch_cite_card(client, c, s, t, p["cite_id"])

            else:
                # Team-level only — try all rounds with docs first, then cites
                cards = await fetch_team_rounds_all(client, c, s, t, query_terms)
                if not cards:
                    cards = await fetch_team_cites_all(client, c, s, t, query_terms)
                return cards

        tasks    = [fetch_for_path(p) for p in parsed_paths]
        nested   = await asyncio.gather(*tasks, return_exceptions=True)
        all_cards = []
        for r in nested:
            if isinstance(r, list):
                all_cards.extend(r)

        if not all_cards:
            return [{
                "team": " | ".join(f"{p['school']}/{p['team']}" for p in parsed_paths),
                "round": "", "source_url": "",
                "title": "Teams found but no card text extracted",
                "text": (
                    "These teams ran this argument but the text couldn't be extracted "
                    "(docs may be private or use different keywords).\n\n"
                    "Teams: " + ", ".join(f"{p['school']}/{p['team']}" for p in parsed_paths)
                ),
            }]

        return all_cards


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
            "serverInfo": {"name": "opencaselist-mcp", "version": "5.0.0"},
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
                    "Automatically routes to the exact round or cite the search matched. "
                    "For rounds: downloads the open-source Word doc and extracts matching card text. "
                    "For cites: returns the cite text directly. "
                    "Falls back gracefully if docs are unavailable."
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

            lines = [f"**{len(cards)} card block(s) for '{query}'** (shard: {shard_override or CASELIST_SHARD}):\n"]
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
    return {"status": "ok", "version": "5.0.0", "shard": CASELIST_SHARD, "credentials_set": bool(TABROOM_USERNAME and TABROOM_PASSWORD)}

@app.get("/")
async def root():
    return {"name": "OpenCaselist MCP Server", "version": "5.0.0", "shard": CASELIST_SHARD}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 OpenCaselist MCP Server v5 on port {port}")
    print(f"📚 Shard: {CASELIST_SHARD}")
    print(f"🔑 Credentials: {'✅' if TABROOM_USERNAME else '❌ missing'}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
