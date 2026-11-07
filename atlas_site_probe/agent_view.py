"""Agent View: read a store the way machines read it.

Every expensive problem on a small store looks fine in a browser and is wrong to
the machine reading it: a feed, an AI shopping agent, a crawler, a structured
data parser. Agent View records what each of those actually receives, in order,
and reports the first 160 characters of it as "reads first".

Two layers, like probe.py:

  capture_site(url, ...)   network: builds a Capture dict from public endpoints
  analyze_capture(capture) pure: turns a Capture into an AgentViewResult

A Capture is plain JSON, so a run can be saved and replayed (the "before" row of
a fix), diffed against the last run, or built by hand from a snapshot of product
bodies. Read-only. Standard library only; certifi is used when installed.
"""

from __future__ import annotations

import datetime as dt
import html as htmlmod
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from .probe import USER_AGENT, TIMEOUT, _CTX, _fetch

READS_FIRST = 160
TITLE_WINDOW = 200

AI_BOTS = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot",
           "PerplexityBot", "Google-Extended", "Applebot-Extended", "Bingbot"]

CTA_WORDS = ["would you like", "request a", "a quote", "free quote", "click", "buy now", "add to cart", "sign up"]

DEFAULT_PROFILE = "https://atlasdigital.design/.well-known/ucp-agent-profile.json"
# Shopify's own hosted example profile. Used when the Atlas profile isn't published yet,
# so a missing file never reads as "this store refuses agents".
FALLBACK_PROFILE = "https://shopify.dev/ucp/agent-profiles/examples/2026-08-25/valid-with-capabilities.json"


# ---------- text the way a feed or agent gets it ----------

class _Stripper(HTMLParser):
    """Tags removed, text kept. This is what a naive feed or plain-text field does,
    which is why CSS inside a <style> block ends up at the top of a description."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.has_style = False
        self.has_script = False

    def handle_starttag(self, tag, attrs):
        if tag == "style":
            self.has_style = True
        elif tag == "script":
            self.has_script = True
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "hr"):
            self.parts.append(" ")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(body_html: str) -> tuple[str, bool, bool]:
    s = _Stripper()
    s.feed(body_html or "")
    text = " ".join("".join(s.parts).split())
    return text, s.has_style, s.has_script


CSS_PATTERN = re.compile(r"[.#]?[\w-]+\s*\{[^}]*:[^}]*\}")
PLACEHOLDER_PATTERN = re.compile(r"\{\{|\{%|\$\{|%\{")
STOPWORDS = {"the", "a", "an", "house", "plan", "plans", "sq", "ft", "sqft", "and", "of", "with", "ii"}


def core_words(title: str) -> list[str]:
    head = re.split(r"\s[|–—-]\s", title or "", maxsplit=1)[0]
    words = [w for w in re.findall(r"[A-Za-z0-9']+", head.lower()) if w not in STOPWORDS and len(w) > 2]
    return words[:4]


def check_body(title: str, body_html: str, kind: str = "product") -> tuple[list[str], str]:
    """Findings for one description, plus its 'reads first' text."""
    text, has_style, has_script = html_to_text(body_html)
    first = text[:READS_FIRST]
    findings: list[str] = []
    if len(text) < 40:
        findings.append(f"{kind} description is empty or near-empty")
        return findings, first
    low = text.lower()
    head = low[:TITLE_WINDOW]
    words = core_words(title)
    if kind == "product" and words and not any(w in head for w in words):
        findings.append(f"{kind} title words not in the first {TITLE_WINDOW} characters")
    # CTA before the first spec-like token (a digit or a colon) in the opening
    opening = low[:TITLE_WINDOW]
    first_spec = re.search(r"\d|:", opening)
    cta_at = min((opening.find(w) for w in CTA_WORDS if w in opening), default=-1)
    # A description that names the product before the CTA has introduced itself;
    # only a CTA ahead of both the name and the first spec is the finding.
    title_at = min((opening.find(w) for w in words if w in opening), default=-1)
    if cta_at >= 0 and (first_spec is None or cta_at < first_spec.start()) and (title_at < 0 or cta_at < title_at):
        findings.append(f"{kind} description opens with a call to action before the {kind}")
    if has_style or CSS_PATTERN.search(text[:600]):
        findings.append(f"{kind} description carries CSS that reaches plain-text readers")
    if has_script:
        findings.append(f"{kind} description carries script text")
    if PLACEHOLDER_PATTERN.search(text):
        findings.append(f"{kind} description carries an unrendered template placeholder")
    return findings, first


# ---------- llms.txt, robots, structured data, feeds ----------

def is_html_page(text: str | None) -> bool:
    """True when a text file request came back as an HTML page (a soft 404)."""
    head = (text or "").lstrip("\ufeff \t\r\n")[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<head" in head


def check_llms(text: str | None, live_count: int | None, noun: str = "plan") -> tuple[list[str], dict]:
    if is_html_page(text):
        text = None  # the site answered /llms.txt with its 404 page or homepage
    info: dict = {"exists": text is not None, "stated_counts": [], "live_count": live_count, "reads_first": ""}
    if text is None:
        return ["no llms.txt"], info
    info["reads_first"] = " ".join(text.split())[:READS_FIRST]
    # (?<![\d.,$]) keeps the cents of "$926.95 for garage plans" out of the counts.
    nums = [int(n) for n in re.findall(r"(?<![\d.,$])\b(\d{1,4})\b\s+(?:\w+\s+){0,3}" + noun, text, flags=re.I)]
    info["stated_counts"] = nums
    if live_count is not None and nums and live_count not in nums:
        # Several stated counts are usually collection sizes, which are allowed to
        # differ from the catalog; one count is read as the catalog total.
        over = [n for n in nums if n > live_count]
        if len(nums) == 1 or over:
            said = over[0] if over else nums[0]
            return [f"llms.txt says {said} {noun}s, live catalog has {live_count}"], info
    return [], info


def parse_robots(text: str) -> dict[str, list[tuple[str, str]]]:
    """{agent-lower: [(directive, value), ...]}; '*' for the default group."""
    groups: dict[str, list[tuple[str, str]]] = {}
    current: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, val = (s.strip() for s in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if current and groups.get(current[-1]) and groups[current[-1]]:
                current = []
            current.append(val.lower())
            groups.setdefault(val.lower(), [])
        elif key in ("allow", "disallow") and current:
            for a in current:
                groups[a].append((key, val))
    return groups


def _robots_allows(groups, agent: str, path: str) -> bool:
    rules = groups.get(agent.lower())
    if rules is None:
        rules = groups.get("*", [])
    best = None
    for d, v in rules:
        if not v:
            continue
        pat = re.escape(v).replace(r"\*", ".*")
        if re.match(pat, path):
            if best is None or len(v) > len(best[1]):
                best = (d, v)
    return best is None or best[0] == "allow"


def check_robots(text: str | None, noindex_paths: list[str] | None = None) -> tuple[list[str], dict]:
    info: dict = {"exists": text is not None, "ai_bots": {}, "blocked_noindex": []}
    if text is None:
        return ["no robots.txt"], info
    groups = parse_robots(text)
    findings = []
    for bot in AI_BOTS:
        ok = _robots_allows(groups, bot, "/")
        info["ai_bots"][bot] = "allowed" if ok else "blocked"
        if not ok:
            findings.append(f"robots.txt blocks {bot}")
    for p in noindex_paths or []:
        if not _robots_allows(groups, "googlebot", p):
            info["blocked_noindex"].append(p)
    if info["blocked_noindex"]:
        findings.append(f"{len(info['blocked_noindex'])} noindex page(s) are blocked in robots.txt, so the noindex is never read")
    return findings, info


class _LdParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.meta_desc = ""
        self._in = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in = True
            self._buf = []
        elif tag == "meta" and a.get("name", "").lower() == "description":
            self.meta_desc = a.get("content", "")

    def handle_endtag(self, tag):
        if tag == "script" and self._in:
            self.blocks.append("".join(self._buf))
            self._in = False

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def check_product_page(html_text: str, live_price: float | None, title: str = "") -> tuple[list[str], dict]:
    p = _LdParser()
    p.feed(html_text or "")
    info: dict = {"meta_description": " ".join(p.meta_desc.split())[:READS_FIRST], "jsonld_name": "",
                  "jsonld_description": "", "jsonld_prices": [], "has_product": False}
    findings: list[str] = []
    for raw in p.blocks:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _walk(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" in types or "IndividualProduct" in types:
                info["has_product"] = True
                info["jsonld_name"] = str(node.get("name", ""))
                d = node.get("description", "")
                info["jsonld_description"] = " ".join(str(d).split())[:READS_FIRST]
            if "Offer" in types and "price" in node:
                try:
                    info["jsonld_prices"].append(float(str(node["price"]).replace(",", "")))
                except ValueError:
                    pass
    if not info["has_product"]:
        findings.append("product page has no Product structured data")
        return findings, info
    low = info["jsonld_description"].lower()[:TITLE_WINDOW]
    if any(w in low for w in CTA_WORDS):
        findings.append("structured data description opens with a call to action")
    if live_price is not None and info["jsonld_prices"] and not any(abs(x - live_price) < 0.01 for x in info["jsonld_prices"]):
        findings.append(f"structured data price {info['jsonld_prices'][0]} differs from live price {live_price}")
    return findings, info


def parse_feed(text: str) -> list[dict]:
    """Rows from an RSS/Atom product feed or a CSV/TSV feed: {link, price}."""
    rows: list[dict] = []
    t = text or ""
    if "<item" in t or "<entry" in t:
        for m in re.finditer(r"<(item|entry)\b.*?</\1>", t, flags=re.S | re.I):
            block = m.group(0)
            link = re.search(r"<(?:g:)?link[^>]*>(.*?)</(?:g:)?link>|<link[^>]*href=\"([^\"]+)\"", block, flags=re.S | re.I)
            price = re.search(r"<g:(?:sale_)?price>([^<]+)</g:(?:sale_)?price>", block, flags=re.I)
            rows.append({"link": htmlmod.unescape((link.group(1) or link.group(2) or "").strip()) if link else "",
                         "price": (price.group(1).strip() if price else "")})
    else:
        import csv
        import io
        sample = t[:2000]
        delim = "\t" if sample.count("\t") > sample.count(",") else ","
        for row in csv.DictReader(io.StringIO(t), delimiter=delim):
            keys = {k.lower().strip(): v for k, v in row.items() if k}
            rows.append({"link": keys.get("link", ""), "price": keys.get("sale_price") or keys.get("price", "")})
    return rows


def _money(s: str) -> float | None:
    m = re.search(r"\d[\d,]*\.?\d*", s or "")
    return float(m.group(0).replace(",", "")) if m else None


def check_feed(feed_text: str, products: list[dict]) -> tuple[list[str], dict]:
    by_handle = {}
    for p in products:
        prices = {_money(v.get("price", "")) for v in p.get("variants", [])}
        by_handle[p.get("handle", "")] = {x for x in prices if x is not None}
    rows = parse_feed(feed_text)
    matched = mismatched = 0
    examples = []
    for r in rows:
        h = urllib.parse.urlsplit(r["link"]).path.rstrip("/").rsplit("/", 1)[-1]
        if h not in by_handle:
            continue
        matched += 1
        fp = _money(r["price"])
        if fp is None or (by_handle[h] and not any(abs(fp - x) < 0.01 for x in by_handle[h])):
            mismatched += 1
            if len(examples) < 5:
                examples.append({"handle": h, "feed_price": r["price"], "live_prices": sorted(by_handle[h])})
    info = {"rows": len(rows), "matched": matched, "mismatched": mismatched, "examples": examples}
    findings = []
    if rows and matched == 0:
        findings.append("feed links match no live product")
    elif matched and mismatched:
        findings.append(f"feed price differs from the store on {mismatched} of {matched} rows ({100 * mismatched // matched}%)")
    return findings, info


# ---------- result ----------

@dataclass
class AgentViewResult:
    site: str
    checked_at: str = ""
    platform: str = ""
    products_checked: int = 0
    collections_checked: int = 0
    product_findings: dict[str, int] = field(default_factory=dict)
    collection_findings: dict[str, int] = field(default_factory=dict)
    ucp_version: str = ""
    ucp_endpoint: str = ""
    ucp_tools: list[str] = field(default_factory=list)
    legacy_mcp_tools: list[str] = field(default_factory=list)
    searches: dict[str, list[str]] = field(default_factory=dict)
    llms: dict = field(default_factory=dict)
    robots: dict = field(default_factory=dict)
    product_page: dict = field(default_factory=dict)
    feed: dict = field(default_factory=dict)
    reads_first: dict[str, str] = field(default_factory=dict)
    surfaces: dict = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    error: str = ""
    group: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_row(self) -> dict:
        d = {
            "site": self.site, "group": self.group, "note": self.note, "checked_at": self.checked_at, "platform": self.platform,
            "products_checked": self.products_checked, "collections_checked": self.collections_checked,
            "ucp_version": self.ucp_version, "ucp_tools": ";".join(self.ucp_tools),
            "legacy_mcp_tools": ";".join(self.legacy_mcp_tools),
            "mcp_card": self.surfaces.get("mcp_card"), "mcp_tools": ";".join(self.surfaces.get("mcp_tools", [])),
            "agent_surfaces": self.surfaces.get("summary", ""),
            "llms_txt": self.llms.get("exists"), "llms_stated": ";".join(map(str, self.llms.get("stated_counts", []))),
            "live_count": self.llms.get("live_count"),
            "ai_bots_blocked": ";".join(b for b, s in self.robots.get("ai_bots", {}).items() if s == "blocked"),
            "feed_rows": self.feed.get("rows"), "feed_mismatched": self.feed.get("mismatched"),
            "product_reads_first": self.reads_first.get("product", ""),
            "collection_reads_first": self.reads_first.get("collection", ""),
            "search_reads_first": self.reads_first.get("search", ""),
            "finding_count": len(self.findings), "findings": " | ".join(self.findings), "error": self.error,
        }
        return d


# ---------- census: which agent surfaces a site publishes ----------

def census_surfaces(cap: dict) -> dict:
    """What a site offers agents, as facts: a UCP profile, a UCP catalog, an MCP server card,
    an MCP endpoint with tools, an llms.txt. No judgement here; the census table is the output."""
    prof = cap.get("ucp_profile")
    ucp_tools = list(cap.get("ucp_tools") or [])
    mcp_card = cap.get("mcp_card")
    mcp_tools = list(cap.get("mcp_tools") or [])
    s = {
        "ucp_profile": isinstance(prof, dict),
        "ucp_catalog": "search_catalog" in ucp_tools,
        "mcp_card": isinstance(mcp_card, dict),
        "mcp_tools": mcp_tools,
        "llms_txt": bool(cap.get("llms_txt")) and not is_html_page(cap.get("llms_txt")),
    }
    if isinstance(mcp_card, dict):
        s["mcp_card_name"] = str(mcp_card.get("name") or mcp_card.get("title") or "")
    have = [k for k in ("ucp_profile", "ucp_catalog", "mcp_card", "llms_txt") if s[k]]
    if mcp_tools:
        have.append(f"mcp_endpoint({len(mcp_tools)} tools)")
    s["summary"] = ";".join(have) if have else "none"
    return s


# ---------- pure analysis over a capture ----------

def analyze_capture(cap: dict) -> AgentViewResult:
    """A check runs only when its input is in the capture, so a product-only snapshot
    reports product findings and nothing about files it never saw."""
    r = AgentViewResult(site=cap.get("site", ""), checked_at=cap.get("checked_at", ""), platform=cap.get("platform", ""))
    r.error = cap.get("error", "")
    r.group = str(cap.get("group") or cap.get("status") or "")
    r.note = str(cap.get("note") or "")

    products = cap.get("products") or []
    r.products_checked = len(products)
    counts: dict[str, int] = {}
    for i, p in enumerate(products):
        f, first = check_body(p.get("title", ""), p.get("body_html") or p.get("descriptionHtml") or "", "product")
        for x in f:
            counts[x] = counts.get(x, 0) + 1
        if i == 0:
            r.reads_first["product"] = first
    r.product_findings = counts
    for k, n in counts.items():
        r.findings.append(f"{k} ({n} of {len(products)} products)")

    cols = cap.get("collections") or []
    r.collections_checked = len(cols)
    ccounts: dict[str, int] = {}
    for i, c in enumerate(cols):
        f, first = check_body(c.get("title", ""), c.get("body_html") or c.get("descriptionHtml") or c.get("description") or "", "collection")
        for x in f:
            ccounts[x] = ccounts.get(x, 0) + 1
        if i == 0:
            r.reads_first["collection"] = first
    r.collection_findings = ccounts
    for k, n in ccounts.items():
        r.findings.append(f"{k} ({n} of {len(cols)} collections)")

    prof = cap.get("ucp_profile")
    if isinstance(prof, dict):
        u = prof.get("ucp", prof)
        r.ucp_version = str(u.get("version", ""))
        svc = u.get("services", {})
        for entries in svc.values():
            for e in entries if isinstance(entries, list) else []:
                if isinstance(e, dict) and e.get("endpoint"):
                    r.ucp_endpoint = e["endpoint"]
    r.ucp_tools = list(cap.get("ucp_tools") or [])
    r.legacy_mcp_tools = list(cap.get("legacy_tools") or [])
    r.surfaces = census_surfaces(cap)
    if cap.get("platform") == "shopify" and "ucp_tools" in cap:
        if not r.ucp_tools:
            r.findings.append("UCP endpoint lists no tools (catalog not reachable by agents, or the profile was refused)")
        elif "search_catalog" not in r.ucp_tools:
            r.findings.append("UCP endpoint is up but search_catalog is missing")
    for q, hits in (cap.get("searches") or {}).items():
        titles = [h.get("title", "") if isinstance(h, dict) else str(h) for h in hits]
        r.searches[q] = titles
        if not titles:
            r.findings.append(f"UCP search returned nothing for '{q}'")
    if r.searches:
        first_q = next(iter(r.searches))
        hits = (cap.get("searches") or {}).get(first_q) or []
        if hits and isinstance(hits[0], dict):
            d = hits[0].get("description")
            d = d.get("html") if isinstance(d, dict) else d
            r.reads_first["search"] = html_to_text(d or "")[0][:READS_FIRST]

    live_count = cap.get("live_count")
    if "llms_txt" in cap:
        f, r.llms = check_llms(cap.get("llms_txt"), live_count, cap.get("count_noun", "plan"))
        r.findings += f
        if r.llms.get("reads_first"):
            r.reads_first["llms_txt"] = r.llms["reads_first"]

    if "robots_txt" in cap:
        f, r.robots = check_robots(cap.get("robots_txt"), cap.get("noindex_paths"))
        r.findings += f

    page = cap.get("product_page_html")
    if page:
        lp = cap.get("product_page_price")
        f, r.product_page = check_product_page(page, lp, cap.get("product_page_title", ""))
        r.findings += f
        r.reads_first["jsonld"] = r.product_page.get("jsonld_description", "")
        r.reads_first["meta"] = r.product_page.get("meta_description", "")

    if cap.get("feed_text") is not None:
        f, r.feed = check_feed(cap["feed_text"], products)
        r.findings += f

    return r


# ---------- network: build a capture ----------

def _get(url: str, accept: str = "*/*") -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as resp:
            return resp.status, resp.read(5_000_000).decode(resp.headers.get_content_charset() or "utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError, OSError):
        # DNS failure, refused connection, bad certificate, timeout: the page is unreadable
        return 0, ""


def _post_json(url: str, payload: dict) -> dict | None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as resp:
            return json.loads(resp.read(5_000_000).decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def mcp_tools_list(endpoint: str, profile: str | None) -> list[str]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    if profile:
        payload["params"] = {"arguments": {"meta": {"ucp-agent": {"profile": profile}}}}
    d = _post_json(endpoint, payload) or {}
    return [t.get("name", "") for t in (d.get("result") or {}).get("tools", [])]


def ucp_search(endpoint: str, profile: str, query: str, limit: int = 5) -> list[dict]:
    payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_catalog", "arguments": {
        "meta": {"ucp-agent": {"profile": profile}},
        "catalog": {"query": query, "context": {"address_country": "US"}, "pagination": {"limit": limit}}}}}
    d = _post_json(endpoint, payload) or {}
    return ((d.get("result") or {}).get("structuredContent") or {}).get("products") or []


def detect_platform(home_html: str) -> str:
    low = home_html.lower()
    if "cdn.shopify.com" in low or "shopify" in low:
        return "shopify"
    if "woocommerce" in low:
        return "woocommerce"
    if "wp-content" in low:
        return "wordpress"
    if "squarespace" in low:
        return "squarespace"
    if "wix.com" in low or "wixstatic" in low:
        return "wix"
    if "volusion" in low:
        return "volusion"
    return ""


def capture_site(url: str, profile: str = DEFAULT_PROFILE, queries: list[str] | None = None,
                 feed_url: str | None = None, count_filter: str | None = None, count_noun: str = "plan",
                 noindex_paths: list[str] | None = None, max_products: int = 1000,
                 sample_page: str | None = None) -> dict:
    if "://" not in url:
        url = "https://" + url
    base = url.rstrip("/")
    host = urllib.parse.urlsplit(base).netloc
    cap: dict = {"site": host, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 "platform": "", "count_noun": count_noun, "noindex_paths": noindex_paths or []}

    status, home = _get(base + "/", "text/html")
    if status == 0:
        cap["error"] = "homepage unreachable (DNS, connection, certificate or timeout)"
    elif status >= 400 and not home:
        cap["error"] = f"homepage returned HTTP {status}"
    cap["platform"] = detect_platform(home)

    # Agent surfaces every platform can publish. These are the census columns.
    s, body = _get(f"{base}/.well-known/ucp", "application/json")
    try:
        cap["ucp_profile"] = json.loads(body) if s == 200 and body else None
    except ValueError:
        cap["ucp_profile"] = None
    s, body = _get(f"{base}/.well-known/mcp/server-card.json", "application/json")
    try:
        cap["mcp_card"] = json.loads(body) if s == 200 and body else None
    except ValueError:
        cap["mcp_card"] = None
    cap["mcp_tools"] = mcp_tools_list(f"{base}/mcp", None)

    if cap["platform"] == "shopify":
        products: list[dict] = []
        page = 1
        while len(products) < max_products:
            s, body = _get(f"{base}/products.json?limit=250&page={page}", "application/json")
            try:
                batch = json.loads(body).get("products", []) if body else []
            except ValueError:
                batch = []
            if not batch:
                break
            products += batch
            page += 1
        cap["products"] = [{"handle": p.get("handle"), "title": p.get("title"), "product_type": p.get("product_type"),
                            "body_html": p.get("body_html"),
                            "variants": [{"price": v.get("price"), "title": v.get("title")} for v in p.get("variants", [])]}
                           for p in products]
        s, body = _get(f"{base}/collections.json?limit=250", "application/json")
        try:
            # the public collections.json carries the description under "description", not body_html
            cap["collections"] = [{"handle": c.get("handle"), "title": c.get("title"),
                                   "body_html": c.get("body_html") or c.get("description") or "",
                                   "products_count": c.get("products_count")}
                                  for c in (json.loads(body).get("collections", []) if body else [])]
        except ValueError:
            cap["collections"] = []
        cap["ucp_tools"] = mcp_tools_list(f"{base}/api/ucp/mcp", profile)
        cap["legacy_tools"] = mcp_tools_list(f"{base}/api/mcp", None)
        cap["searches"] = {}
        for q in queries or []:
            hits = ucp_search(f"{base}/api/ucp/mcp", profile, q)
            cap["searches"][q] = [{"title": h.get("title"), "url": h.get("url"), "description": h.get("description")} for h in hits]
        if products:
            first = products[0]
            s, html_text = _get(f"{base}/products/{first.get('handle')}", "text/html")
            cap["product_page_html"] = html_text
            cap["product_page_title"] = first.get("title", "")
            try:
                cap["product_page_price"] = float(first["variants"][0]["price"])
            except (KeyError, IndexError, ValueError, TypeError):
                cap["product_page_price"] = None
        if count_filter:
            cap["live_count"] = sum(1 for p in cap["products"] if re.search(count_filter, p.get("product_type") or "", re.I))
        else:
            cap["live_count"] = len(cap["products"])
    else:
        # No public catalog endpoint. Read one page the way a crawler does: the sample
        # page if the site file names one (a plan page), else the homepage.
        page_url = sample_page if sample_page else base + "/"
        if page_url.startswith("/"):
            page_url = base + page_url
        s, html_text = _get(page_url, "text/html")
        cap["product_page_html"] = html_text if s == 200 else home
        cap["product_page_url"] = page_url
        cap["product_page_price"] = None

    s, txt = _get(f"{base}/llms.txt", "text/plain")
    cap["llms_txt"] = txt if s == 200 and txt.strip() and not is_html_page(txt) else None
    s, txt = _get(f"{base}/robots.txt", "text/plain")
    cap["robots_txt"] = txt if s == 200 else None
    if feed_url:
        s, txt = _get(feed_url, "*/*")
        cap["feed_text"] = txt if s == 200 else ""
        cap["feed_url"] = feed_url
    return cap


# ---------- runs, diffs, notifications ----------

def save_run(result: AgentViewResult, runs_dir: Path, capture: dict | None = None) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    safe = re.sub(r"[^a-z0-9.-]", "-", result.site.lower())
    path = runs_dir / f"{safe}_{stamp}.json"
    payload = {"result": result.to_dict()}
    if capture is not None:
        payload["capture"] = capture
    path.write_text(json.dumps(payload, indent=1))
    return path


def last_run(site: str, runs_dir: Path, before: Path | None = None) -> dict | None:
    safe = re.sub(r"[^a-z0-9.-]", "-", site.lower())
    files = sorted(runs_dir.glob(f"{safe}_*.json"))
    if before is not None:
        files = [f for f in files if f != before]
    if not files:
        return None
    return json.loads(files[-1].read_text())


def diff_findings(current: list[str], previous: list[str]) -> dict[str, list[str]]:
    cur, prev = set(current), set(previous)
    return {"new": sorted(cur - prev), "fixed": sorted(prev - cur), "unchanged": sorted(cur & prev)}


def notify(title: str, message: str) -> None:
    """Mac notification, the same way Atlas DMARC Watch does it. Silent elsewhere."""
    msg = message.replace('"', "'")[:200]
    try:
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
                       check=False, capture_output=True)
    except FileNotFoundError:
        pass


def append_catch_candidates(log_path: Path, site: str, new_findings: list[str], when: str) -> None:
    """Add new findings under 'Probe found, not yet confirmed' at the bottom of the catch log."""
    if not new_findings or not log_path.exists():
        return
    text = log_path.read_text()
    header = "## Probe found, not yet confirmed"
    if header not in text:
        text = text.rstrip("\n") + f"\n\n{header}\n\nAgent View adds rows here. A person confirms each one before it moves into the main table.\n\n| Date | Site | Finding |\n|---|---|---|\n"
    rows = "".join(f"| {when[:10]} | {site} | {f} |\n" for f in new_findings)
    log_path.write_text(text.rstrip("\n") + "\n" + rows)


def census_table(results: list["AgentViewResult"]) -> str:
    """One Markdown row per site: which agent surfaces it publishes, with the date checked."""
    yn = lambda v: "yes" if v else "no"
    lines = ["| Site | Group | Platform | UCP profile | UCP catalog | MCP server card | /mcp tools | llms.txt | Checked |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        s = r.surfaces or {}
        tools = ", ".join(s.get("mcp_tools", [])) or "none"
        lines.append(f"| {r.site} | {r.group} | {r.platform or 'unknown'} | {yn(s.get('ucp_profile'))} | {yn(s.get('ucp_catalog'))} "
                     f"| {yn(s.get('mcp_card'))} | {tools} | {yn(s.get('llms_txt'))} | {r.checked_at[:10]}{' (' + r.error + ')' if r.error else ''} |")
    return "\n".join(lines) + "\n"


# ---------- command line ----------

def _load_sites(arg: str) -> list[dict]:
    p = Path(arg)
    if p.is_file() and p.suffix == ".json":
        cfg = json.loads(p.read_text())
        return cfg.get("sites", cfg) if isinstance(cfg, dict) else cfg
    if p.is_file():
        return [{"url": ln.strip()} for ln in p.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    return [{"url": arg}]


def capture_from_snapshot(path: Path, site: str, **extra) -> dict:
    """A capture built from a saved product set, for a 'before' row. Accepts the rollback
    shape ({"products":[{"handle","title","descriptionHtml"}]}) or Shopify's products.json."""
    data = json.loads(path.read_text())
    prods = data.get("products", data) if isinstance(data, dict) else data
    cap = {"site": site, "checked_at": data.get("saved_at", "") if isinstance(data, dict) else "", "platform": "shopify",
           "products": [{"handle": p.get("handle"), "title": p.get("title") or p.get("handle", ""),
                         "body_html": p.get("body_html") or p.get("descriptionHtml") or "",
                         "variants": p.get("variants", [])} for p in prods],
           "collections": data.get("collections", []) if isinstance(data, dict) else [],
           "snapshot": str(path)}
    cap.update(extra)
    return cap


def main(argv: list[str] | None = None) -> int:
    import argparse
    import csv
    import sys

    ap = argparse.ArgumentParser(prog="atlas_site_probe.agent_view",
                                 description="Read a store the way agents, feeds and crawlers read it.")
    ap.add_argument("sites", help="a URL, a text file of URLs, or a JSON config (see my_sites.json)")
    ap.add_argument("--profile", default=DEFAULT_PROFILE, help="UCP agent profile URL used for catalog calls")
    ap.add_argument("--runs", default="runs", help="folder for dated run files (default: runs/)")
    ap.add_argument("--csv", help="write one row per site to this CSV")
    ap.add_argument("--json", help="write full results to this JSON file")
    ap.add_argument("--snapshot", help="analyze a saved product JSON instead of fetching (a 'before' row)")
    ap.add_argument("--site-name", help="site label to use with --snapshot")
    ap.add_argument("--capture", help="analyze a saved capture JSON instead of fetching")
    ap.add_argument("--diff", action="store_true", help="compare with the last run of each site")
    ap.add_argument("--notify", action="store_true", help="Mac notification when a diff shows new findings")
    ap.add_argument("--catch-log", help="append new findings to this catch log under 'Probe found, not yet confirmed'")
    ap.add_argument("--no-save", action="store_true", help="do not write a run file")
    ap.add_argument("--census", help="write a Markdown table of agent surfaces per site (UCP, MCP card, /mcp, llms.txt)")
    a = ap.parse_args(argv)

    runs_dir = Path(a.runs)
    results: list[AgentViewResult] = []
    captures: list[dict] = []

    if not a.snapshot and not a.capture:
        status, _ = _get(a.profile, "application/json")
        if status != 200:
            print(f"agent profile {a.profile} returned HTTP {status}; using Shopify's example profile instead", file=sys.stderr)
            a.profile = FALLBACK_PROFILE

    if a.snapshot:
        cap = capture_from_snapshot(Path(a.snapshot), a.site_name or Path(a.snapshot).stem)
        captures.append(cap)
    elif a.capture:
        captures.append(json.loads(Path(a.capture).read_text()))
    else:
        for s in _load_sites(a.sites):
            print(f"reading {s['url']}", file=sys.stderr)
            cap = capture_site(s["url"], profile=s.get("profile", a.profile), queries=s.get("queries"),
                               feed_url=s.get("feed_url"), count_filter=s.get("count_filter"),
                               count_noun=s.get("count_noun", "plan"), noindex_paths=s.get("noindex_paths"),
                               sample_page=s.get("sample_page"))
            for k in ("note", "status", "group"):
                if k in s:
                    cap[k] = s[k]
            captures.append(cap)

    for cap in captures:
        r = analyze_capture(cap)
        results.append(r)
        print(f"{r.site}: {len(r.findings)} findings", file=sys.stderr)
        if a.diff:
            prev = last_run(r.site, runs_dir)
            if prev:
                d = diff_findings(r.findings, prev["result"]["findings"])
                print(f"  new {len(d['new'])}, fixed {len(d['fixed'])}, unchanged {len(d['unchanged'])}", file=sys.stderr)
                for x in d["new"]:
                    print(f"  NEW   {x}", file=sys.stderr)
                for x in d["fixed"]:
                    print(f"  FIXED {x}", file=sys.stderr)
                if d["new"] and a.notify:
                    notify("Agent View", f"{r.site}: {len(d['new'])} new finding(s)")
                if d["new"] and a.catch_log:
                    append_catch_candidates(Path(a.catch_log), r.site, d["new"], r.checked_at or dt.date.today().isoformat())
            else:
                print("  first run, nothing to compare", file=sys.stderr)
        if not a.no_save:
            save_run(r, runs_dir, cap)

    rows = [r.to_row() for r in results]
    if a.csv and rows:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    if a.json:
        Path(a.json).write_text(json.dumps([r.to_dict() for r in results], indent=2))
    if a.census and results:
        Path(a.census).write_text(census_table(results))
    if not a.csv and not a.json and not a.census:
        json.dump([r.to_dict() for r in results], sys.stdout, indent=2)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
