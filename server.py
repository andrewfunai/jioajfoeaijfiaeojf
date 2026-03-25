"""
OpenCaselist MCP Server — pulls real cards from api.opencaselist.com
Authenticates with Tabroom credentials and uses the /v1/search endpoint.

Required environment variables (set in Render dashboard):
  TABROOM_USERNAME   — your Tabroom.com username (email)
  TABROOM_PASSWORD   — your Tabroom.com password
  CASELIST_SHARD     — which caselist to search, e.g. "ndtceda24" (default: ndtceda24)

Deploy on Render:
  Start command: uvicorn server:app --host 0.0.0.0 --port $PORT
"""

import json
import asyncio
import os
import uuid
import httpx
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
CASELIST_SHARD   = os.environ.get("CASELIST_SHARD", "ndtceda24")
API_BASE         = "https://api.opencaselist.com/v1"

_auth_cookie: str | None = None


# ─────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────

async def login() -> str | None:
    """Log in to Tabroom and return the caselist_token cookie."""
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


# ─────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────

async def search_opencaselist(query: str, shard: str = None) -> list[dict]:
    """Search the real OpenCaselist API and return result cards."""
    target_shard = shard or CASELIST_SHARD
    cookie = await get_auth_cookie()

    if not cookie:
        return [{
            "id": "auth_error",
            "title": "Authentication Error",
            "text": (
                "No Tabroom credentials configured. "
                "Go to Render → Environment and add:\n"
                "  TABROOM_USERNAME = your Tabroom email\n"
                "  TABROOM_PASSWORD = your Tabroom password"
            ),
        }]

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{API_BASE}/search",
            params={"q": query, "shard": target_shard},
            cookies={"caselist_token": cookie},
            timeout=15.0,
        )

        # Refresh token once on 401
        if resp.status_code == 401:
            global _auth_cookie
            _auth_cookie = await login()
            if not _auth_cookie:
                return [{"id": "auth_error", "title": "Auth Refresh Failed",
                         "text": "Could not re-authenticate. Check TABROOM_USERNAME/PASSWORD on Render."}]
            resp = await client.get(
                f"{API_BASE}/search",
                params={"q": query, "shard": target_shard},
                cookies={"caselist_token": _auth_cookie},
                timeout=15.0,
            )

        if resp.status_code != 200:
            return [{"id": "api_error", "title": f"API Error {resp.status_code}",
                     "text": resp.text[:500]}]

        data = resp.json()

        # Normalize response shape
        if isinstance(data, list):
            results = data
        elif isinstance(data, dict) and "results" in data:
            results = data["results"]
        else:
            results = [data] if data else []

        if not results:
            return [{"id": "no_results", "title": "No Results",
                     "text": f"No results found for '{query}' in shard '{target_shard}'."}]

        return [
            {
                "id":    str(item.get("id", i)),
                "title": item.get("path", "Unknown"),
                "text":  item.get("content", ""),
                "shard": item.get("shard", target_shard),
                "path":  item.get("path", ""),
            }
            for i, item in enumerate(results)
        ]


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
            "serverInfo": {"name": "opencaselist-mcp", "version": "2.0.0"},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return make_response(msg_id, {
            "tools": [{
                "name": "search_caselist",
                "description": (
                    f"Search the real OpenCaselist wiki for debate evidence cards. "
                    f"Default shard: {CASELIST_SHARD}. "
                    "Returns full card content including cite, tag, and card text from disclosed team files."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search terms, e.g. 'regulation K cancer housing'",
                        },
                        "shard": {
                            "type": "string",
                            "description": (
                                f"Caselist shard to search (default: {CASELIST_SHARD}). "
                                "Examples: ndtceda24, hspolicy24, hspolicy25, ndtceda25"
                            ),
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
            query         = arguments.get("query", "")
            shard_override = arguments.get("shard")
            cards = await search_opencaselist(query, shard=shard_override)

            lines = [f"**{len(cards)} result(s) for '{query}'** (shard: {shard_override or CASELIST_SHARD}):\n"]
            for card in cards:
                lines.append(f"---\n📁 **{card['title']}**\n{card['text']}\n")

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


# Required OAuth discovery endpoints (Claude.ai probes these)
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
        "protocol": "MCP SSE 2024-11-05",
        "shard": CASELIST_SHARD,
        "credentials_set": bool(TABROOM_USERNAME and TABROOM_PASSWORD),
    }


@app.get("/")
async def root():
    return {
        "name": "OpenCaselist MCP Server",
        "shard": CASELIST_SHARD,
        "endpoints": {"sse": "GET /sse", "messages": "POST /messages", "health": "GET /health"},
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 OpenCaselist MCP Server on port {port}")
    print(f"📚 Shard: {CASELIST_SHARD}")
    print(f"🔑 Credentials: {'✅ set' if TABROOM_USERNAME else '❌ missing'}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
