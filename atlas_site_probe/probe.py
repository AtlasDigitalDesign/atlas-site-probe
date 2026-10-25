"""Read-only checks on a single public web page.

The probe fetches one URL the way a crawler would, then reports the things that
fail silently on small commerce sites: a canonical pointing somewhere else, a
noindex header nobody remembers adding, structured data that does not parse,
analytics tags that are missing from the page source, and a cart that does not
answer. It uses only the Python standard library.
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser

USER_AGENT = "AtlasSiteProbe/0.1 (+https://github.com/AtlasDigitalDesign/atlas-site-probe)"
TIMEOUT = 20

TAG_PATTERNS = {
    "ga4": re.compile(r"\bG-[A-Z0-9]{6,12}\b"),
    "google_ads": re.compile(r"\bAW-\d{6,12}\b"),
    "gtm": re.compile(r"\bGTM-[A-Z0-9]{4,10}\b"),
    "meta_pixel": re.compile(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{8,20})['\"]"),
}


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.h1_count = 0
        self.canonicals: list[str] = []
        self.meta: dict[str, str] = {}
        self.jsonld_raw: list[str] = []
        self._in_title = False
        self._in_jsonld = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "h1":
            self.h1_count += 1
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canonicals.append(a.get("href", ""))
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_jsonld = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_jsonld:
            self.jsonld_raw.append("".join(self._buf))
            self._in_jsonld = False

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        if self._in_jsonld:
            self._buf.append(data)


@dataclass
class ProbeResult:
    url: str
    final_url: str = ""
    status: int | None = None
    error: str = ""
    elapsed_ms: int = 0
    bytes: int = 0
    title: str = ""
    h1_count: int = 0
    canonical: str = ""
    canonical_matches: bool | None = None
    robots_meta: str = ""
    x_robots_tag: str = ""
    noindex: bool = False
    jsonld_blocks: int = 0
    jsonld_broken: int = 0
    schema_types: list[str] = field(default_factory=list)
    tags: dict[str, list[str]] = field(default_factory=dict)
    platform: str = ""
    cart_status: int | None = None
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_row(self) -> dict:
        """Flat dict for CSV export (one row per site, for a Clay table)."""
        d = self.to_dict()
        d["schema_types"] = ";".join(self.schema_types)
        for name in TAG_PATTERNS:
            d[f"tag_{name}"] = ";".join(self.tags.get(name, []))
        d.pop("tags")
        d["findings"] = " | ".join(self.findings)
        d["finding_count"] = len(self.findings)
        return d


def _ssl_context() -> ssl.SSLContext:
    """Default trust store, or certifi's bundle when it is installed.

    Python from python.org on macOS ships without root certificates until
    'Install Certificates.command' is run, so every HTTPS fetch fails. Using
    certifi when present avoids that without changing system settings."""
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_CTX = _ssl_context()

CERT_HELP = ("certificate verify failed: this Python has no root certificates. On macOS with "
             "Python from python.org, run '/Applications/Python 3.x/Install Certificates.command' once, "
             "or 'pip install certifi'")


def _fetch(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    return urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX)


def _collect_types(node, out: set[str]) -> None:
    if isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, str):
            out.add(t)
        elif isinstance(t, list):
            out.update(x for x in t if isinstance(x, str))
        for v in node.values():
            _collect_types(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_types(v, out)


def _norm(u: str) -> str:
    p = urllib.parse.urlsplit(u)
    path = p.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), path, p.query, ""))


def analyze(url: str, final_url: str, status: int, headers: dict, body: str) -> ProbeResult:
    """Pure analysis step, separated from the network so it can be tested on fixtures."""
    r = ProbeResult(url=url, final_url=final_url, status=status, bytes=len(body.encode("utf-8", "replace")))
    p = _PageParser()
    p.feed(body)
    r.title = " ".join("".join(p.title_parts).split())
    r.h1_count = p.h1_count
    r.robots_meta = p.meta.get("robots", "")
    r.x_robots_tag = headers.get("x-robots-tag", "")
    r.noindex = "noindex" in (r.robots_meta + " " + r.x_robots_tag).lower()

    if p.canonicals:
        r.canonical = urllib.parse.urljoin(final_url, p.canonicals[0])
        r.canonical_matches = _norm(r.canonical) == _norm(final_url)

    types: set[str] = set()
    for raw in p.jsonld_raw:
        r.jsonld_blocks += 1
        try:
            _collect_types(json.loads(raw), types)
        except (json.JSONDecodeError, ValueError):
            r.jsonld_broken += 1
    r.schema_types = sorted(types)

    for name, pat in TAG_PATTERNS.items():
        found = sorted(set(m if isinstance(m, str) else m for m in pat.findall(body)))
        if found:
            r.tags[name] = found

    low = body.lower()
    if "cdn.shopify.com" in low or "shopify.theme" in low:
        r.platform = "shopify"
    elif "wp-content" in low:
        r.platform = "wordpress"
    elif "squarespace" in low:
        r.platform = "squarespace"
    elif "wix.com" in low or "wixstatic" in low:
        r.platform = "wix"

    # Findings: each one is a fact about the page, phrased for a person.
    if status >= 400:
        r.findings.append(f"page returned HTTP {status}")
    if r.noindex:
        r.findings.append("page is marked noindex")
    if len(p.canonicals) > 1:
        r.findings.append(f"{len(p.canonicals)} canonical tags on one page")
    if r.canonical_matches is False:
        r.findings.append("canonical points to a different URL")
    if not p.canonicals:
        r.findings.append("no canonical tag")
    if r.jsonld_broken:
        r.findings.append(f"{r.jsonld_broken} structured data block(s) do not parse")
    if r.jsonld_blocks == 0:
        r.findings.append("no structured data")
    if r.h1_count != 1:
        r.findings.append(f"{r.h1_count} H1 headings (expected 1)")
    if not r.tags.get("ga4") and not r.tags.get("gtm"):
        r.findings.append("no GA4 or GTM tag found in page source (may load later via an app)")
    if not r.title:
        r.findings.append("no title")
    return r


def probe(url: str, check_cart: bool = True) -> ProbeResult:
    if "://" not in url:
        url = "https://" + url
    start = time.monotonic()
    try:
        with _fetch(url) as resp:
            body = resp.read(3_000_000).decode(resp.headers.get_content_charset() or "utf-8", "replace")
            headers = {k.lower(): v for k, v in resp.headers.items()}
            r = analyze(url, resp.geturl(), resp.status, headers, body)
    except urllib.error.HTTPError as e:
        body = e.read(3_000_000).decode("utf-8", "replace") if e.fp else ""
        r = analyze(url, e.geturl() or url, e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = str(getattr(e, "reason", e))
        r = ProbeResult(url=url, error=CERT_HELP if "CERTIFICATE_VERIFY_FAILED" in reason else reason)
        r.findings.append(f"could not fetch: {r.error}")
        return r
    r.elapsed_ms = int((time.monotonic() - start) * 1000)

    if check_cart and r.platform == "shopify":
        cart = urllib.parse.urljoin(r.final_url, "/cart")
        try:
            with _fetch(cart) as c:
                r.cart_status = c.status
        except urllib.error.HTTPError as e:
            r.cart_status = e.code
        except (urllib.error.URLError, TimeoutError, OSError):
            r.cart_status = None
        if r.cart_status and r.cart_status >= 400:
            r.findings.append(f"/cart returned HTTP {r.cart_status}")
    return r
