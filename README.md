# Webify

Adaptive web research for AI coding agents. Search the web, build semantic graphs, get synthesized answers — at 5% the cost of deep research tools.

A skill by [GrapeRoot](https://graperoot.dev)

**Docs:** [中文](docs/README.zh-CN.md) | [日本語](docs/README.ja.md) | [한국어](docs/README.ko.md) | [Español](docs/README.es.md) | [हिन्दी](docs/README.hi.md) | [Français](docs/README.fr.md) | [Deutsch](docs/README.de.md) | [Português](docs/README.pt-BR.md) | [Русский](docs/README.ru.md)

## What it does

| Tool | Purpose | Cost |
|------|---------|------|
| `web_find(query)` | Multi-source web search + synthesis | ~$0.003/query |
| `web_lookup(url, query)` | Single-page graph retrieval | ~$0.0005/query |

**web_find** searches DuckDuckGo, builds semantic graphs from multiple sources in parallel, extracts relevant content via BM25, and synthesizes with Haiku. It adapts depth based on query complexity — simple factual queries hit 3 sources, multi-dimensional research queries scale to 6+ sources with multi-aspect retrieval.

**web_lookup** fetches a single page, builds a heading-hierarchy graph, and returns only the relevant nodes (~250-750 tokens instead of 5,000-50,000).

## Benchmarks

Blind A/B evaluation against Claude's Deep Research on 15 unseen queries (5 tech, 5 non-tech, 5 mixed). Judge: Sonnet, scoring accuracy + completeness + specificity (1-5 each, max 15/query).

| Metric | Webify | Deep Research |
|--------|--------|--------------|
| Quality score | **68/75** (90.7%) | 73/75 (97.3%) |
| Cost per query | **~$0.003** | ~$0.05+ |
| Latency | **30-90s** | 80-280s |
| Cost efficiency | **18× better** | baseline |

Webify achieves 91% of Deep Research quality at 5% of the cost. The gap is always on completeness/specificity, never accuracy — Webify finds correct information but Deep Research finds more of it.

<details>
<summary>Per-query breakdown (unseen validation set)</summary>

| Query | Webify | Deep Research | Winner |
|-------|--------|--------------|--------|
| Battery degradation mechanisms | 13/15 | 15/15 | Deep |
| OAuth vs OIDC | 13/15 | 15/15 | Deep |
| Coral reef bleaching | 14/15 | 15/15 | Deep |
| CRISPR gene editing | 15/15 | 13/15 | **Webify** |
| Earthquake tsunami mechanics | 13/15 | 15/15 | Deep |

Scoring: (accuracy/completeness/specificity), each 1-5. Blind judge with randomized A/B order.
</details>

## How it works

### web_find pipeline

```
Query → Complexity Detection (1-3) → DuckDuckGo Search
  → Parallel Graph Builds (3-6+ sources)
  → Multi-Aspect BM25 Extraction
  → Haiku Synthesis + Raw Fragments
```

Key components:
- **Adaptive complexity**: Heuristic scoring scales sources, nodes, and synthesis depth
- **LinUCB contextual bandit**: Learns query reformulation strategies per query type
- **Multi-aspect retrieval**: Decomposes complex queries into sub-aspects, runs BM25 independently
- **Domain affinity**: Welford online stats — learns which sites produce quality content
- **Citation chasing**: Follows primary-source URLs found in pages
- **No hard caps**: The calling model controls depth by making multiple calls

### web_lookup pipeline

```
URL → Fetch → Extract (Readability/NEXT_DATA/JSON-LD)
  → Build heading-hierarchy graph → Cache (24h)
Query → BM25 score nodes → BFS traversal → ~250-750 tokens
```

## Installation

```bash
pip install webify-mcp
claude mcp add webify -- webify-mcp
```

That's it. Two commands. Works with any MCP client — see [Tool-Specific Setup](#tool-specific-setup) for VS Code, Cursor, Windsurf, Zed.

**Requirements:** Python 3.9+, pip

## Updating

```bash
pip install --upgrade webify-mcp
```

## Tool-Specific Setup

### Claude Code

Already configured during installation. Verify:
```bash
claude mcp list  # Should show "webify"
```

### VS Code (Continue / Cline)

Add to `~/.continue/config.json`:
```json
{
  "mcpServers": {
    "webify": {
      "command": "webify-mcp"
    }
  }
}
```

### Cursor

Add to `~/.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "webify": {
      "command": "webify-mcp",
      "env": {}
    }
  }
}
```

### Windsurf

Add to `~/.windsurf/settings.json`:
```json
{
  "mcp.servers": {
    "webify": {
      "command": "webify-mcp"
    }
  }
}
```

### Zed

Add to `~/.config/zed/settings.json`:
```json
{
  "mcp_servers": {
    "webify": {
      "command": "webify-mcp"
    }
  }
}
```

### Other MCP Clients

Webify uses stdio transport. Configure with:
- **Command:** `webify-mcp`
- **Transport:** stdio

## MCP Tools

| Tool | Description |
|------|-------------|
| `web_find(query)` | Search the web + synthesize multi-source answer |
| `web_lookup(url, query)` | Retrieve relevant content from a specific URL |
| `web_build(url)` | Pre-build graph for a URL (cache it) |
| `web_stats(url)` | Show graph stats for a cached URL |

## Usage

### As a Claude Code MCP tool

```
> What are the tradeoffs between Raft and Paxos consensus algorithms?
# Claude uses web_find → searches, builds graphs, synthesizes answer

> Look up rate limits in the GitHub REST API docs
# Claude uses web_lookup → fetches specific page, returns relevant sections
```

### As a CLI

```bash
python webify.py build https://docs.python.org/3/library/json.html
python webify.py lookup https://docs.python.org/3/library/json.html "parse JSON string"
python webify.py stats https://docs.python.org/3/library/json.html
```

### As a Python library

```python
import webify

# Multi-source web search
result = webify.web_find("how does mTLS work in service meshes")
print(result["content"])     # Synthesized answer
print(result["sources"])     # [{url, title, confidence, tokens}]

# Single-page lookup
result = webify.smart_lookup("https://docs.python.org/3/library/json.html", "parse JSON")
print(result["content"])     # Relevant sections only (~376 tokens)
```

## Configuration

| Env var | Required | Description |
|---------|----------|-------------|
| `ANTHROPIC_API_KEY` | For `web_find` | Haiku synthesis + bandit learning |
| `BRAVE_SEARCH_API_KEY` | Recommended | Reliable search ([free 2k queries/mo](https://brave.com/search/api/)) |
| `WEBIFY_CACHE_DIR` | No | Cache location (default: `~/.cache/webify`) |

**Search priority:** Brave API (if key set) → DuckDuckGo Lite (free, no key, may rate-limit under heavy use).

### Setting API Keys

**macOS / Linux** — add to `~/.zshrc` or `~/.bashrc`:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export BRAVE_SEARCH_API_KEY="BSA..."
```
Then restart your terminal or run `source ~/.zshrc`.

**Windows (PowerShell)** — set permanently:
```powershell
[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")
[Environment]::SetEnvironmentVariable("BRAVE_SEARCH_API_KEY", "BSA...", "User")
```
Then restart your terminal.

**Per-tool env (Claude Code, Cursor, Windsurf)** — add to your MCP config:
```json
{
  "mcpServers": {
    "webify": {
      "command": "webify-mcp",
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "BRAVE_SEARCH_API_KEY": "BSA..."
      }
    }
  }
}
```

**Get your keys:**
- Anthropic: https://console.anthropic.com/settings/keys
- Brave Search: https://brave.com/search/api/ (free plan — 2,000 queries/month)

## Troubleshooting

```bash
python3 --version              # Must be >= 3.9
webify-mcp                     # Test server (Ctrl+C to exit)
ls ~/.cache/webify/            # Check cache
```

Common issues:
- **"webify-mcp: command not found"** → Run `pip install webify-mcp`
- **Tool not detected** → Restart your editor after config changes
- **web_find returns errors** → Set `ANTHROPIC_API_KEY` environment variable
- **web_find returns "no_results"** → DDG is rate-limiting; set `BRAVE_SEARCH_API_KEY` for reliable search

## License

[MIT](LICENSE) — Copyright (c) 2026 GrapeRoot
