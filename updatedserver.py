"""
Correct MCP SSE server for Claude Desktop / Claude.ai connectors.
Implements JSON-RPC 2.0 over SSE transport per the MCP spec.

Endpoints:
  GET  /sse       — SSE stream (Claude connects here)
  POST /messages  — JSON-RPC message handler (Claude sends calls here)
  GET  /health    — health check
"""

import json
import asyncio
import os
import glob
from typing import AsyncGenerator
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ─────────────────────────────────────────────
# Sample card database
# ─────────────────────────────────────────────
SAMPLE_CARDS = [
    {
        "id": "card001",
        "title": "K-Disad – Regulation Fails to Prevent Cancer",
        "text": (
            "Federal environmental regulation has consistently failed to prevent cancer "
            "caused by industrial chemical exposure. The regulatory capture of the EPA "
            "means that industry-friendly standards allow carcinogen thresholds far above "
            "safe levels, disproportionately harming low-income communities near facilities."
        ),
    },
    {
        "id": "card002",
        "title": "Kritik – Capitalism Link to Environmental Destruction",
        "text": (
            "Capitalist production logic treats nature as an infinite sink for externalities. "
            "The drive for profit necessarily externalizes environmental costs onto "
            "communities that lack political power to resist. Reform within the system "
            "merely legitimizes ongoing extraction."
        ),
    },
    {
        "id": "card003",
        "title": "Topicality – 'Restrictions' Means Quantitative Limits",
        "text": (
            "Restrictions in the context of energy policy means quantitative caps or "
            "numerical thresholds on production or emissions. Broadening the term to "
            "include permitting delays or procedural requirements destroys predictability "
            "and undermines the negative's ability to prepare."
        ),
    },
    {
        "id": "card004",
        "title": "Counterplan – Carbon Tax Solves Warming Better Than Regulation",
        "text": (
            "A revenue-neutral carbon tax achieves emissions reductions at lower economic "
            "cost than command-and-control regulation. Price signals incentivize innovation "
            "across every sector simultaneously, whereas technology mandates only target "
            "specific industries and freeze technology choices."
        ),
    },
    {
        "id": "card005",
        "title": "Affirmative – FDA Regulation Prevents Cancer Drug Shortages",
        "text": (
            "Robust FDA oversight of oncology drug manufacturing is the last line of "
            "defense against critical cancer drug shortages. When quality-control "
            "inspections are weakened, contamination events spike and hospitals face "
            "allocation crises that directly harm patient survival rates."
        ),
    },
    {
        "id": "card006",
        "title": "Disadvantage – Deregulation Causes Systemic Financial Risk",
        "text": (
            "Rolling back financial regulation removes safeguards that prevent systemic "
            "collapse. Interconnected institutions transmit shocks across the global "
            "economy; without capital requirements and stress-testing mandates, a single "
            "failure cascades into a depression-level event within weeks."
        ),
    },
    {
        "id": "card007",
        "title": "Solvency – Federal Preemption Necessary for Uniform Standards",
        "text": (
            "A patchwork of fifty state regimes creates compliance nightmares for "
            "interstate industries and generates regulatory arbitrage that nullifies "
            "protective intent. Only a federal floor with preemption authority guarantees "
            "uniform protection regardless of where a facility is located."
        ),
    },
    {
        "id": "card008",
        "title": "Impact – Nuclear War Outweighs All Other Impacts",
        "text": (
            "A nuclear exchange between great powers would kill hundreds of millions "
            "immediately and trigger a nuclear winter lasting a decade, collapsing "
            "agriculture globally and causing billions more deaths from starvation. "
            "No other impact on the flow compares in magnitude or irreversibility."
        ),
    },
]

# ─────────────────────────────────────────────
# Search logic
# ─────────────────────────────────────────────

def search_cards(query: str) -> list[dict]:
    terms = [t.lower() for t in query.split() if t]
    if not terms:
        return []
    matched = []
    for card in SAMPLE_CARDS:
        haystack = (card["title"] + " " + card["text"]).lower()
        if any(t in haystack for t in terms):
            matched.append(card)
    return matched


# ─────────────────────────────────────────────
# SSE connection manager
# ─────────────────────────────────────────────

# Each connected client gets a queue for server→client messages
_client_queues: dict[str, asyncio.Queue] = {}


async def sse_stream(client_id: str) -> AsyncGenerator[str, None]:
    """Stream JSON-RPC messages to a connected Claude client."""
    queue = asyncio.Queue()
    _client_queues[client_id] = queue

    # Send the SSE endpoint URL so Claude knows where to POST messages
    # (Required by MCP SSE transport spec)
    endpoint_event = f"event: endpoint\ndata: /messages?client_id={client_id}\n\n"
    yield endpoint_event

    try:
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield f"data: {json.dumps(message)}\n\n"
            except asyncio.TimeoutError:
                # Send keepalive ping
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


def handle_jsonrpc(message: dict) -> dict:
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params", {})

    # ── initialize ──
    if method == "initialize":
        return make_response(msg_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "opencaselist-mcp", "version": "1.0.0"},
        })

    # ── notifications (no response needed) ──
    if method == "notifications/initialized":
        return None

    # ── tools/list ──
    if method == "tools/list":
        return make_response(msg_id, {
            "tools": [
                {
                    "name": "search_caselist",
                    "description": (
                        "Search the OpenCaselist debate card database. "
                        "Returns matching evidence cards with title and full text."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search terms, e.g. 'regulation K cancer housing'",
                            }
                        },
                        "required": ["query"],
                    },
                }
            ]
        })

    # ── tools/call ──
    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if tool_name == "search_caselist":
            query = arguments.get("query", "")
            cards = search_cards(query)

            if not cards:
                content = [{"type": "text", "text": f"No results found for: {query}"}]
            else:
                lines = [f"Found {len(cards)} result(s) for '{query}':\n"]
                for card in cards:
                    lines.append(f"**{card['title']}**\n{card['text']}\n")
                content = [{"type": "text", "text": "\n".join(lines)}]

            return make_response(msg_id, {"content": content})

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

import uuid

@app.get("/sse")
async def sse_endpoint(request: Request):
    """SSE stream — Claude Desktop connects here."""
    client_id = str(uuid.uuid4())
    return StreamingResponse(
        sse_stream(client_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/messages")
async def messages_endpoint(request: Request):
    """JSON-RPC message handler — Claude POSTs tool calls here."""
    client_id = request.query_params.get("client_id")
    body = await request.json()

    # Handle batch (list) or single message
    messages = body if isinstance(body, list) else [body]
    
    for message in messages:
        response = handle_jsonrpc(message)
        if response is None:
            continue  # notification, no reply needed

        # If we have a live SSE client, push via SSE queue
        if client_id and client_id in _client_queues:
            await _client_queues[client_id].put(response)
        else:
            # Fallback: return directly (some clients support this)
            return JSONResponse(content=response)

    return JSONResponse(content={"status": "ok"})


@app.get("/health")
async def health():
    return {"status": "ok", "protocol": "MCP SSE 2024-11-05"}


@app.get("/")
async def root():
    return {
        "name": "OpenCaselist MCP Server",
        "protocol": "MCP SSE",
        "version": "2024-11-05",
        "endpoints": {
            "sse": "GET /sse",
            "messages": "POST /messages?client_id=<id>",
            "health": "GET /health",
        },
    }


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 OpenCaselist MCP Server starting on port {port}")
    print(f"🔗 SSE endpoint: http://0.0.0.0:{port}/sse")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
