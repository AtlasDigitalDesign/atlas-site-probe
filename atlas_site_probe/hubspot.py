"""Push probe findings into a HubSpot CRM as company records with a note.

Uses HubSpot's CRM v3 API with a service key or a legacy private app token
(works on the free CRM).
Set HUBSPOT_TOKEN in the environment. The token needs these scopes:
crm.objects.companies.read, crm.objects.companies.write.
Notes are written through the same token.

Nothing here deletes or overwrites a company. If a company with the same
domain exists, the probe adds a new note to it and leaves its fields alone.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .probe import _CTX

API = "https://api.hubapi.com"
NOTE_TO_COMPANY = 190  # HubSpot-defined association type: note -> company


class HubSpotError(RuntimeError):
    pass


def _call(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=_CTX) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        detail = raw[:300].decode("utf-8", "replace")
        try:
            body = json.loads(raw)
            scopes = sorted({s for err in body.get("errors", [])
                             for s in (err.get("context") or {}).get("requiredGranularScopes", [])})
            detail = body.get("message", detail)
            if scopes:
                detail += " Add one of these scopes to your key: " + ", ".join(scopes)
        except (ValueError, AttributeError):
            pass
        raise HubSpotError(f"{method} {path} -> {e.code}: {detail}") from None


def _domain(url: str) -> str:
    host = urllib.parse.urlsplit(url if "://" in url else "https://" + url).hostname or url
    return host.removeprefix("www.")


def company_name(title: str, domain: str) -> str:
    """Brand part of a page title: 'Acme · Shop shoes' -> 'Acme'. Falls back to the domain."""
    for sep in (" · ", " | ", " – ", " — ", " - ", ": "):
        if sep in title:
            title = title.split(sep)[0]
    title = title.strip()
    return title[:100] if title else domain


def find_company(domain: str, token: str) -> str | None:
    res = _call("POST", "/crm/v3/objects/companies/search", token, {
        "filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]}],
        "properties": ["domain", "name"],
        "limit": 1,
    })
    results = res.get("results") or []
    return results[0]["id"] if results else None


def create_company(domain: str, name: str, token: str) -> str:
    res = _call("POST", "/crm/v3/objects/companies", token, {"properties": {"domain": domain, "name": name or domain}})
    return res["id"]


def note_body(row: dict) -> str:
    lines = [f"<b>Site probe</b> {row.get('final_url') or row.get('url')}", ""]
    for f in (row.get("findings") or "").split(" | "):
        if f:
            lines.append(f"- {f}")
    lines += ["", f"Schema types: {row.get('schema_types') or 'none'}",
              f"Platform: {row.get('platform') or 'unknown'}"]
    return "<br>".join(lines)


def add_note(company_id: str, html: str, token: str) -> str:
    res = _call("POST", "/crm/v3/objects/notes", token, {
        "properties": {"hs_timestamp": int(time.time() * 1000), "hs_note_body": html},
        "associations": [{
            "to": {"id": company_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_COMPANY}],
        }],
    })
    return res["id"]


def push_rows(rows: list[dict], min_findings: int = 1, dry_run: bool = False, token: str | None = None) -> list[dict]:
    """Send each row with at least `min_findings` findings to HubSpot. Returns a log."""
    token = token or os.environ.get("HUBSPOT_TOKEN", "")
    if not dry_run and not token:
        raise HubSpotError("HUBSPOT_TOKEN is not set")
    log = []
    for row in rows:
        if int(row.get("finding_count", 0)) < min_findings:
            log.append({"url": row["url"], "action": "skipped (below threshold)"})
            continue
        domain = _domain(row.get("final_url") or row["url"])
        if dry_run:
            log.append({"url": row["url"], "action": f"would push {domain} with {row['finding_count']} findings"})
            continue
        cid = find_company(domain, token)
        created = cid is None
        if created:
            cid = create_company(domain, company_name(row.get("title", ""), domain), token)
        nid = add_note(cid, note_body(row), token)
        log.append({"url": row["url"], "action": ("created" if created else "updated") + f" company {cid}, note {nid}"})
    return log
