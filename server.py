"""
MCP-style SSE server for Claude Desktop.
Searches a sample card database (or local .docx/.txt files) and streams results.
"""

import json
import os
import glob
import asyncio
import argparse
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ─────────────────────────────────────────────
# 1. Sample card database (fallback / demo data)
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
# 2. File-based search (optional, scans a folder)
# ─────────────────────────────────────────────

def search_local_files(query: str, folder: str) -> list[dict]:
    """
    Scan .txt and .docx files in `folder` for lines containing `query`.
    Returns a list of card-style dicts.
    """
    results = []
    terms = [t.lower() for t in query.split() if t]

    # ── plain-text files ──
    for path in glob.glob(os.path.join(folder, "**", "*.txt"), recursive=True):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            snippets = [
                l.strip() for l in lines
                if all(t in l.lower() for t in terms)
            ]
            for i, snippet in enumerate(snippets[:3]):   # max 3 hits per file
                results.append({
                    "id": f"file_{os.path.basename(path)}_{i}",
                    "title": f"{os.path.basename(path)} – match {i+1}",
                    "text": snippet,
                })
        except Exception:
            pass

    # ── Word documents ──
    try:
        import docx  # python-docx
        for path in glob.glob(os.path.join(folder, "**", "*.docx"), recursive=True):
            try:
                doc = docx.Document(path)
                paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
                snippets = [
                    p for p in paragraphs
                    if all(t in p.lower() for t in terms)
                ]
                for i, snippet in enumerate(snippets[:3]):
                    results.append({
                        "id": f"docx_{os.path.basename(path)}_{i}",
                        "title": f"{os.path.basename(path)} – match {i+1}",
                        "text": snippet,
                    })
            except Exception:
                pass
    except ImportError:
        pass   # python-docx not installed; skip .docx files

    return results


# ─────────────────────────────────────────────
# 3. Core search logic
# ─────────────────────────────────────────────

def search_cards(query: str, cards_folder: str | None = None) -> list[dict]:
    """
    Return cards matching `query`.
    Priority: local files (if folder given) → sample cards.
    """
    terms = [t.lower() for t in query.split() if t]
    if not terms:
        return []

    # Try local files first
    if cards_folder and os.path.isdir(cards_folder):
        file_results = search_local_files(query, cards_folder)
        if file_results:
            return file_results

    # Fall back to sample cards
    matched = []
    for card in SAMPLE_CARDS:
        haystack = (card["title"] + " " + card["text"]).lower()
        if any(t in haystack for t in terms):
            matched.append(card)
    return matched


# ─────────────────────────────────────────────
# 4. FastAPI app
# ─────────────────────────────────────────────

app = FastAPI(title="OpenCaselist MCP SSE Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global config (set at startup via CLI args)
CARDS_FOLDER: str | None = None


async def event_stream(query: str) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted search result events."""
    cards = search_cards(query, CARDS_FOLDER)

    if not cards:
        payload = json.dumps({"type": "no_results", "query": query})
        yield f"data: {payload}\n\n"
        return

    for card in cards:
        payload = json.dumps({
            "type": "search_result",
            "card_id": card["id"],
            "title": card["title"],
            "text": card["text"],
        })
        yield f"data: {payload}\n\n"
        await asyncio.sleep(0.05)   # small delay to make streaming visible

    # Signal end of results
    yield f"data: {json.dumps({'type': 'done', 'total': len(cards)})}\n\n"


@app.get("/mcp-sse")
async def mcp_sse(query: str = ""):
    """
    MCP-style SSE endpoint.
    Usage: GET /mcp-sse?query=K-disad+regulation+cancer
    """
    return StreamingResponse(
        event_stream(query),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok", "cards_folder": CARDS_FOLDER or "sample data"}


@app.get("/")
async def root():
    return {
        "message": "OpenCaselist MCP SSE Server",
        "endpoints": {
            "sse": "GET /mcp-sse?query=<your search>",
            "health": "GET /health",
        },
    }


# ─────────────────────────────────────────────
# 5. Entry point
# ─────────────────────────────────────────────

def main():
    global CARDS_FOLDER

    parser = argparse.ArgumentParser(description="OpenCaselist MCP SSE Server")
    parser.add_argument(
        "--folder",
        default=None,
        help="Path to a folder of .txt/.docx card files (optional; uses sample data if omitted)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    args = parser.parse_args()

    CARDS_FOLDER = args.folder
    if CARDS_FOLDER:
        print(f"📂  Scanning cards folder: {CARDS_FOLDER}")
    else:
        print("📋  No folder supplied – using built-in sample cards.")

    print(f"\n🚀  Server starting at http://{args.host}:{args.port}")
    print(f"🔗  MCP SSE endpoint: http://{args.host}:{args.port}/mcp-sse\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
