#!/usr/bin/env python3
"""Webify MCP Server — stdio transport.

Exposes Webify as MCP tools for Claude Code / any MCP client.

Setup:
    claude mcp add webify -- python3 /path/to/mcp_server.py

Tools:
    web_lookup(url, query)       — Smart lookup with graph retrieval + confidence
    web_find(query)              — Search web + parallel multi-source retrieval
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
        "Webify provides efficient web lookup via semantic graph retrieval. "
        "Use web_find(query) to search the web and get a synthesized, multi-source answer — "
        "it searches DuckDuckGo, builds semantic graphs in parallel, extracts structurally "
        "relevant subtrees from multiple anchors per page, then synthesizes with Haiku. "
        "Result quality matches DeepSearch at 1-5% of the token cost. "
        "Use web_lookup(url, query) when you already know the URL. "
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
def web_find(query: str, num_sources: int = 0, synthesize: bool = True) -> dict:
    """Search the web and return a synthesized, high-quality answer from multiple sources.

    Pipeline: DuckDuckGo search → parallel semantic graph builds → multi-aspect
    BM25 extraction → Haiku synthesis. Adaptively scales depth based on query
    complexity (more sources + broader retrieval for multi-dimensional questions).

    No hard cap on sources. For exhaustive research, call web_find multiple times
    with different sub-queries — each call runs its own search and graph builds.
    This is how you get deep-research-level coverage through Webify.

    Args:
        query: What to search for (e.g. "gut microbiome mental health evidence").
        num_sources: Sources to fetch per search. 0 = auto-scale by complexity (3-6).
                     Pass higher (8, 10, 12) for broader single-query coverage.
                     For exhaustive research, prefer multiple calls with focused queries.
        synthesize: Set False to return raw fragments instead of synthesized answer.

    Returns:
        status: "success" | "partial" | "no_results"
        content: Synthesized answer with source-attributed fragments appended
                 for complex queries (gives the caller full context).
        sources: [{url, title, confidence, tokens}]
        tokens_used: Tokens in response.
        search_results: Raw DDG results for transparency.
    """
    result = webify.web_find(query, num_sources=num_sources, synthesize=synthesize)

    # For complex queries: append raw fragments so the calling model has full
    # material to synthesize from (Haiku synthesis is concise; the caller may
    # want the underlying evidence for deeper answers)
    complexity = webify._query_complexity(query)
    if complexity >= 2 and synthesize and result.get("raw_fragments"):
        result["content"] = (
            result["content"] +
            "\n\n---\n## Source Fragments (for additional detail)\n\n" +
            result["raw_fragments"]
        )
        result["tokens_used"] = result.get("fragment_tokens", 0) + result.get("tokens_used", 0)

    result.pop("raw_fragments", None)
    return result


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


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
