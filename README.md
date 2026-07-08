<div align="center">

# Webify

**Adaptive web research for AI coding agents**

91% of Deep Research quality · 5% of the cost · Works in every MCP client

[![PyPI version](https://img.shields.io/pypi/v/webify-mcp?color=blue&label=pip%20install%20webify-mcp)](https://pypi.org/project/webify-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/)

A skill by [GrapeRoot](https://graperoot.dev)

**Docs:** [中文](docs/README.zh-CN.md) · [日本語](docs/README.ja.md) · [한국어](docs/README.ko.md) · [Español](docs/README.es.md) · [हिन्दी](docs/README.hi.md) · [Français](docs/README.fr.md) · [Deutsch](docs/README.de.md) · [Português](docs/README.pt-BR.md) · [Русский](docs/README.ru.md)

</div>

---

## Install in 2 commands

```bash
pip install webify-mcp
claude mcp add webify -- webify-mcp
```

That's it. Works in Claude Code, Cursor, VS Code, Windsurf, and Zed.

---

## What it does

Webify gives your AI two tools for web research — both dramatically cheaper than reading full pages:

| Tool | When to use | Cost |
|------|------------|------|
| `web_find(query)` | Research questions, anything needing search | ~$0.003/query |
| `web_lookup(url, query)` | You know the exact URL | ~$0.0005/query |

### web_find — multi-source research

```
Query → Search (Brave/DDG) → Parallel graph builds (3–6 sources)
     → BM25 multi-aspect extraction → Haiku synthesis → Answer
```

Adapts depth to query complexity. Simple questions hit 3 sources. Multi-dimensional research scales to 6+ with independent sub-aspect retrieval. Call it multiple times with focused sub-queries for deep-research-level coverage.

### web_lookup — single-page retrieval

```
URL → Fetch → Semantic graph → BFS traversal → ~250–750 tokens
```

Scores nodes against your query, returns only the relevant subtree — 80–300 tokens instead of the 3,000–15,000 tokens of full page text WebFetch puts in context.

---

## Benchmarks

Blind A/B test against Claude's Deep Research — 15 unseen queries, randomized order, Sonnet judge scoring accuracy + completeness + specificity (1–5 each).

| | Webify | Deep Research |
|--|--------|--------------|
| **Quality** | 68/75 · 91% | 73/75 · 97% |
| **Cost/query** | ~$0.003 | ~$0.05+ |
| **Latency** | 30–90s | 80–280s |
| **Cost efficiency** | **18× better** | baseline |

Webify finds correct information every time. The gap is always completeness — Deep Research reads more. For most queries that difference doesn't matter; for exhaustive research, call `web_find` multiple times.

<details>
<summary>Per-query breakdown</summary>

| Query | Webify | Deep Research |
|-------|--------|--------------|
| Battery degradation | 13/15 | 15/15 |
| OAuth vs OIDC | 13/15 | 15/15 |
| Coral reef bleaching | 14/15 | 15/15 |
| CRISPR gene editing | **15/15** | 13/15 |
| Earthquake & tsunamis | 13/15 | 15/15 |

</details>

---

## How the AI uses it

Once installed, the AI automatically uses Webify for web research instead of expensive built-in tools — no configuration needed. The preference policy is embedded in the package itself.

```
> What are the tradeoffs between Raft and Paxos consensus?
→ Claude calls web_find() — searches, builds graphs, synthesizes answer

> Look up rate limits in the GitHub API docs
→ Claude calls web_lookup() — fetches that page, returns relevant sections only
```

---

## Tool-specific setup

### Claude Code
```bash
pip install webify-mcp
claude mcp add webify -- webify-mcp
```

### Cursor · Windsurf · VS Code (Continue/Cline) · Zed

Add to your MCP config:
```json
{
  "mcpServers": {
    "webify": {
      "command": "webify-mcp"
    }
  }
}
```

Config file locations:
- **Cursor** → `~/.cursor/mcp.json`
- **Windsurf** → `~/.windsurf/settings.json`
- **VS Code / Continue** → `~/.continue/config.json`
- **Zed** → `~/.config/zed/settings.json`

### Any other MCP client
- **Command:** `webify-mcp`
- **Transport:** stdio

---

## Updating

```bash
pip install --upgrade webify-mcp
```

---

## Configuration

| Env var | Required | Description |
|---------|----------|-------------|
| `ANTHROPIC_API_KEY` | For `web_find` | Haiku synthesis |
| `BRAVE_SEARCH_API_KEY` | Recommended | Reliable search · [free 2k/mo](https://brave.com/search/api/) |
| `WEBIFY_CACHE_DIR` | No | Cache dir · default `~/.cache/webify` |

**Search:** Brave API (if key set) → DuckDuckGo Lite (free fallback, no key needed)

### Setting keys

**macOS / Linux** — add to `~/.zshrc` or `~/.bashrc`:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export BRAVE_SEARCH_API_KEY="BSA..."
```

**Windows (PowerShell):**
```powershell
[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")
[Environment]::SetEnvironmentVariable("BRAVE_SEARCH_API_KEY", "BSA...", "User")
```

**In your MCP config** (applies only to Webify):
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

Get your keys:
- Anthropic → https://console.anthropic.com/settings/keys
- Brave Search → https://brave.com/search/api/

---

## CLI usage

```bash
# Build a graph for a URL
python -m webify build https://docs.python.org/3/library/json.html

# Look up specific info
python -m webify lookup https://docs.python.org/3/library/json.html "parse JSON string"
```

## Python library

```python
import webify

# Multi-source search
result = webify.web_find("how does mTLS work in service meshes")
print(result["content"])   # synthesized answer
print(result["sources"])   # [{url, title, confidence, tokens}]

# Single-page lookup
result = webify.smart_lookup("https://docs.python.org/3/library/json.html", "parse JSON")
print(result["content"])   # relevant sections only (~376 tokens)
```

---

## Troubleshooting

```bash
webify-mcp                  # test server (Ctrl+C to exit)
ls ~/.cache/webify/         # check cache
```

- **`webify-mcp: command not found`** → Run `pip install webify-mcp`
- **Tool not showing up** → Restart your editor after adding to config
- **`web_find` errors** → Set `ANTHROPIC_API_KEY`
- **`web_find` returns no results** → DDG rate-limited; set `BRAVE_SEARCH_API_KEY`

---

## License

[MIT](LICENSE) · Copyright © 2026 [GrapeRoot](https://graperoot.dev)
