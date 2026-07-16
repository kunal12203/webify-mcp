"""
Webify v1.0: DOM Structural Graph for Claude Code

Builds a structural graph from any web page, then retrieves only the relevant
nodes via BFS traversal. Returns ~250-750 tokens instead of 5000-50000.

Architecture:
1. Multi-strategy content extraction (__NEXT_DATA__, JSON-LD, Readability, meta)
2. Heading-hierarchy chunking (Algolia DocSearch approach)
3. Edge extraction from DOM structure (parent-child, references, examples)
4. Graph traversal retrieval (BFS from best match + edge following)
5. Confidence scoring + automatic fallback detection

Zero external dependencies — stdlib only.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import ssl

CACHE_DIR = Path(os.environ.get("WEBIFY_CACHE_DIR", Path.home() / ".cache" / "webify"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

ssl_ctx = ssl.create_default_context()
try:
    # Optional: supplements (never replaces) system verification. Some Python
    # installs (notably python.org builds on macOS) ship without a populated
    # root CA path, which makes every HTTPS fetch fail cert verification.
    # certifi is not a hard dependency — this is a no-op if it isn't installed.
    import certifi
    ssl_ctx.load_verify_locations(certifi.where())
except ImportError:
    pass

MAX_PAGE_BYTES = 3_000_000
TARGET_CHUNK_TOKENS = 250

MIN_NODES_FOR_CONFIDENCE = 5
MIN_AVG_CONTENT_LEN = 40
MAX_BOILERPLATE_RATIO = 0.6


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _content_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:8]


def _estimate_tokens(text: str) -> int:
    code_chars = sum(len(m) for m in re.findall(r'```.*?```|`[^`]+`', text, re.DOTALL))
    return round((len(text) - code_chars) / 3.7 + code_chars / 2.5)


def _decode_entities(text: str) -> str:
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    text = text.replace('&#x27;', "'").replace('&#x2F;', '/').replace('&#x3D;', '=')
    text = re.sub(r'&#x([0-9a-fA-F]+);', lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    return text


def _split_camel(name: str) -> list[str]:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
    return [p.lower() for p in parts.split() if len(p) >= 3]


def _extract_keywords(text: str, extra: str = "") -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []

    def add(w: str):
        w = w.lower().strip("_-./()[]{}\"'`,;:#*")
        if len(w) >= 3 and w not in seen and not w.isdigit():
            seen.add(w)
            tokens.append(w)

    combined = f"{extra} {text}" if extra else text
    words = re.findall(r'[A-Za-z_]\w{2,}', combined)
    for w in words[:100]:
        add(w)
        for p in _split_camel(w):
            add(p)

    return tokens[:30]


# ═══════════════════════════════════════════════════════════════════════════════
# FETCHING
# ═══════════════════════════════════════════════════════════════════════════════

def _is_safe_url(url: str) -> bool:
    """Reject URLs whose host resolves to a private/loopback/link-local/reserved
    address, so agent-driven fetches can't be steered at internal services or
    cloud metadata endpoints (SSRF)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False
    return True


def fetch_page(url: str) -> tuple[str, str]:
    if not _is_safe_url(url):
        raise URLError(f"blocked: '{url}' resolves to a non-public address")
    req = Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    response = urlopen(req, timeout=20, context=ssl_ctx)
    content_type = response.headers.get('Content-Type', 'text/html')
    raw = response.read(MAX_PAGE_BYTES)
    return raw.decode('utf-8', errors='ignore'), content_type


# ═══════════════════════════════════════════════════════════════════════════════
# CACHE PROXY FALLBACK (Wayback Machine, Google Cache, raw source URLs)
# ═══════════════════════════════════════════════════════════════════════════════

_URL_REWRITES = {
    "registry.terraform.io": lambda url: _terraform_raw_url(url),
}

_OPENAPI_SPECS = {
    "petstore.swagger.io": "https://petstore.swagger.io/v2/swagger.json",
}


def _terraform_raw_url(url: str) -> Optional[str]:
    m = re.search(r'/providers/([^/]+)/([^/]+)/latest/docs/resources/(\w+)', url)
    if m:
        org, provider, resource = m.group(1), m.group(2), m.group(3)
        return f"https://raw.githubusercontent.com/{org}/terraform-provider-{provider}/main/website/docs/r/{resource}.html.markdown"
    return None


def _try_openapi_spec(url: str) -> Optional[str]:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""

    spec_url = None
    for pattern, surl in _OPENAPI_SPECS.items():
        if pattern in host:
            spec_url = surl
            break

    if not spec_url:
        return None

    try:
        req = Request(spec_url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
        response = urlopen(req, timeout=5, context=ssl_ctx)
        raw = response.read(MAX_PAGE_BYTES).decode('utf-8', errors='ignore')
        spec = json.loads(raw)
        return _openapi_to_html(spec)
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return None


def _openapi_to_html(spec: dict) -> str:
    parts = []
    info = spec.get("info", {})
    title = info.get("title", "API")
    version = info.get("version", "")
    desc = info.get("description", "")

    parts.append(f"<h1>{title} v{version}</h1>")
    if desc:
        parts.append(f"<p>{desc}</p>")

    host = spec.get("host", "")
    base = spec.get("basePath", "")
    if host:
        parts.append(f"<p>Base URL: {host}{base}</p>")

    paths = spec.get("paths", {})
    for path, methods in paths.items():
        parts.append(f"<h2>{path}</h2>")
        for method, details in methods.items():
            if not isinstance(details, dict):
                continue
            summary = details.get("summary", "")
            op_id = details.get("operationId", "")
            parts.append(f"<h3>{method.upper()} {path}</h3>")
            if summary:
                parts.append(f"<p>{summary}</p>")
            if op_id:
                parts.append(f"<p>Operation: {op_id}</p>")

            params = details.get("parameters", [])
            if params:
                parts.append("<dl>")
                for p in params:
                    if isinstance(p, dict):
                        pname = p.get("name", "")
                        pin = p.get("in", "")
                        pdesc = p.get("description", "")
                        preq = "required" if p.get("required") else "optional"
                        ptype = p.get("type", p.get("schema", {}).get("type", ""))
                        parts.append(f"<dt>{pname} ({pin}, {ptype}, {preq})</dt>")
                        parts.append(f"<dd>{pdesc}</dd>")
                parts.append("</dl>")

            responses = details.get("responses", {})
            if responses:
                resp_items = []
                for code, resp in responses.items():
                    if isinstance(resp, dict):
                        resp_items.append(f"- {code}: {resp.get('description', '')}")
                if resp_items:
                    parts.append("<ul>" + "".join(f"<li>{r}</li>" for r in resp_items) + "</ul>")

    return "\n".join(parts)


def _try_wayback(url: str) -> Optional[str]:
    wayback_url = f"https://web.archive.org/web/2024/{url}"
    try:
        req = Request(wayback_url, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; research bot)',
        })
        response = urlopen(req, timeout=3, context=ssl_ctx)
        html = response.read(MAX_PAGE_BYTES).decode('utf-8', errors='ignore')
        html = re.sub(r'<!-- BEGIN WAYBACK TOOLBAR INSERT -->.*?<!-- END WAYBACK TOOLBAR INSERT -->', '', html, flags=re.DOTALL)
        return html
    except (HTTPError, URLError, OSError, TimeoutError):
        return None


def _try_google_cache(url: str) -> Optional[str]:
    cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{url}"
    try:
        req = Request(cache_url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        })
        response = urlopen(req, timeout=3, context=ssl_ctx)
        html = response.read(MAX_PAGE_BYTES).decode('utf-8', errors='ignore')
        html = re.sub(r'<div[^>]*class="[^"]*google-cache[^"]*"[^>]*>.*?</div>', '', html, flags=re.DOTALL)
        return html
    except (HTTPError, URLError, OSError, TimeoutError):
        return None


def _try_raw_source(url: str) -> Optional[str]:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    for pattern, rewriter in _URL_REWRITES.items():
        if pattern in host:
            raw_url = rewriter(url)
            if raw_url:
                try:
                    req = Request(raw_url, headers={'User-Agent': 'Mozilla/5.0'})
                    response = urlopen(req, timeout=5, context=ssl_ctx)
                    content = response.read(MAX_PAGE_BYTES).decode('utf-8', errors='ignore')
                    if content and len(content) > 200:
                        return _text_to_html(content)
                except (HTTPError, URLError, OSError, TimeoutError):
                    pass
    return None


def _fallback_fetch(url: str) -> Optional[str]:
    api_html = _try_openapi_spec(url)
    if api_html and len(api_html) > 500:
        return api_html

    raw = _try_raw_source(url)
    if raw:
        return raw

    wb = _try_wayback(url)
    if wb and len(wb) > 1000:
        return wb

    gc = _try_google_cache(url)
    if gc and len(gc) > 1000:
        if 'Please click here if you are not redirected' not in gc:
            return gc

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# EMBEDDED DATA EXTRACTION (__NEXT_DATA__, JSON-LD, Nuxt, Gatsby)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_next_data(html: str) -> Optional[str]:
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return _walk_json_for_content(data, max_depth=8)
    except (json.JSONDecodeError, KeyError):
        return None


def _walk_json_ld_node(node, _depth: int = 0) -> list[str]:
    """Recursively extract human-readable text from a JSON-LD node."""
    if _depth > 6:
        return []
    parts = []
    if isinstance(node, list):
        for item in node:
            parts.extend(_walk_json_ld_node(item, _depth))
        return parts
    if not isinstance(node, dict):
        return []

    # @graph: unwrap and recurse — don't also process the wrapper's own fields
    if '@graph' in node:
        graph = node['@graph']
        if isinstance(graph, list):
            for item in graph:
                parts.extend(_walk_json_ld_node(item, _depth + 1))
        return parts

    # Collect primary content (pick the richest field, not all of them)
    content = None
    for key in ('articleBody', 'text', 'content', 'description', 'abstract'):
        val = node.get(key)
        if isinstance(val, str) and len(val) > 40:
            content = val
            break

    # Emit as "## heading\ncontent" when a name/headline is present, else bare
    name = node.get('name') or node.get('headline')
    if isinstance(name, str) and len(name) > 3 and content:
        parts.append(f"## {name}\n{content}")
    elif content:
        parts.append(content)

    # FAQ / Q&A and nested-list patterns
    for key in ('mainEntity', 'hasPart', 'itemListElement', 'about'):
        val = node.get(key)
        if isinstance(val, list):
            for entity in val[:30]:
                if not isinstance(entity, dict):
                    continue
                q = entity.get('name') or entity.get('question', {})
                if isinstance(q, dict):
                    q = q.get('text', '')
                a = entity.get('acceptedAnswer') or entity.get('answer', {})
                if isinstance(a, dict):
                    a = a.get('text', '') or a.get('description', '')
                if q and a and isinstance(q, str) and isinstance(a, str):
                    parts.append(f"## {q}\n{a}")
                else:
                    parts.extend(_walk_json_ld_node(entity, _depth + 1))
        elif isinstance(val, dict):
            parts.extend(_walk_json_ld_node(val, _depth + 1))

    return parts


def _extract_json_ld(html: str) -> Optional[str]:
    matches = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not matches:
        return None
    parts = []
    for raw in matches:
        try:
            data = json.loads(raw)
            # Root may be a list of entities
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                parts.extend(_walk_json_ld_node(node))
        except (json.JSONDecodeError, TypeError):
            continue
    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return "\n\n".join(deduped) if deduped else None


def _extract_nuxt_data(html: str) -> Optional[str]:
    m = re.search(r'window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        raw = m.group(1)
        if raw.startswith('{') and '"' in raw[:100]:
            data = json.loads(raw)
            return _walk_json_for_content(data, max_depth=6)
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _walk_json_for_content(data, max_depth: int = 6, _depth: int = 0) -> Optional[str]:
    if _depth > max_depth:
        return None

    content_keys = {'content', 'body', 'text', 'description', 'markdown', 'html',
                    'raw', 'article', 'post', 'excerpt', 'summary', 'rendered'}
    parts = []

    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, str) and len(val) > 200 and key.lower() in content_keys:
                parts.append(val)
            elif isinstance(val, (dict, list)):
                sub = _walk_json_for_content(val, max_depth, _depth + 1)
                if sub and len(sub) > 200:
                    parts.append(sub)
    elif isinstance(data, list):
        for item in data[:50]:
            sub = _walk_json_for_content(item, max_depth, _depth + 1)
            if sub and len(sub) > 200:
                parts.append(sub)

    if parts:
        parts.sort(key=len, reverse=True)
        return parts[0][:10000]
    return None


def _text_to_html(text: str) -> str:
    lines = text.split('\n')
    html_parts = []
    in_code = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith('```'):
            if in_code:
                html_parts.append('</code></pre>')
                in_code = False
            else:
                lang = stripped[3:].strip()
                html_parts.append(f'<pre><code class="language-{lang}">')
                in_code = True
            continue

        if in_code:
            html_parts.append(line)
            continue

        if stripped.startswith('######'):
            html_parts.append(f'<h6>{stripped[6:].strip()}</h6>')
        elif stripped.startswith('#####'):
            html_parts.append(f'<h5>{stripped[5:].strip()}</h5>')
        elif stripped.startswith('####'):
            html_parts.append(f'<h4>{stripped[4:].strip()}</h4>')
        elif stripped.startswith('###'):
            html_parts.append(f'<h3>{stripped[3:].strip()}</h3>')
        elif stripped.startswith('##'):
            html_parts.append(f'<h2>{stripped[2:].strip()}</h2>')
        elif stripped.startswith('#'):
            html_parts.append(f'<h1>{stripped[1:].strip()}</h1>')
        elif stripped.startswith('- ') or stripped.startswith('* '):
            html_parts.append(f'<li>{stripped[2:]}</li>')
        elif re.match(r'^\d+\.\s', stripped):
            html_parts.append(f'<li>{re.sub(r"^d+.s", "", stripped)}</li>')
        else:
            html_parts.append(f'<p>{stripped}</p>')

    if in_code:
        html_parts.append('</code></pre>')

    return '\n'.join(html_parts)


# ═══════════════════════════════════════════════════════════════════════════════
# READABILITY-STYLE CONTENT SCORING
# ═══════════════════════════════════════════════════════════════════════════════

_POSITIVE_PATTERNS = re.compile(
    r'article|body|content|entry|main|page|post|text|blog|story|paragraph|prose|doc',
    re.IGNORECASE
)
_NEGATIVE_PATTERNS = re.compile(
    r'combx|comment|contact|footer|footnote|masthead|media|meta|outbrain|promo|'
    r'related|scroll|shoutbox|sidebar|sponsor|shopping|tags|tool|widget|nav|'
    r'menu|breadcrumb|cookie|banner|popup|modal|overlay|skip|pagination|toolbar',
    re.IGNORECASE
)


def _score_block(tag: str, class_id: str, text_len: int, link_len: int) -> float:
    score = 0.0

    tag_scores = {
        'article': 10, 'main': 10, 'section': 5, 'div': 3, 'p': 5,
        'pre': 8, 'code': 6, 'table': 3, 'ul': 2, 'ol': 2, 'dl': 4,
        'blockquote': 3, 'figure': 3,
        'nav': -10, 'footer': -10, 'header': -5, 'aside': -8,
        'form': -5, 'button': -5,
    }
    score += tag_scores.get(tag.lower(), 0)

    if class_id:
        if _POSITIVE_PATTERNS.search(class_id):
            score += 15
        if _NEGATIVE_PATTERNS.search(class_id):
            score -= 20

    if text_len > 0:
        score += min(text_len / 100, 10)

    if text_len > 0 and link_len > 0:
        link_ratio = link_len / text_len
        if link_ratio > 0.5:
            score -= 10
        elif link_ratio > 0.3:
            score -= 5

    return score


def _find_matching_close(html: str, tag: str, start_pos: int) -> int:
    depth = 1
    pos = start_pos
    open_pat = re.compile(rf'<{tag}[\s>]', re.IGNORECASE)
    close_pat = re.compile(rf'</{tag}\s*>', re.IGNORECASE)

    while depth > 0 and pos < len(html):
        next_open = open_pat.search(html, pos)
        next_close = close_pat.search(html, pos)

        if not next_close:
            return len(html)

        if next_open and next_open.start() < next_close.start():
            depth += 1
            pos = next_open.end()
        else:
            depth -= 1
            if depth == 0:
                return next_close.end()
            pos = next_close.end()

    return len(html)


def extract_main_content(html: str) -> str:
    cleaned = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<style[^>]*>.*?</style>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<noscript[^>]*>.*?</noscript>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<!--.*?-->', '', cleaned, flags=re.DOTALL)

    body_m = re.search(r'<body[^>]*>(.*?)</body>', cleaned, re.DOTALL | re.IGNORECASE)
    if not body_m:
        return cleaned
    body = body_m.group(1)

    candidates = []

    open_pattern = re.compile(
        r'<(main|article|section|div)([^>]*)>',
        re.IGNORECASE
    )

    for m in open_pattern.finditer(body):
        tag = m.group(1).lower()
        attrs = m.group(2)
        content_start = m.end()

        close_pos = _find_matching_close(body, tag, content_start)
        close_tag_len = len(f'</{tag}>') + 1
        content = body[content_start:close_pos - close_tag_len] if close_pos > content_start + close_tag_len else body[content_start:close_pos]

        class_id = ""
        cls_m = re.search(r'class="([^"]*)"', attrs)
        id_m = re.search(r'id="([^"]*)"', attrs)
        if cls_m:
            class_id += cls_m.group(1)
        if id_m:
            class_id += " " + id_m.group(1)

        text_only = re.sub(r'<[^>]+>', '', content)
        text_len = len(text_only.strip())
        link_text = ''.join(re.findall(r'<a[^>]*>(.*?)</a>', content, re.DOTALL))
        link_len = len(re.sub(r'<[^>]+>', '', link_text))

        score = _score_block(tag, class_id, text_len, link_len)

        heading_count = len(re.findall(r'<h[1-4]', content))
        score += heading_count * 3

        code_count = len(re.findall(r'<pre[^>]*>', content))
        score += code_count * 4

        if text_len > 200:
            candidates.append((score, text_len, content))

    if candidates:
        candidates.sort(key=lambda x: (-x[0], -x[1]))
        best_score, best_len, best = candidates[0]
        if best_score > 10 or len(re.findall(r'<h[1-6]', best)) >= 2:
            return best
        if best_len > 2000:
            return best

    stripped = body
    stripped = re.sub(r'<nav[^>]*>.*?</nav>', '', stripped, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r'<footer[^>]*>.*?</footer>', '', stripped, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r'<aside[^>]*>.*?</aside>', '', stripped, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r'<header[^>]*>.*?</header>', '', stripped, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r'<div[^>]*class="[^"]*(?:nav|menu|sidebar|footer|header|banner|cookie|popup)[^"]*"[^>]*>.*?</div>', '', stripped, flags=re.DOTALL | re.IGNORECASE)
    return stripped


# ═══════════════════════════════════════════════════════════════════════════════
# HEADING-HIERARCHY CHUNKING
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Section:
    id: str
    heading: str
    depth: int
    breadcrumb: list
    content_blocks: list = field(default_factory=list)
    links: list = field(default_factory=list)
    parent_id: str = ""
    children_ids: list = field(default_factory=list)


def parse_into_sections(html_content: str, url: str) -> list[Section]:
    url_hash = _url_hash(url)

    code_blocks = []
    def save_code(m):
        idx = len(code_blocks)
        lang = ""
        lang_m = re.search(r'class="[^"]*(?:language-|lang-|highlight-)(\w+)', m.group(0))
        if lang_m:
            lang = lang_m.group(1)
        code_text = re.sub(r'<[^>]+>', '', m.group(1))
        code_text = _decode_entities(code_text).strip()
        code_blocks.append((lang, code_text))
        return f' __CODE_{idx}__ '

    content = re.sub(r'<pre[^>]*>(?:<code[^>]*>)?(.*?)(?:</code>)?</pre>', save_code, html_content, flags=re.DOTALL)

    parts = re.split(r'(<h[1-6][^>]*>.*?</h[1-6]>)', content, flags=re.DOTALL | re.IGNORECASE)

    sections: list[Section] = []
    breadcrumb: list[str] = []
    breadcrumb_ids: list[str] = []
    counter = 0
    current_depth = 0

    for part in parts:
        heading_m = re.match(r'<h([1-6])[^>]*>(.*?)</h[1-6]>', part, re.DOTALL | re.IGNORECASE)
        if heading_m:
            depth = int(heading_m.group(1))
            title = re.sub(r'<[^>]+>', '', heading_m.group(2)).strip()
            title = _decode_entities(title)
            title = re.sub(r'[\\u00b6#]$', '', title).strip()

            if not title or len(title) < 2:
                continue

            while len(breadcrumb) >= depth:
                breadcrumb.pop()
                if breadcrumb_ids:
                    breadcrumb_ids.pop()
            breadcrumb.append(title)

            counter += 1
            section_id = f"{url_hash}_{counter:04d}"
            breadcrumb_ids.append(section_id)

            parent_id = breadcrumb_ids[-2] if len(breadcrumb_ids) >= 2 else ""

            section = Section(
                id=section_id,
                heading=title,
                depth=depth,
                breadcrumb=list(breadcrumb),
                parent_id=parent_id,
            )
            sections.append(section)
            current_depth = depth

            if parent_id:
                for s in sections:
                    if s.id == parent_id:
                        s.children_ids.append(section_id)
                        break
        else:
            if not sections:
                counter += 1
                section_id = f"{url_hash}_{counter:04d}"
                sections.append(Section(
                    id=section_id,
                    heading="(root)",
                    depth=0,
                    breadcrumb=["(root)"],
                ))

            current_section = sections[-1]
            _parse_content_block(part, current_section, code_blocks)

    return sections


def _parse_content_block(html: str, section: Section, code_blocks: list):
    for idx, (lang, code_text) in enumerate(code_blocks):
        if f'__CODE_{idx}__' in html:
            html = html.replace(f'__CODE_{idx}__', '')
            if code_text and len(code_text) > 10:
                section.content_blocks.append(("code", code_text, lang))

    for m in re.finditer(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', html, re.DOTALL):
        href = m.group(1)
        link_text = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        if href and link_text and len(link_text) > 2:
            section.links.append((href, link_text))

    for m in re.finditer(r'<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>', html, re.DOTALL):
        term = _decode_entities(re.sub(r'<[^>]+>', '', m.group(1)).strip())
        desc = _decode_entities(re.sub(r'<[^>]+>', '', m.group(2)).strip())
        if term:
            section.content_blocks.append(("parameter", f"{term}: {desc}", ""))

    table_matches = re.finditer(r'<table[^>]*>(.*?)</table>', html, re.DOTALL | re.IGNORECASE)
    for tm in table_matches:
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', tm.group(1), re.DOTALL)
        table_data = []
        for row in rows:
            cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', row, re.DOTALL)
            cells = [_decode_entities(re.sub(r'<[^>]+>', '', c).strip()) for c in cells]
            if any(cells):
                table_data.append(" | ".join(cells))
        if len(table_data) >= 2:
            section.content_blocks.append(("table", "\n".join(table_data), ""))

    list_items = re.findall(r'<li[^>]*>(.*?)</li>', html, re.DOTALL)
    if len(list_items) >= 2:
        items = []
        for li in list_items:
            item_text = _decode_entities(re.sub(r'<[^>]+>', '', li).strip())
            if item_text and len(item_text) > 5:
                items.append(f"- {item_text}")
        if items:
            section.content_blocks.append(("list", "\n".join(items[:20]), ""))

    text = re.sub(r'<[^>]+>', ' ', html)
    text = _decode_entities(text)
    text = re.sub(r'\s+', ' ', text).strip()

    if len(text) > 80:
        existing_content = ' '.join(c[1] for c in section.content_blocks)
        if len(text) > len(existing_content) * 0.3 or not existing_content:
            section.content_blocks.append(("text", text[:1000], ""))


# ═══════════════════════════════════════════════════════════════════════════════
# GRAPH CONSTRUCTION
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class GraphNode:
    id: str
    type: str
    content: str
    section_id: str
    heading: str
    breadcrumb: list
    depth: int
    keywords: list
    tokens: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.id, "type": self.type, "content": self.content,
            "section_id": self.section_id, "heading": self.heading,
            "breadcrumb": self.breadcrumb, "depth": self.depth,
            "keywords": self.keywords, "tokens": self.tokens,
        }


@dataclass
class GraphEdge:
    source: str
    target: str
    type: str
    weight: float = 1.0

    def as_dict(self) -> dict:
        return {"source": self.source, "target": self.target, "type": self.type, "weight": self.weight}


def _assess_confidence(nodes: list[dict], html: str) -> dict:
    if not nodes:
        return {"level": "none", "score": 0, "reason": "no nodes extracted"}

    num_nodes = len(nodes)
    avg_content_len = sum(len(n.get("content", "")) for n in nodes) / max(num_nodes, 1)
    type_counts = {}
    for n in nodes:
        type_counts[n.get("type", "unknown")] = type_counts.get(n.get("type", "unknown"), 0) + 1

    heading_count = type_counts.get("heading", 0)
    code_count = type_counts.get("code", 0)
    list_count = type_counts.get("list", 0)
    text_count = type_counts.get("text", 0)

    boilerplate_words = {'cookie', 'privacy', 'consent', 'subscribe', 'newsletter',
                         'sign in', 'log in', 'accept', 'terms', 'policy'}
    all_content = " ".join(n.get("content", "").lower() for n in nodes[:10])
    boilerplate_hits = sum(1 for w in boilerplate_words if w in all_content)

    score = 0
    reasons = []

    if num_nodes >= MIN_NODES_FOR_CONFIDENCE:
        score += 30
    elif num_nodes >= 3:
        score += 15
    else:
        reasons.append(f"only {num_nodes} nodes")

    if avg_content_len >= MIN_AVG_CONTENT_LEN:
        score += 20
    else:
        reasons.append(f"avg content too short ({avg_content_len:.0f} chars)")

    if heading_count >= 3:
        score += 20
    elif heading_count >= 1:
        score += 10
    else:
        reasons.append("no headings found")

    if code_count >= 1:
        score += 15
    if text_count >= 2 or list_count >= 2:
        score += 15

    if boilerplate_hits >= 3:
        score -= 30
        reasons.append(f"boilerplate detected ({boilerplate_hits} signals)")

    # Nav-shell inflation: many nodes but nearly all are short headings/nav
    # This is the false-confidence pattern on JS-heavy docs sites
    nav_count = sum(1 for n in nodes if _is_nav_node(n))
    nav_ratio = nav_count / max(num_nodes, 1)
    if nav_ratio > 0.6:
        score -= 25
        reasons.append(f"nav-shell inflation ({nav_ratio:.0%} nav nodes)")
    elif nav_ratio > 0.4:
        score -= 10
        reasons.append(f"high nav ratio ({nav_ratio:.0%})")

    # High node count but very thin content = JS app shell, not real extraction
    if num_nodes >= 20 and avg_content_len < 60:
        score -= 20
        reasons.append(f"thin content at scale ({num_nodes} nodes, avg {avg_content_len:.0f} chars)")

    if '<div id="root"></div>' in html or '<div id="app"></div>' in html:
        text_in_body = len(re.sub(r'<[^>]+>', '', html))
        script_len = sum(len(m) for m in re.findall(r'<script[^>]*>.*?</script>', html, re.DOTALL))
        if script_len > text_in_body * 3:
            score -= 20
            reasons.append("SPA with minimal server HTML")

    if score >= 70:
        level = "high"
    elif score >= 40:
        level = "medium"
    elif score >= 20:
        level = "low"
    else:
        level = "none"

    return {
        "level": level,
        "score": score,
        "reason": "; ".join(reasons) if reasons else "good extraction",
        "signals": {
            "num_nodes": num_nodes,
            "avg_content_len": round(avg_content_len),
            "headings": heading_count,
            "code_blocks": code_count,
            "boilerplate_hits": boilerplate_hits,
        }
    }


def build_graph(url: str, force_refresh: bool = False) -> dict:
    url_hash = _url_hash(url)
    cache_path = CACHE_DIR / f"{url_hash}.json"

    if not force_refresh and cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            if time.time() - data.get("fetched_at", 0) < 86400:
                return data
        except (json.JSONDecodeError, KeyError):
            pass

    fetch_failed = False
    html = ""
    try:
        html, content_type = fetch_page(url)
    except (HTTPError, URLError, OSError) as e:
        fetch_failed = True
        fallback_html = _fallback_fetch(url)
        if fallback_html:
            html = fallback_html
        else:
            raise

    page_hash = _content_hash(html)

    extraction_method = "readability"
    if fetch_failed:
        extraction_method = "cache_proxy"
    main_content = extract_main_content(html)

    heading_count = len(re.findall(r'<h[1-6][^>]*>', main_content, re.IGNORECASE))
    text_length = len(re.sub(r'<[^>]+>', '', main_content))

    if heading_count < 2 and text_length < 1000:
        next_data = _extract_next_data(html)
        if next_data and len(next_data) > 500:
            candidate = _text_to_html(next_data)
            candidate_headings = len(re.findall(r'<h[1-6]', candidate))
            candidate_text = len(re.sub(r'<[^>]+>', '', candidate))
            if candidate_headings > heading_count or candidate_text > text_length * 1.5:
                main_content = candidate
                extraction_method = "next_data"

        if extraction_method in ("readability", "cache_proxy"):
            json_ld = _extract_json_ld(html)
            if json_ld and len(json_ld) > 500:
                candidate = _text_to_html(json_ld)
                candidate_text = len(re.sub(r'<[^>]+>', '', candidate))
                if candidate_text > text_length * 1.5:
                    main_content = candidate
                    extraction_method = "json_ld"

        if extraction_method in ("readability", "cache_proxy"):
            nuxt = _extract_nuxt_data(html)
            if nuxt and len(nuxt) > 500:
                candidate = _text_to_html(nuxt)
                candidate_text = len(re.sub(r'<[^>]+>', '', candidate))
                if candidate_text > text_length * 1.5:
                    main_content = candidate
                    extraction_method = "nuxt_data"

        if extraction_method in ("readability", "cache_proxy") and text_length < 500 and not fetch_failed:
            fallback_html = _fallback_fetch(url)
            if fallback_html:
                fb_content = extract_main_content(fallback_html)
                fb_headings = len(re.findall(r'<h[1-6][^>]*>', fb_content, re.IGNORECASE))
                fb_text = len(re.sub(r'<[^>]+>', '', fb_content))
                if fb_headings > heading_count and fb_text > text_length * 2:
                    main_content = fb_content
                    html = fallback_html
                    extraction_method = "cache_proxy"

    sections = parse_into_sections(main_content, url)

    nodes: list[dict] = []
    edges: list[dict] = []
    section_to_node_ids: dict[str, list[str]] = {}

    node_counter = 0

    for section in sections:
        section_node_ids = []

        if section.heading != "(root)":
            node_counter += 1
            heading_node_id = f"{url_hash}_n{node_counter:04d}"
            heading_node = GraphNode(
                id=heading_node_id, type="heading", content=section.heading,
                section_id=section.id, heading=section.heading,
                breadcrumb=section.breadcrumb, depth=section.depth,
                keywords=_extract_keywords(section.heading, " ".join(section.breadcrumb)),
                tokens=_estimate_tokens(section.heading),
            )
            nodes.append(heading_node.as_dict())
            section_node_ids.append(heading_node_id)

            if section.parent_id and section.parent_id in section_to_node_ids:
                parent_heading = section_to_node_ids[section.parent_id][0] if section_to_node_ids[section.parent_id] else None
                if parent_heading:
                    edges.append(GraphEdge(
                        source=parent_heading, target=heading_node_id,
                        type="contains", weight=1.0,
                    ).as_dict())

        for block_type, content, meta in section.content_blocks:
            if not content or len(content) < 15:
                continue

            node_counter += 1
            node_id = f"{url_hash}_n{node_counter:04d}"

            node = GraphNode(
                id=node_id, type=block_type, content=content[:800],
                section_id=section.id, heading=section.heading,
                breadcrumb=section.breadcrumb, depth=section.depth,
                keywords=_extract_keywords(content[:500], section.heading),
                tokens=_estimate_tokens(content[:800]),
            )
            nodes.append(node.as_dict())
            section_node_ids.append(node_id)

            if section_node_ids and section.heading != "(root)":
                heading_id = section_node_ids[0]
                edge_type = {
                    "code": "has_example", "parameter": "has_parameter",
                    "table": "has_table", "list": "has_list", "text": "describes",
                }.get(block_type, "contains")

                edges.append(GraphEdge(
                    source=heading_id, target=node_id,
                    type=edge_type,
                    weight=0.8 if block_type in ("code", "parameter") else 0.5,
                ).as_dict())

        content_ids = section_node_ids[1:]
        for i in range(len(content_ids) - 1):
            edges.append(GraphEdge(
                source=content_ids[i], target=content_ids[i + 1],
                type="sibling", weight=0.3,
            ).as_dict())

        section_to_node_ids[section.id] = section_node_ids

        for href, link_text in section.links:
            if href.startswith('#'):
                target_anchor = href[1:]
                for other_section in sections:
                    if _slugify(other_section.heading) == target_anchor or target_anchor in other_section.heading.lower().replace(' ', '-'):
                        if other_section.id in section_to_node_ids and section_to_node_ids[other_section.id]:
                            edges.append(GraphEdge(
                                source=section_node_ids[0] if section_node_ids else section.id,
                                target=section_to_node_ids[other_section.id][0],
                                type="see_also", weight=0.6,
                            ).as_dict())
                        break

    confidence = _assess_confidence(nodes, html)

    meta = _extract_meta(html)
    title = meta.get('title', '')
    if not title:
        for n in nodes:
            if n['type'] == 'heading' and n['depth'] <= 2:
                title = n['content']
                break
    if not title:
        title = url.split('/')[-1] or url

    type_counts = {}
    for n in nodes:
        type_counts[n['type']] = type_counts.get(n['type'], 0) + 1

    edge_type_counts = {}
    for e in edges:
        edge_type_counts[e['type']] = edge_type_counts.get(e['type'], 0) + 1

    citation_urls = _extract_citation_urls(html, url)

    graph = {
        "url": url,
        "url_hash": url_hash,
        "title": title,
        "fetched_at": time.time(),
        "extraction_method": extraction_method,
        "confidence": confidence,
        "citation_urls": citation_urls,
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "node_types": type_counts,
            "edge_types": edge_type_counts,
            "total_tokens_stored": sum(n.get('tokens', 0) for n in nodes),
            "raw_page_tokens": _estimate_tokens(html),
        },
    }

    cache_path.write_text(json.dumps(graph, indent=2, ensure_ascii=False))
    return graph


def _slugify(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def _extract_meta(html: str) -> dict:
    meta = {}
    for m in re.finditer(r'<meta[^>]*(?:name|property)="([^"]*)"[^>]*content="([^"]*)"', html):
        key = m.group(1).lower()
        val = _decode_entities(m.group(2))
        if key in ('description', 'og:description', 'og:title', 'twitter:description', 'title'):
            meta[key] = val
    title_m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL)
    if title_m:
        meta['title'] = _decode_entities(re.sub(r'<[^>]+>', '', title_m.group(1)).strip())
    return meta


# ═══════════════════════════════════════════════════════════════════════════════
# CONTEXTUAL BANDIT — learns which search strategies work for which queries
#
# Problem: given query q, choose search strategy s* from S that maximises
# retrieved content quality. No domain labels. Learns from observed reward.
#
# Algorithm: LinUCB (Li et al., 2010)
#   - Context x ∈ R^d : char-trigram hash → random projection (d=16, stdlib only)
#   - Arms: 6 domain-agnostic query reformulation templates
#   - Per-arm state: (A ∈ R^{d×d}, b ∈ R^d), A_0 = I, b_0 = 0
#   - Selection: a* = argmax_a [θ̂_a^T x + α √(x^T A_a^{-1} x)]
#   - Update (rank-1 Sherman-Morrison): A ← A + xx^T, b ← b + rx
#   - Domain affinity: Welford online mean/var per domain → re-rank DDG results
#   - Persistence: ~/.cache/webify/ml_state.json
# ═══════════════════════════════════════════════════════════════════════════════

_ML_STATE_PATH = CACHE_DIR / "ml_state.json"
_BANDIT_DIM = 16          # context vector dimension
_BANDIT_ALPHA = 1.0       # UCB exploration coefficient
_BANDIT_ARMS = [
    "{q}",                                        # baseline
    "{q} evidence research study",                # evidence-seeking
    "{q} mechanism explained how it works",       # mechanistic
    "{q} review analysis findings data",          # analytical
    "{q} history background context causes",      # contextual/historical
    "{q} guide tutorial examples",                # practical
]

# ── Deterministic random projection (fixed seed, no numpy) ────────────────────

def _lcg(seed: int, n: int) -> list[float]:
    """Linear congruential generator — deterministic, stdlib-only Gaussian approx."""
    vals = []
    s = seed & 0xFFFFFFFF
    for _ in range(n):
        s = (1664525 * s + 1013904223) & 0xFFFFFFFF
        # Box-Muller via two uniform samples
        u1 = s / 0xFFFFFFFF
        s = (1664525 * s + 1013904223) & 0xFFFFFFFF
        u2 = s / 0xFFFFFFFF
        u1 = max(u1, 1e-10)
        import math as _math
        z = _math.sqrt(-2 * _math.log(u1)) * _math.cos(2 * _math.pi * u2)
        vals.append(z / _math.sqrt(n))   # scale for unit norm projection
    return vals

def _build_projection_matrix(vocab_size: int, dim: int) -> list[list[float]]:
    """Fixed projection matrix R ∈ R^{vocab_size × dim}."""
    return [_lcg(i * 1000003 + 7, dim) for i in range(vocab_size)]

_TRIGRAM_VOCAB = 1000   # hash space
_PROJ_MATRIX: list[list[float]] | None = None

def _get_projection() -> list[list[float]]:
    global _PROJ_MATRIX
    if _PROJ_MATRIX is None:
        _PROJ_MATRIX = _build_projection_matrix(_TRIGRAM_VOCAB, _BANDIT_DIM)
    return _PROJ_MATRIX

def _query_to_context(query: str) -> list[float]:
    """
    query → unit-norm vector in R^{_BANDIT_DIM}.
    Method: char-trigram TF (hash to _TRIGRAM_VOCAB) → random projection → L2-norm.
    """
    q = query.lower()
    counts: dict[int, int] = {}
    for i in range(len(q) - 2):
        tri = q[i:i+3]
        h = (hash(tri) & 0x7FFFFFFF) % _TRIGRAM_VOCAB
        counts[h] = counts.get(h, 0) + 1

    total = max(sum(counts.values()), 1)
    proj = _get_projection()
    vec = [0.0] * _BANDIT_DIM
    for h, c in counts.items():
        tf = c / total
        for j in range(_BANDIT_DIM):
            vec[j] += tf * proj[h][j]

    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ── Matrix helpers (no numpy) ─────────────────────────────────────────────────

def _mat_identity(d: int) -> list[list[float]]:
    return [[1.0 if i == j else 0.0 for j in range(d)] for i in range(d)]

def _mat_vec(M: list[list[float]], v: list[float]) -> list[float]:
    return [sum(M[i][j] * v[j] for j in range(len(v))) for i in range(len(M))]

def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))

def _sherman_morrison_update(A_inv: list[list[float]], x: list[float]) -> list[list[float]]:
    """Rank-1 update: (A + xx^T)^{-1} = A^{-1} - (A^{-1}x)(A^{-1}x)^T / (1 + x^T A^{-1} x)"""
    Ax = _mat_vec(A_inv, x)
    denom = 1.0 + _dot(x, Ax)
    d = len(x)
    return [
        [A_inv[i][j] - Ax[i] * Ax[j] / denom for j in range(d)]
        for i in range(d)
    ]


# ── Persistent state ──────────────────────────────────────────────────────────

def _load_ml_state() -> dict:
    try:
        if _ML_STATE_PATH.exists():
            raw = json.loads(_ML_STATE_PATH.read_text())
            # Validate shape
            if (isinstance(raw.get("arms"), list) and
                    len(raw["arms"]) == len(_BANDIT_ARMS) and
                    len(raw["arms"][0].get("A_inv", [])) == _BANDIT_DIM):
                return raw
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        pass
    # Fresh state
    d = _BANDIT_DIM
    return {
        "arms": [
            {"A_inv": _mat_identity(d), "b": [0.0] * d, "n": 0}
            for _ in _BANDIT_ARMS
        ],
        "domain_stats": {},   # domain → {n, mean, M2}  (Welford)
        "total_pulls": 0,
    }

def _save_ml_state(state: dict) -> None:
    try:
        _ML_STATE_PATH.write_text(json.dumps(state))
    except OSError:
        pass

_ml_state_lock = threading.Lock()
_ml_state: dict | None = None

def _get_ml_state() -> dict:
    global _ml_state
    if _ml_state is None:
        _ml_state = _load_ml_state()
    return _ml_state


# ── LinUCB select & update ────────────────────────────────────────────────────

def _bandit_select(query: str) -> tuple[int, str]:
    """Return (arm_index, expanded_query) using LinUCB."""
    x = _query_to_context(query)
    state = _get_ml_state()
    arms = state["arms"]

    best_arm, best_score = 0, -1e9
    for i, arm_state in enumerate(arms):
        A_inv = arm_state["A_inv"]
        b = arm_state["b"]
        theta = _mat_vec(A_inv, b)
        exploit = _dot(theta, x)
        Ax = _mat_vec(A_inv, x)
        explore = _BANDIT_ALPHA * math.sqrt(max(_dot(x, Ax), 0.0))
        score = exploit + explore
        if score > best_score:
            best_score, best_arm = score, i

    template = _BANDIT_ARMS[best_arm]
    expanded = template.replace("{q}", query)
    return best_arm, expanded


def _bandit_update(arm_index: int, query: str, reward: float) -> None:
    """Update arm (arm_index) with observed reward ∈ [0,1]."""
    x = _query_to_context(query)
    with _ml_state_lock:
        state = _get_ml_state()
        arm = state["arms"][arm_index]
        arm["A_inv"] = _sherman_morrison_update(arm["A_inv"], x)
        arm["b"] = [arm["b"][j] + reward * x[j] for j in range(_BANDIT_DIM)]
        arm["n"] += 1
        state["total_pulls"] = state.get("total_pulls", 0) + 1
        _save_ml_state(state)


# ── Domain affinity — Welford online stats ────────────────────────────────────

def _domain_of(url: str) -> str:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or url
    # strip www. and use registrable domain (last 2 parts)
    parts = host.lstrip("www.").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host

def _update_domain_affinity(url: str, quality: float) -> None:
    domain = _domain_of(url)
    with _ml_state_lock:
        state = _get_ml_state()
        ds = state.setdefault("domain_stats", {})
        if domain not in ds:
            ds[domain] = {"n": 0, "mean": 0.0, "M2": 0.0}
        s = ds[domain]
        s["n"] += 1
        delta = quality - s["mean"]
        s["mean"] += delta / s["n"]
        s["M2"] += delta * (quality - s["mean"])
        _save_ml_state(state)

def _domain_affinity_score(url: str) -> float:
    """
    Returns estimated quality for this domain ∈ [0,1], with UCB uncertainty bonus.
    Falls back to 0.5 (neutral) when unseen.
    """
    domain = _domain_of(url)
    state = _get_ml_state()
    ds = state.get("domain_stats", {})
    if domain not in ds or ds[domain]["n"] == 0:
        return 0.5
    s = ds[domain]
    n = s["n"]
    mean = s["mean"]
    # UCB bonus: prefer less-observed domains slightly
    ucb_bonus = math.sqrt(2 * math.log(max(state.get("total_pulls", 1), 1)) / n)
    return min(mean + 0.1 * ucb_bonus, 1.0)


# ── Reward computation from retrieval result ──────────────────────────────────

def _compute_reward(source_results: list[dict]) -> float:
    """
    Composite reward ∈ [0,1] from parallel retrieval results.

    r = 0.4 * confidence_score
      + 0.4 * content_depth_score
      + 0.2 * top_bm25_score (normalised)
    """
    if not source_results:
        return 0.0

    conf_map = {"high": 1.0, "medium": 0.6, "low": 0.3, "none": 0.0}
    conf_scores = [conf_map.get(s.get("confidence", "none"), 0.0) for s in source_results]
    conf = sum(conf_scores) / len(conf_scores)

    # Depth: avg content length across all retrieved nodes, normalised at 400 chars
    all_nodes = [n for s in source_results for n in s.get("nodes", [])]
    if all_nodes:
        avg_len = sum(len(n.get("content", "")) for n in all_nodes) / len(all_nodes)
        depth = min(avg_len / 400.0, 1.0)
    else:
        depth = 0.0

    # Top BM25 score normalised at 30.0 (typical high-quality match)
    top_bm25 = max((n.get("_score", 0.0) for n in all_nodes), default=0.0)
    bm25 = min(top_bm25 / 30.0, 1.0)

    return 0.4 * conf + 0.4 * depth + 0.2 * bm25


# ═══════════════════════════════════════════════════════════════════════════════
# WEB SEARCH (DuckDuckGo HTML, no API key)
# ═══════════════════════════════════════════════════════════════════════════════

def _search_brave(query: str, max_results: int = 5) -> list[dict]:
    """Search via Brave Search API. Requires BRAVE_SEARCH_API_KEY env var."""
    import json as _json
    from urllib.parse import urlencode
    api_key = os.environ.get('BRAVE_SEARCH_API_KEY', '')
    if not api_key:
        return []
    try:
        params = urlencode({'q': query, 'count': max_results})
        req = Request(f'https://api.search.brave.com/res/v1/web/search?{params}', headers={
            'Accept': 'application/json',
            'Accept-Encoding': 'gzip',
            'X-Subscription-Token': api_key,
        })
        response = urlopen(req, timeout=10, context=ssl_ctx)
        raw = response.read()
        if response.headers.get('Content-Encoding') == 'gzip':
            import gzip
            raw = gzip.decompress(raw)
        data = _json.loads(raw.decode('utf-8'))
        results = []
        for item in data.get('web', {}).get('results', []):
            results.append({
                "url": item.get('url', ''),
                "title": item.get('title', ''),
                "snippet": item.get('description', ''),
            })
            if len(results) >= max_results:
                break
        return results
    except (HTTPError, URLError, OSError, ValueError):
        return []


def _search_ddg(query: str, max_results: int = 5) -> list[dict]:
    """Search DuckDuckGo Lite (POST) — no API key, may rate-limit."""
    from urllib.parse import urlencode, urlparse
    try:
        data = urlencode({'q': query, 'kl': 'us-en'}).encode()
        req = Request('https://lite.duckduckgo.com/lite/', data=data, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': 'https://lite.duckduckgo.com/',
            'Origin': 'https://lite.duckduckgo.com',
        })
        response = urlopen(req, timeout=10, context=ssl_ctx)
        html = response.read(300_000).decode('utf-8', errors='ignore')
    except (HTTPError, URLError, OSError):
        return []

    results = []
    for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>([^<]+)</a>', html):
        raw_url = _decode_entities(m.group(1))
        title = _decode_entities(m.group(2)).strip()
        if not title or len(title) < 5:
            continue
        parsed = urlparse(raw_url)
        if 'duckduckgo.com' in parsed.netloc or 'duck.com' in parsed.netloc:
            continue
        results.append({"url": raw_url, "title": title, "snippet": ""})
        if len(results) >= max_results:
            break

    return results


def search_web(query: str, max_results: int = 5) -> list[dict]:
    """Search the web. Uses Brave API if key is set, falls back to DDG Lite."""
    results = _search_brave(query, max_results)
    if results:
        return results
    return _search_ddg(query, max_results)


# ═══════════════════════════════════════════════════════════════════════════════
# BM25 SCORING (replaces ad-hoc _score_node)
# ═══════════════════════════════════════════════════════════════════════════════

_NAV_HEADING_PATTERNS = re.compile(
    r'^(overview|quickstart|get started|introduction|welcome|contents|'
    r'table of contents|navigation|menu|sidebar|home|back|next|previous|'
    r'on this page|in this section|see also|related|more|resources|'
    r'documentation|docs|guide|tutorial|reference|api|changelog|'
    r'community|support|contact|about|faq|search|login|sign in|sign up)$',
    re.IGNORECASE,
)

# Primary-source URL patterns for citation chasing
_PRIMARY_SOURCE_PATTERNS = re.compile(
    r'(?:doi\.org/10\.|'
    r'pubmed\.ncbi\.nlm\.nih\.gov/\d|'
    r'ncbi\.nlm\.nih\.gov/pmc/articles/|'
    r'nejm\.org/doi/|bmj\.com/content/|thelancet\.com/journals/|'
    r'nature\.com/articles/|science\.org/doi/|cell\.com/[a-z-]+/fulltext/|'
    r'pnas\.org/doi/|jamanetwork\.com/journals/|'
    r'frontiersin\.org/articles/|journals\.plos\.org/plosone/article|'
    r'nih\.gov/news-events/|who\.int/news-room/|cdc\.gov/[a-z].+/[a-z]|'
    r'arxiv\.org/abs/\d|biorxiv\.org/content/|'
    r'(?:harvard|stanford|mit|oxford|cambridge)\.edu/research/|'
    r'(?:harvard|stanford|mit|oxford|cambridge)\.edu/[a-z].+/[a-z].+/)',
    re.IGNORECASE,
)


def _extract_citation_urls(html: str, base_url: str) -> list[str]:
    """Extract primary-source URLs (journals, DOI, NIH, .edu) from raw page HTML."""
    from urllib.parse import urljoin, urlparse
    base_host = urlparse(base_url).hostname or ''
    found: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a[^>]+href="([^"#][^"]*)"', html, re.IGNORECASE):
        href = m.group(1).strip()
        if not href:
            continue
        if href.startswith('http'):
            url = href
        elif href.startswith('//'):
            url = 'https:' + href
        else:
            try:
                url = urljoin(base_url, href)
            except Exception:
                continue
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            continue
        if (parsed.hostname or '') == base_host:
            continue
        if _PRIMARY_SOURCE_PATTERNS.search(url) and url not in seen:
            seen.add(url)
            found.append(url)
        if len(found) >= 8:
            break
    return found[:5]

def _is_nav_node(node: dict) -> bool:
    """True if this node is navigation/chrome, not substantive content."""
    content = node.get("content", "")
    heading = node.get("heading", "")
    breadcrumb = node.get("breadcrumb", [])
    ntype = node.get("type", "")

    # Short heading-only nodes at depth ≤ 1 with no real content
    if ntype == "heading" and len(content) < 60 and len(breadcrumb) <= 1:
        return True

    # Heading matches known nav labels exactly
    if ntype == "heading" and _NAV_HEADING_PATTERNS.match(content.strip()):
        return True

    # List nodes that are pure link collections (short items, many of them)
    if ntype == "list":
        items = [l for l in content.split("\n") if l.strip().startswith("- ")]
        if items and sum(len(i) for i in items) / max(len(items), 1) < 25:
            return True

    # Text nodes that are just a restatement of nav link text (very short)
    if ntype == "text" and len(content) < 80 and len(breadcrumb) <= 1:
        return True

    return False


def _build_idf(nodes: list[dict]) -> dict[str, float]:
    """Compute IDF across substantive nodes only — nav nodes skew IDF badly."""
    content_nodes = [n for n in nodes if not _is_nav_node(n)]
    N = max(len(content_nodes), 1)
    df: dict[str, int] = {}
    for node in content_nodes:
        field = f"{node.get('content','')} {node.get('heading','')} {' '.join(node.get('breadcrumb',[]))}"
        seen_terms: set[str] = set()
        for term in re.findall(r'\w{3,}', field.lower()):
            if term not in seen_terms:
                df[term] = df.get(term, 0) + 1
                seen_terms.add(term)
    idf: dict[str, float] = {}
    for term, freq in df.items():
        idf[term] = math.log((N - freq + 0.5) / (freq + 0.5) + 1)
    return idf


def _bm25_score(node: dict, query_terms: list[str], idf: dict[str, float],
                avg_len: float, query_lower: str) -> float:
    k1, b = 1.5, 0.75

    # Field weights: content, heading (3×), breadcrumb (2×)
    content = node.get("content", "").lower()
    heading = node.get("heading", "").lower()
    breadcrumb = " ".join(node.get("breadcrumb", [])).lower()

    doc_len = len(content.split())
    score = 0.0

    for term in query_terms:
        term_idf = idf.get(term, 0.0)
        if term_idf <= 0:
            continue

        # Content BM25
        tf_c = content.count(term)
        if tf_c:
            norm = tf_c * (k1 + 1) / (tf_c + k1 * (1 - b + b * doc_len / max(avg_len, 1)))
            score += term_idf * norm

        # Heading bonus (3×) — short field, no length norm
        if term in heading:
            score += term_idf * 3.0

        # Breadcrumb bonus (2×)
        if term in breadcrumb:
            score += term_idf * 2.0

    # Exact phrase match in any field
    if query_lower in content or query_lower in heading:
        score += 10.0

    # Type bonus: code and parameters are high-signal
    type_bonus = {"code": 3.0, "parameter": 3.0, "table": 2.0, "list": 1.5, "heading": 0.5}
    score += type_bonus.get(node.get("type", ""), 0.0)

    return score


# ═══════════════════════════════════════════════════════════════════════════════
# GRAPH RETRIEVAL — subtree-anchored (directional, not BFS scatter)
# ═══════════════════════════════════════════════════════════════════════════════

def retrieve(url: str, query: str, max_results: int = 5) -> list[dict]:
    graph = build_graph(url)
    nodes = graph["nodes"]
    edges = graph["edges"]

    if not nodes:
        return []

    # Build adjacency: forward (parent→child) edges only for subtree collection
    children: dict[str, list[tuple[str, str]]] = {}   # parent_id → [(child_id, etype)]
    for e in edges:
        etype = e["type"]
        if etype in ("contains", "has_example", "has_parameter", "has_table", "has_list", "describes"):
            children.setdefault(e["source"], []).append((e["target"], etype))

    # BM25 setup
    avg_len = sum(len(n.get("content", "").split()) for n in nodes) / max(len(nodes), 1)
    idf = _build_idf(nodes)
    query_lower = query.lower()
    query_terms = re.findall(r'\w{3,}', query_lower)

    node_map = {n["id"]: n for n in nodes}

    # Score every substantive node — nav nodes excluded
    node_scores: dict[str, float] = {}
    for node in nodes:
        if _is_nav_node(node):
            continue
        s = _bm25_score(node, query_terms, idf, avg_len, query_lower)
        if s > 0:
            node_scores[node["id"]] = s

    if not node_scores:
        for node in nodes:
            s = _bm25_score(node, query_terms, idf, avg_len, query_lower)
            if s > 0:
                node_scores[node["id"]] = s

    if not node_scores:
        return []

    # Score every non-nav heading by aggregate (self + children)
    heading_nodes = [n for n in nodes if n["type"] == "heading" and not _is_nav_node(n)]
    anchor_scores: list[tuple[float, str]] = []
    for hn in heading_nodes:
        agg = node_scores.get(hn["id"], 0.0)
        for child_id, _ in children.get(hn["id"], []):
            agg += node_scores.get(child_id, 0.0) * 0.6
        if agg > 0:
            anchor_scores.append((agg, hn["id"]))
    anchor_scores.sort(reverse=True)

    # Take top-3 non-overlapping anchors so we cover multiple sections per page
    seen_content: set[str] = set()
    results: list[dict] = []

    def _add(nid: str, score: float):
        if len(results) >= max_results:
            return
        node = node_map.get(nid)
        if not node:
            return
        content = node.get("content", "")
        if not content or len(content) < 10:
            return
        ch = _content_hash(content[:200])
        if ch in seen_content:
            return
        seen_content.add(ch)
        results.append({**node, "_score": score})

    subtree_budget = max_results * 2  # nodes per anchor
    used_subtrees: set[str] = set()
    for _, anchor_id in anchor_scores[:3]:
        subtree_ids: list[str] = []
        _collect_subtree(anchor_id, children, subtree_ids, max_nodes=subtree_budget,
                         _visited=set(used_subtrees))
        for nid in subtree_ids:
            used_subtrees.add(nid)
            _add(nid, node_scores.get(nid, 0.0))

    # Fill any remaining slots with highest-scored nodes not yet included
    if len(results) < max_results:
        for nid, score in sorted(node_scores.items(), key=lambda x: -x[1]):
            if len(results) >= max_results:
                break
            _add(nid, score)

    return results


def _collect_subtree(
    node_id: str,
    children: dict[str, list[tuple[str, str]]],
    out: list[str],
    max_nodes: int,
    _visited: Optional[set] = None,
):
    if _visited is None:
        _visited = set()
    if node_id in _visited or len(out) >= max_nodes:
        return
    _visited.add(node_id)
    out.append(node_id)
    # Depth-first in document order; prioritise high-signal edge types
    priority = {"has_example": 0, "has_parameter": 1, "has_table": 2,
                "has_list": 3, "describes": 4, "contains": 5}
    kids = sorted(children.get(node_id, []), key=lambda x: priority.get(x[1], 9))
    for child_id, _ in kids:
        _collect_subtree(child_id, children, out, max_nodes, _visited)


# ═══════════════════════════════════════════════════════════════════════════════
# HIGH-LEVEL API
# ═══════════════════════════════════════════════════════════════════════════════

def lookup(url: str, query: str, max_results: int = 3) -> str:
    results = retrieve(url, query, max_results)

    if not results:
        return f"No relevant content found for '{query}' in {url}"

    output = []
    total_tokens = 0
    for node in results:
        content = node.get("content", "")
        total_tokens += _estimate_tokens(content)
        breadcrumb = " > ".join(node.get("breadcrumb", []))
        node_type = node["type"]
        output.append(f"[{node_type}] {breadcrumb}\n{content}")

    header = f"# Webify: {len(results)} nodes, ~{total_tokens} tokens\n\n"
    return header + "\n\n---\n\n".join(output)


def smart_lookup(url: str, query: str, max_results: int = 3, force_refresh: bool = False) -> dict:
    """
    Intelligent lookup with confidence-aware fallback.

    Returns:
        status: "success" | "low_confidence" | "fallback_needed"
        content: The retrieved content (relevant sections only).
        confidence: Graph quality assessment.
        tokens_used: How many tokens the response costs.
        fallback_reason: Why to use WebFetch instead (if fallback_needed).
    """
    try:
        graph = build_graph(url, force_refresh=force_refresh)
    except (HTTPError, URLError, OSError) as e:
        return {
            "status": "fallback_needed",
            "content": "",
            "confidence": {"level": "none", "score": 0},
            "method": "failed",
            "tokens_used": 0,
            "fallback_reason": f"fetch failed: {e}",
        }

    confidence = graph.get("confidence", {})
    conf_level = confidence.get("level", "none")

    if conf_level == "none":
        return {
            "status": "fallback_needed",
            "content": "",
            "confidence": confidence,
            "method": graph.get("extraction_method", "unknown"),
            "tokens_used": 0,
            "fallback_reason": confidence.get("reason", "no content extracted"),
        }

    results = retrieve(url, query, max_results)

    if not results:
        if conf_level == "low":
            return {
                "status": "fallback_needed",
                "content": "",
                "confidence": confidence,
                "method": graph.get("extraction_method", "unknown"),
                "tokens_used": 0,
                "fallback_reason": "no matching nodes in low-confidence graph",
            }
        return {
            "status": "low_confidence",
            "content": f"No relevant content found for '{query}' — graph has {graph['stats']['total_nodes']} nodes but none matched.",
            "confidence": confidence,
            "method": graph.get("extraction_method", "unknown"),
            "tokens_used": 0,
            "fallback_reason": "",
        }

    total_tokens = sum(_estimate_tokens(n.get("content", "")) for n in results)
    avg_content = sum(len(n.get("content", "")) for n in results) / len(results)

    if avg_content < 30 and conf_level in ("low", "medium"):
        return {
            "status": "low_confidence",
            "content": lookup(url, query, max_results),
            "confidence": confidence,
            "method": graph.get("extraction_method", "unknown"),
            "tokens_used": total_tokens,
            "fallback_reason": "results are thin, consider WebFetch for richer output",
        }

    output = []
    for node in results:
        content = node.get("content", "")
        breadcrumb = " > ".join(node.get("breadcrumb", []))
        node_type = node["type"]
        output.append(f"[{node_type}] {breadcrumb}\n{content}")

    status = "success" if conf_level == "high" else "low_confidence"

    return {
        "status": status,
        "content": "\n\n---\n\n".join(output),
        "confidence": confidence,
        "method": graph.get("extraction_method", "unknown"),
        "tokens_used": total_tokens,
        "fallback_reason": "" if status == "success" else "medium confidence — results may be incomplete",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MULTI-SOURCE SEARCH (search → parallel graph builds → merge)
# ═══════════════════════════════════════════════════════════════════════════════

def _synthesize(query: str, fragments: str, sources: list[dict]) -> str:
    """
    One Haiku call over pre-filtered graph fragments → synthesized answer.

    Haiku over 3k pre-filtered tokens costs ~$0.003 and produces synthesis
    quality matching Sonnet over 50k raw tokens — because noise is already gone.
    Falls back to raw fragments if the API key is missing or the call fails.
    """
    import os
    import json as _json
    from urllib.request import Request as _Req, urlopen as _urlopen
    from urllib.error import URLError, HTTPError

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return fragments  # graceful fallback: no synthesis without key

    source_list = "\n".join(f"- {s['title']} ({s['url']})" for s in sources[:8])
    complexity = _query_complexity(query)
    depth_instruction = ""
    if complexity >= 2:
        depth_instruction = (
            "- This is a multi-dimensional question — cover EVERY aspect asked about in depth\n"
            "- For comparisons: address each item across each dimension explicitly with specifics\n"
            "- Prefer concrete data (numbers, benchmarks, versions) over vague summaries\n"
            "- Be thorough — aim for a complete reference answer, not a brief summary\n"
            "- Include implementation details, edge cases, and practical recommendations\n"
        )
    prompt = (
        f"You are a research synthesizer. Using ONLY the source fragments below, "
        f"answer the question precisely and completely.\n\n"
        f"QUESTION: {query}\n\n"
        f"SOURCES USED:\n{source_list}\n\n"
        f"FRAGMENTS:\n{fragments}\n\n"
        f"Write a structured answer that:\n"
        f"- Covers all key mechanisms, findings, and evidence from the fragments\n"
        f"- Includes specific numbers, study names, or effect sizes where present\n"
        f"- Notes caveats and limitations explicitly\n"
        f"- Cites which source each major claim comes from\n"
        f"- Uses markdown headers and bullet points for clarity\n"
        f"{depth_instruction}"
        f"Do not add information not present in the fragments."
    )

    max_tokens = {1: 1500, 2: 3000, 3: 4000}[complexity]
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        req = _Req(
            "https://api.anthropic.com/v1/messages",
            data=_json.dumps(payload).encode(),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        resp = _urlopen(req, timeout=30, context=ssl_ctx)
        data = _json.loads(resp.read().decode())
        return data["content"][0]["text"]
    except (HTTPError, URLError, OSError, KeyError, IndexError):
        return fragments  # fallback to raw fragments on any error


def _query_complexity(query: str) -> int:
    """
    Estimate query complexity (1-3) from surface signals.
    Multi-dimensional queries need more sources for completeness.
    """
    q = query.lower()
    score = 1

    # Comparison/vs queries need multiple perspectives
    compare_signals = ["compare", " vs ", " versus ", "difference between", "pros and cons",
                       "tradeoff", "trade-off", "advantages", "which is better"]
    if any(s in q for s in compare_signals):
        score += 1

    # Multi-entity: counting distinct capitalized entities or comma-separated items
    entities = re.findall(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*', query)
    if len(set(entities)) >= 3:
        score += 1

    # Long queries with multiple sub-questions (em dashes, commas, "and")
    parts = re.split(r'[,—–]| and | \+ ', query)
    if len(parts) >= 3:
        score = max(score, 2)
    if len(parts) >= 5:
        score = 3

    # Multi-hop causal chains ("from X through Y to Z", "causes → effects → response")
    chain_signals = ["from .+ through .+ to", "causes .+ effects", "chain",
                     "from .+ to .+ to"]
    if any(re.search(s, q) for s in chain_signals):
        score = max(score, 2)

    # Technical depth markers
    depth_signals = ["how does", "mechanism", "explain", "architecture", "implementation"]
    if any(s in q for s in depth_signals) and len(query) > 80:
        score = max(score, 2)

    # Length alone signals multi-part question
    if len(query) > 100:
        score = max(score, 2)

    return min(score, 3)


def _decompose_aspects(query: str) -> list[str]:
    """
    Split a multi-dimensional query into sub-aspects for independent retrieval.
    Returns the full query + distinct sub-queries. Zero cost — pure string logic.

    Each sub-aspect must be a viable BM25 query: subject + aspect keywords.
    For broad queries without clear delimiters, extracts noun-phrase clusters.
    """
    q_lower = query.lower()

    # Extract the subject/topic from the query stem
    subject_match = re.match(
        r'(?:what (?:is|are)|how (?:does|do)|explain|compare|is)\s+(.+?)(?:\s*[—–?]|$)',
        q_lower
    )
    subject = subject_match.group(1).strip() if subject_match else ""
    # Trim subject: keep up to first delimiter or "and which/and how/and what"
    subject = re.split(r'\band (?:which|how|what|who|where)\b', subject)[0].strip()
    subject_words = subject.split()
    if len(subject_words) > 8:
        subject = " ".join(subject_words[:8])

    # Split on delimiters that separate aspects
    parts = re.split(r'[,—–]', query)
    parts = [p.strip() for p in parts if len(p.strip()) > 8]

    aspects = [query]  # full query is always primary

    if len(parts) > 1:
        for part in parts[1:]:
            part_clean = part.strip().rstrip('?')
            if len(part_clean.split()) < 2:
                continue
            # Ensure each aspect has enough context to be a good BM25 query
            if subject and len(part_clean.split()) < 4:
                aspect = f"{subject} {part_clean}"
            elif subject:
                aspect = f"{subject} {part_clean}"
            else:
                aspect = part_clean
            aspects.append(aspect)
    elif len(query) > 80:
        # Broad query without delimiters — extract "and which/and how/and what" sub-questions
        sub_qs = re.split(r'\band (?:which|how|what|who|where|when)\b', query, flags=re.IGNORECASE)
        if len(sub_qs) >= 2 and subject:
            for part in sub_qs[1:]:
                part_clean = part.strip().rstrip('?')
                if len(part_clean.split()) >= 3:
                    aspects.append(f"{subject} {part_clean}")

    return aspects[:5]


def web_find(query: str, num_sources: int = 0, max_results_per_source: int = 0,
             synthesize: bool = True) -> dict:
    """
    Search the web for a query, fetch top sources in parallel, merge best nodes.

    Unlike DeepSearch (100 agents × full pages), this:
    - Fetches only structurally relevant subtrees (no full-page reads)
    - Drops low-confidence sources before reading them
    - Returns complete relevant content — never truncates for token savings
    - Adaptively scales sources based on query complexity

    Returns:
        status: "success" | "partial" | "no_results"
        content: Merged ranked nodes with source attribution
        sources: [{url, title, confidence}]
        tokens_used: Total tokens across all sources
        search_results: Raw DDG results for transparency
    """
    # Adaptive: scale sources to query complexity if not explicitly set
    complexity = _query_complexity(query)
    if num_sources <= 0:
        num_sources = {1: 3, 2: 5, 3: 6}[complexity]
    if max_results_per_source <= 0:
        max_results_per_source = {1: 5, 2: 7, 3: 9}[complexity]

    # No hard cap — the model can request as many sources as it needs.
    # Practical limit is DDG result count per search (~20-30 max).
    # For broader coverage, the model should call web_find multiple times
    # with different sub-queries (like deep-research spawns sub-agents).

    # LinUCB: choose search strategy based on query context
    arm_index, expanded_query = _bandit_select(query)

    search_hits = search_web(expanded_query, max_results=num_sources * 2)
    if not search_hits:
        # Fallback: retry with base query if expanded got nothing
        search_hits = search_web(query, max_results=num_sources * 2)
    if not search_hits:
        return {
            "status": "no_results",
            "content": f"No search results for '{query}'",
            "sources": [], "tokens_used": 0, "search_results": [],
        }

    # Re-rank DDG results using learned domain affinity scores
    for h in search_hits:
        h["_affinity"] = _domain_affinity_score(h["url"])
    search_hits.sort(key=lambda h: -h["_affinity"])

    candidate_urls = [h["url"] for h in search_hits[:num_sources * 2]]

    # Parallel graph builds — one thread per URL, timeout-guarded
    results_lock = threading.Lock()
    source_results: list[dict] = []

    citation_urls_lock = threading.Lock()
    citation_urls_found: list[tuple[str, str]] = []  # [(url, parent_title)]

    # Decompose query into aspects for broader retrieval coverage
    aspects = _decompose_aspects(query) if complexity >= 2 else [query]

    def _fetch_source(url: str, title: str, collect_citations: bool = False):
        try:
            graph = build_graph(url)
            conf = graph.get("confidence", {})
            if conf.get("level") == "none":
                return

            # Multi-aspect retrieval: retrieve for each aspect, merge unique nodes
            seen_ids: set[str] = set()
            all_nodes: list[dict] = []
            per_aspect = max(3, max_results_per_source // len(aspects))

            for aspect in aspects:
                nodes = retrieve(url, aspect, max_results=per_aspect)
                for n in nodes:
                    if n["id"] not in seen_ids:
                        seen_ids.add(n["id"])
                        all_nodes.append(n)

            if not all_nodes:
                return
            with results_lock:
                source_results.append({
                    "url": url, "title": title,
                    "confidence": conf.get("level", "low"),
                    "nodes": all_nodes[:max_results_per_source * 2],
                })
            if collect_citations:
                cites = graph.get("citation_urls", [])
                with citation_urls_lock:
                    for cu in cites[:2]:
                        citation_urls_found.append((cu, title))
        except Exception:
            pass

    threads = []
    url_to_title = {h["url"]: h["title"] for h in search_hits}
    for url in candidate_urls[:num_sources * 2]:
        t = threading.Thread(
            target=_fetch_source,
            args=(url, url_to_title.get(url, url), True),  # collect citations
            daemon=True,
        )
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=25)

    # Wave 2: chase up to 4 primary-source citations found in summary pages
    already_fetched = set(candidate_urls[:num_sources * 2])
    cite_threads = []
    for cite_url, parent_title in citation_urls_found[:4]:
        if cite_url in already_fetched:
            continue
        already_fetched.add(cite_url)
        cite_title = f"[primary source via {parent_title[:30]}]"
        t = threading.Thread(
            target=_fetch_source,
            args=(cite_url, cite_title, False),
            daemon=True,
        )
        t.start()
        cite_threads.append(t)
    for t in cite_threads:
        t.join(timeout=20)

    if not source_results:
        return {
            "status": "no_results",
            "content": f"Sources found but none had extractable content for '{query}'",
            "sources": [], "tokens_used": 0,
            "search_results": [{"url": h["url"], "title": h["title"]} for h in search_hits],
        }

    # Compute reward and update bandit + domain affinity models
    reward = _compute_reward(source_results)
    _bandit_update(arm_index, query, reward)
    for src in source_results:
        conf_map = {"high": 1.0, "medium": 0.6, "low": 0.3, "none": 0.0}
        src_quality = conf_map.get(src.get("confidence", "none"), 0.0)
        _update_domain_affinity(src["url"], src_quality)

    # Rank sources: high-confidence first, then by top node score
    source_results.sort(key=lambda s: (
        {"high": 0, "medium": 1, "low": 2}.get(s["confidence"], 3),
        -max((n.get("_score", 0) for n in s["nodes"]), default=0)
    ))

    # Merge: include all relevant nodes, dedup identical content across sources
    seen_content: set[str] = set()
    merged_nodes: list[dict] = []
    tokens_used = 0
    sources_used: list[dict] = []

    for src in source_results:
        src_tokens = 0
        src_added = False
        for node in src["nodes"]:
            content = node.get("content", "")
            ch = _content_hash(content[:200])
            if ch in seen_content:
                continue
            seen_content.add(ch)
            merged_nodes.append({**node, "_source_url": src["url"], "_source_title": src["title"]})
            node_tokens = _estimate_tokens(content)
            tokens_used += node_tokens
            src_tokens += node_tokens
            src_added = True
        if src_added:
            sources_used.append({
                "url": src["url"], "title": src["title"],
                "confidence": src["confidence"], "tokens": src_tokens,
            })

    if not merged_nodes:
        return {
            "status": "no_results",
            "content": "No relevant content extracted from sources",
            "sources": [], "tokens_used": 0,
            "search_results": [{"url": h["url"], "title": h["title"]} for h in search_hits],
        }

    # Format raw fragments — source-attributed
    parts = []
    for node in merged_nodes:
        breadcrumb = " > ".join(node.get("breadcrumb", []))
        src_title = node.get("_source_title", node.get("_source_url", ""))
        node_type = node["type"]
        parts.append(f"[{node_type}] {src_title} | {breadcrumb}\n{node.get('content', '')}")

    raw_content = "\n\n---\n\n".join(parts)
    status = "success" if len(sources_used) >= 1 else "partial"

    if not synthesize:
        return {
            "status": status,
            "content": raw_content,
            "sources": sources_used,
            "tokens_used": tokens_used,
            "search_results": [{"url": h["url"], "title": h["title"]} for h in search_hits],
            "_ml": {"arm": arm_index, "strategy": _BANDIT_ARMS[arm_index], "reward": round(reward, 3)},
        }

    # Synthesis: one Haiku call over pre-filtered fragments → structured answer
    synthesized = _synthesize(query, raw_content, sources_used)
    synth_tokens = _estimate_tokens(synthesized)

    return {
        "status": status,
        "content": synthesized,
        "raw_fragments": raw_content,
        "sources": sources_used,
        "tokens_used": synth_tokens,
        "fragment_tokens": tokens_used,
        "search_results": [{"url": h["url"], "title": h["title"]} for h in search_hits],
        "_ml": {"arm": arm_index, "strategy": _BANDIT_ARMS[arm_index], "reward": round(reward, 3)},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python webify.py build <url>            # Build graph")
        print("  python webify.py lookup <url> <query>   # Retrieve via graph")
        print("  python webify.py stats <url>            # Graph statistics")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "build":
        url = sys.argv[2]
        graph = build_graph(url, force_refresh=True)
        print(f"Built graph: {graph['title'][:60]}")
        print(f"  Nodes: {graph['stats']['total_nodes']}")
        print(f"  Edges: {graph['stats']['total_edges']}")
        print(f"  Stored: {graph['stats']['total_tokens_stored']} tokens")
        print(f"  Raw: {graph['stats']['raw_page_tokens']} tokens")
        ratio = graph['stats']['raw_page_tokens'] / max(graph['stats']['total_tokens_stored'], 1)
        print(f"  Compression: {ratio:.1f}x")

    elif cmd == "lookup":
        url = sys.argv[2]
        query = ' '.join(sys.argv[3:])
        print(lookup(url, query))

    elif cmd == "stats":
        url = sys.argv[2]
        graph = build_graph(url)
        print(json.dumps(graph["stats"], indent=2))

    elif cmd == "find":
        query = ' '.join(sys.argv[2:])
        result = web_find(query)
        print(f"Status: {result['status']}  |  {result['tokens_used']} tokens")
        print(f"Sources: {len(result['sources'])}")
        for s in result['sources']:
            print(f"  [{s['confidence']}] {s['title']} ({s['tokens']} tok) — {s['url']}")
        print()
        print(result['content'])

    elif cmd == "search":
        query = ' '.join(sys.argv[2:])
        hits = search_web(query)
        for h in hits:
            print(f"{h['url']}")
            if h.get('snippet'):
                print(f"  {h['snippet'][:80]}")

    else:
        print(f"Unknown command: {cmd}")
