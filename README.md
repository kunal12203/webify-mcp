# Webify

Semantic web graph for AI coding agents. Builds a graph from any web page, then retrieves only the relevant nodes via BFS traversal.

**74% cheaper** and **16x faster** than reading full pages.

## How it works

```
URL → Fetch → Extract content → Build heading-hierarchy graph → Cache
Query → Score nodes (BM25) → BFS traversal → Return ~250-750 tokens
```

Instead of feeding 5,000-50,000 tokens of a full web page to your AI, Webify returns only the 3-5 most relevant sections (~250-750 tokens).

## Installation

### Quick Install (Recommended)

**macOS / Linux:**
```bash
curl -fsSL https://graperoot.dev/webify/install.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://graperoot.dev/webify/install.ps1 | iex
```

The installer will:
- Download Webify to `~/.webify/`
- Install Python dependencies (`mcp>=1.3.0`)
- Auto-configure detected MCP tools (Claude Code, VS Code, Cursor, Windsurf, Zed)
- Set up cache directory at `~/.cache/webify`

**Requirements:** Python 3.9+, pip, git

---

### Manual Installation

If you prefer manual setup or the auto-installer doesn't detect your tool:

1. **Clone the repository:**
   ```bash
   git clone https://github.com/kunal12203/webify.git
   cd webify
   ```

2. **Install the MCP package:**
   ```bash
   pip install "mcp>=1.3.0"
   ```

3. **Configure your MCP client** (see [Tool-Specific Setup](#tool-specific-setup) below)

## Usage

### As a Claude Code MCP tool

```bash
# Already configured if you used the installer
# Otherwise, manually add:
claude mcp add webify -- python3 /path/to/webify/mcp_server.py
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

## Tool-Specific Setup

### Claude Code

```bash
claude mcp add webify -- python3 ~/.webify/mcp_server.py
```

Verify installation:
```bash
claude mcp list  # Should show "webify"
```

### VS Code (Continue / Cline)

Add to `~/.continue/config.json`:
```json
{
  "mcpServers": {
    "webify": {
      "command": "python3",
      "args": ["/Users/YOUR_USERNAME/.webify/mcp_server.py"]
    }
  }
}
```

**Note:** Replace `/Users/YOUR_USERNAME/` with your actual home directory path.

### Cursor

Add to `~/.cursor/mcp.json` (create if it doesn't exist):
```json
{
  "mcpServers": {
    "webify": {
      "command": "python3",
      "args": ["/Users/YOUR_USERNAME/.webify/mcp_server.py"],
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
      "command": "python3",
      "args": ["/Users/YOUR_USERNAME/.webify/mcp_server.py"]
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
      "command": "python3",
      "args": ["/Users/YOUR_USERNAME/.webify/mcp_server.py"]
    }
  }
}
```

### Other MCP Clients

Webify uses the stdio transport. Configure your MCP client with:
- **Command:** `python3`
- **Args:** `["/path/to/webify/mcp_server.py"]`
- **Transport:** stdio

## Configuration

### Environment Variables

| Env var | Default | Description |
|---------|---------|-------------|
| `WEBIFY_CACHE_DIR` | `~/.cache/webify` | Where to store cached graphs |

### Troubleshooting

**Check Python version:**
```bash
python3 --version  # Must be >= 3.9
```

**Test MCP server directly:**
```bash
python3 ~/.webify/mcp_server.py
# Should wait for stdin (MCP protocol active)
# Press Ctrl+C to exit
```

**Check cache:**
```bash
ls ~/.cache/webify/  # Should show cached .json files after first use
```

**Verify MCP package:**
```bash
python3 -c "import mcp; print(mcp.__version__)"  # Should print version >= 1.3.0
```

**Common issues:**
- **"mcp module not found"** → Run `pip install "mcp>=1.3.0"`
- **"Permission denied"** → Make sure `mcp_server.py` is readable: `chmod +r ~/.webify/mcp_server.py`
- **Tool doesn't see Webify** → Restart your editor after configuration changes

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
