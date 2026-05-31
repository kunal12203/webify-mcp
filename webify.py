"""
Webify v1.0: Semantic Web Graph for Claude Code

Builds a semantic graph from any web page, then retrieves only the relevant
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
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import ssl

CACHE_DIR = Path(os.environ.get("WEBIFY_CACHE_DIR", Path.home() / ".cache" / "webify"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

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
    return len(text) // 4


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

def fetch_page(url: str) -> tuple[str, str]:
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


def _extract_json_ld(html: str) -> Optional[str]:
    matches = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not matches:
        return None
    parts = []
    for raw in matches:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for key in ('articleBody', 'description', 'text', 'content', 'mainEntity'):
                    if key in data and isinstance(data[key], str) and len(data[key]) > 100:
                        parts.append(data[key])
                if 'mainEntity' in data and isinstance(data['mainEntity'], list):
                    for entity in data['mainEntity'][:20]:
                        if isinstance(entity, dict):
                            q = entity.get('name', '')
                            a = entity.get('acceptedAnswer', {})
                            if isinstance(a, dict):
                                a = a.get('text', '')
                            if q and a:
                                parts.append(f"## {q}\n{a}")
        except (json.JSONDecodeError, TypeError):
            continue
    return "\n\n".join(parts) if parts else None


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

    graph = {
        "url": url,
        "url_hash": url_hash,
        "title": title,
        "fetched_at": time.time(),
        "extraction_method": extraction_method,
        "confidence": confidence,
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
# GRAPH RETRIEVAL (BFS from best match + edge traversal)
# ═══════════════════════════════════════════════════════════════════════════════

def retrieve(url: str, query: str, max_results: int = 5) -> list[dict]:
    graph = build_graph(url)
    nodes = graph["nodes"]
    edges = graph["edges"]

    if not nodes:
        return []

    adj: dict[str, list[tuple[str, str, float]]] = {}
    for e in edges:
        src, tgt, etype, w = e["source"], e["target"], e["type"], e.get("weight", 1.0)
        adj.setdefault(src, []).append((tgt, etype, w))
        adj.setdefault(tgt, []).append((src, etype, w))

    query_lower = query.lower()
    query_terms = set(re.findall(r'\w{3,}', query_lower))
    node_scores: dict[str, float] = {}

    for node in nodes:
        score = _score_node(node, query_terms, query_lower)
        if score > 0:
            node_scores[node["id"]] = score

    if not node_scores:
        for node in nodes:
            keywords = set(node.get("keywords", []))
            overlap = query_terms & keywords
            if overlap:
                node_scores[node["id"]] = len(overlap)

    if not node_scores:
        return []

    sorted_seeds = sorted(node_scores.items(), key=lambda x: -x[1])
    seeds = [s[0] for s in sorted_seeds[:3]]

    visited = set()
    cluster_scores: dict[str, float] = {}

    for seed_id in seeds:
        seed_score = node_scores.get(seed_id, 0)
        _bfs_collect(seed_id, adj, node_scores, cluster_scores, visited, seed_score, max_depth=2)

    for nid, score in node_scores.items():
        if nid not in cluster_scores:
            cluster_scores[nid] = score * 0.5

    ranked = sorted(cluster_scores.items(), key=lambda x: -x[1])

    node_map = {n["id"]: n for n in nodes}
    results = []
    seen_content = set()

    for nid, score in ranked:
        if nid not in node_map:
            continue
        node = node_map[nid]

        content = node.get("content", "")
        if not content or len(content) < 10:
            continue

        c_hash = _content_hash(content[:200])
        if c_hash in seen_content:
            continue
        seen_content.add(c_hash)

        results.append({**node, "_score": score})
        if len(results) >= max_results:
            break

    return results


def _score_node(node: dict, query_terms: set, query_lower: str) -> float:
    score = 0.0
    content_lower = node.get("content", "").lower()
    heading_lower = node.get("heading", "").lower()
    keywords = set(node.get("keywords", []))
    breadcrumb_lower = " ".join(node.get("breadcrumb", [])).lower()

    for term in query_terms:
        tf = content_lower.count(term)
        if tf > 0:
            score += (tf * 2.5) / (tf + 1.5)

        if term in heading_lower:
            score += 4.0
        if term in breadcrumb_lower:
            score += 2.0

    overlap = query_terms & keywords
    score += len(overlap) * 2.0

    if query_lower in content_lower:
        score += 8.0

    type_bonus = {
        "code": 3.0, "parameter": 3.0, "table": 2.0,
        "list": 1.0, "heading": 0.0, "text": -0.5,
    }
    score += type_bonus.get(node.get("type", ""), 0)

    tokens = node.get("tokens", 0)
    if tokens > 50:
        score += 1.0
    if tokens > 150:
        score += 1.0

    return score


def _bfs_collect(
    start_id: str, adj: dict, node_scores: dict,
    cluster_scores: dict, visited: set, seed_score: float, max_depth: int = 2,
):
    queue = [(start_id, 0, seed_score)]

    while queue:
        nid, depth, propagated_score = queue.pop(0)

        if nid in visited or depth > max_depth:
            continue
        visited.add(nid)

        own_score = node_scores.get(nid, 0)
        final_score = own_score + propagated_score * (0.5 ** depth)
        cluster_scores[nid] = max(cluster_scores.get(nid, 0), final_score)

        for neighbor_id, edge_type, weight in adj.get(nid, []):
            if neighbor_id not in visited:
                edge_boost = {
                    "has_example": 1.5, "has_parameter": 1.3,
                    "contains": 1.0, "see_also": 0.8,
                    "has_table": 0.7, "has_list": 0.6,
                    "sibling": 0.4, "describes": 0.5,
                }.get(edge_type, 0.5)

                next_score = propagated_score * weight * edge_boost
                queue.append((neighbor_id, depth + 1, next_score))


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

    else:
        print(f"Unknown command: {cmd}")
