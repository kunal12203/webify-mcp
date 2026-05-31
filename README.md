# Webify

Semantic web graph for AI coding agents. Builds a graph from any web page, then retrieves only the relevant nodes via BFS traversal.

**74% cheaper** and **16x faster** than reading full pages.

## How it works

```
URL → Fetch → Extract content → Build heading-hierarchy graph → Cache
Query → Score nodes (BM25) → BFS traversal → Return ~250-750 tokens
```

Instead of feeding 5,000-50,000 tokens of a full web page to your AI, Webify returns only the 3-5 most relevant sections (~250-750 tokens).

## Quick start

### As a Claude Code MCP tool

```bash
claude mcp add webify -- python3 /path/to/Webify/mcp_server.py
```

Then use in any Claude Code session:
```
> Look up the rate limits for the GitHub API
# Claude automatically uses web_lookup("https://docs.github.com/en/rest/rate-limit", "rate limits")
```

### As a CLI

```bash
# Build a graph
python webify.py build https://docs.python.org/3/library/json.html

# Look up specific info
python webify.py lookup https://docs.python.org/3/library/json.html "how to parse JSON string"

# Check stats
python webify.py stats https://docs.python.org/3/library/json.html
```

### As a Python library

```python
import webify

# Smart lookup with confidence scoring
result = webify.smart_lookup("https://docs.python.org/3/library/json.html", "parse JSON")
print(result["status"])      # "success"
print(result["content"])     # relevant sections only
print(result["tokens_used"]) # ~376 vs ~12000 for full page

# Build graph separately
graph = webify.build_graph("https://example.com/docs")

# Direct retrieval
nodes = webify.retrieve("https://example.com/docs", "authentication")
```

## Features

- **Zero dependencies** — stdlib only (just needs `mcp` for the MCP server)
- **Multi-strategy extraction** — Readability scoring, `__NEXT_DATA__`, JSON-LD, Nuxt
- **Automatic fallback** — Wayback Machine, Google Cache, raw source URLs
- **Confidence scoring** — tells you when to fall back to full page fetch
- **24h caching** — one fetch per page per day
- **BFS graph traversal** — follows edges to find connected context

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  1. FETCH                                           │
│     Primary → Cache proxies (Wayback/Google/Raw)    │
├─────────────────────────────────────────────────────┤
│  2. EXTRACT                                         │
│     Readability scoring → __NEXT_DATA__ → JSON-LD   │
├─────────────────────────────────────────────────────┤
│  3. CHUNK                                           │
│     Heading-hierarchy sections (Algolia approach)   │
├─────────────────────────────────────────────────────┤
│  4. GRAPH                                           │
│     Nodes (heading/code/param/table/list/text)      │
│     Edges (contains/has_example/see_also/sibling)   │
├─────────────────────────────────────────────────────┤
│  5. RETRIEVE                                        │
│     BM25 scoring → BFS from top seeds → cluster     │
├─────────────────────────────────────────────────────┤
│  6. CONFIDENCE                                      │
│     Score extraction quality → fallback signal      │
└─────────────────────────────────────────────────────┘
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `web_lookup(url, query)` | Smart lookup — returns relevant content or fallback signal |
| `web_build(url)` | Pre-build graph for a URL (useful for multi-query pages) |
| `web_stats(url)` | Show graph stats for a cached URL |

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `WEBIFY_CACHE_DIR` | `~/.cache/webify` | Where to store cached graphs |

## Performance

Tested on 50 documentation pages:
- **86% reliability** (returns useful content)
- **74% token savings** vs full page reads
- **16x faster** after initial build (cache hit)
- Average response: ~376 tokens (vs ~12,000 for full page)

## Contributing

PRs welcome. The engine is a single file (`webify.py`) with zero external dependencies.

Key areas for contribution:
- More extraction strategies (MDX, Docusaurus, Sphinx)
- Better BM25 scoring
- Edge type improvements
- Cache invalidation strategies
- Test coverage

## License

MIT
