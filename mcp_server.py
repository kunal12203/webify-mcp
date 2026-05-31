#!/usr/bin/env python3
"""Webify MCP Server — stdio transport.

Exposes Webify as MCP tools for Claude Code / any MCP client.

Setup:
    claude mcp add webify -- python3 /path/to/mcp_server.py

Tools:
    web_lookup(url, query)       — Smart lookup with graph retrieval + confidence
    web_build(url)               — Pre-build graph for a URL (cache it)
    web_stats(url)               — Show graph stats for a cached URL
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP
import webify

mcp = FastMCP(
    "webify",
    instructions=(
        "Webify provides efficient web page lookup via semantic graph retrieval. "
        "Use web_lookup(url, query) to extract specific information from a web page. "
        "It builds a graph once per page then retrieves only relevant nodes — "
        "74% cheaper and 16x faster than reading full pages. "
        "If web_lookup returns status='fallback_needed', use WebFetch instead."
    ),
)


@mcp.tool()
def web_lookup(url: str, query: str, max_results: int = 3) -> dict:
    """Look up specific information from a web page using graph-based retrieval.

    Fetches the page (cached after first hit), builds a semantic graph, then
    retrieves only the relevant nodes via BFS traversal. Returns ~250-750 tokens
    instead of the full page (5000-50000 tokens).

    Args:
        url: The web page URL to look up.
        query: What you're looking for (e.g. "authentication setup", "API rate limits").
        max_results: Max nodes to return (default 3, usually sufficient).

    Returns:
        status: "success" | "low_confidence" | "fallback_needed"
        content: The retrieved content (relevant sections only).
        confidence: Graph quality assessment.
        tokens_used: How many tokens the response costs.
        fallback_reason: Why to use WebFetch instead (if fallback_needed).
    """
    return webify.smart_lookup(url, query, max_results=max_results)


@mcp.tool()
def web_build(url: str, force_refresh: bool = False) -> dict:
    """Pre-build/refresh the graph for a URL. Useful for pages you'll query multiple times.

    Args:
        url: The web page URL to index.
        force_refresh: Re-fetch even if cached.
    """
    try:
        graph = webify.build_graph(url, force_refresh=force_refresh)
        return {
            "ok": True,
            "url": url,
            "title": graph.get("title", "")[:100],
            "stats": graph.get("stats", {}),
            "confidence": graph.get("confidence", {}),
            "extraction_method": graph.get("extraction_method", "unknown"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def web_stats(url: str) -> dict:
    """Show graph statistics for a previously fetched URL.

    Args:
        url: The URL to check stats for.
    """
    try:
        graph = webify.build_graph(url, force_refresh=False)
        return {
            "ok": True,
            "url": url,
            "title": graph.get("title", "")[:100],
            "stats": graph.get("stats", {}),
            "confidence": graph.get("confidence", {}),
            "cached": True,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
