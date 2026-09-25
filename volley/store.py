"""The lead database: one JSON file keyed by company, plus the tracker exports.

data/leads.json is the source of truth (diff-friendly, committed by the
workflow). data/tracker.csv and TRACKER.md are regenerated from it on every
run so progress is readable on GitHub, phone included.
"""
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

from .config import DATA, ROOT

LEADS = DATA / "leads.json"
USAGE = DATA / "usage.json"

# Lifecycle, in order. Anything after "queued" means an email exists.
STATUSES = ["new", "skipped", "no_contact", "low_fit", "queued", "drafted", "sent",
            "followup_1", "followup_2", "replied", "bounced", "interview", "closed"]
ACTIVE = {"drafted", "sent", "followup_1", "followup_2"}


def now():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if s else None


def domain_of(url):
    if not url:
        return ""
    host = urlparse(url if "//" in url else "https://" + url).netloc.lower()
    return host.removeprefix("www.").split(":")[0]


def company_key(company, domain=""):
    """Stable id: the domain when we have one, else the normalized company name."""
    base = domain or re.sub(r"[^a-z0-9]", "", company.lower())
    return hashlib.sha1(base.encode()).hexdigest()[:10]


def load():
    return json.loads(LEADS.read_text()) if LEADS.exists() else {}


def save(leads):
    DATA.mkdir(exist_ok=True)
    LEADS.write_text(json.dumps(leads, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


def load_usage():
    return json.loads(USAGE.read_text()) if USAGE.exists() else {}


def save_usage(usage):
    DATA.mkdir(exist_ok=True)
    USAGE.write_text(json.dumps(usage, indent=1, sort_keys=True) + "\n")


def delivered_on(leads, day):
    """First-touch emails drafted or sent on a given YYYY-MM-DD (UTC)."""
    return sum(1 for l in leads.values() if (l.get("email") or {}).get("delivered_at", "").startswith(day))


TRACKER_COLUMNS = ["company", "status", "fit", "angle", "contact_name", "contact_role", "contact_email",
                   "contact_confidence", "title", "location", "source", "website", "posting",
                   "delivered_at", "last_touch", "replied_at", "subject", "hook", "summary", "note"]


def export(leads):
    rows = []
    for l in leads.values():
        c, e, r = l.get("contact") or {}, l.get("email") or {}, l.get("research") or {}
        touches = [e.get("delivered_at", "")] + [f.get("at", "") for f in l.get("followups", [])]
        rows.append({
            "company": l["company"], "status": l["status"], "fit": r.get("fit", ""),
            "angle": r.get("angle", ""), "contact_name": c.get("name", ""), "contact_role": c.get("role", ""),
            "contact_email": c.get("email", ""), "contact_confidence": c.get("confidence", ""),
            "title": l.get("title", ""), "location": l.get("location", ""), "source": l.get("source", ""),
            "website": l.get("website", ""), "posting": l.get("url", ""), "delivered_at": e.get("delivered_at", ""),
            "last_touch": max(touches), "replied_at": l.get("replied_at", ""), "subject": e.get("subject", ""),
            "hook": r.get("hook", ""), "summary": r.get("summary", ""), "note": l.get("note", ""),
        })
    rows.sort(key=lambda r: (r["last_touch"], r["fit"] or 0), reverse=True)
    rows = [r for r in rows if r["status"] not in ("new", "skipped")]

    with open(DATA / "tracker.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRACKER_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    counts = Counter(l["status"] for l in leads.values())
    emailed = [l for l in leads.values() if (l.get("email") or {}).get("delivered_at")]
    sent = [l for l in emailed if l["status"] != "drafted"]
    replied = [l for l in sent if l.get("replied_at")]
    rate = f"{100 * len(replied) / len(sent):.0f}%" if sent else "–"
    by_angle = Counter((l.get("research") or {}).get("angle", "?") for l in sent)
    replied_angle = Counter((l.get("research") or {}).get("angle", "?") for l in replied)
    usage = load_usage()
    spend = sum(d.get("usd", 0) for d in usage.values())

    lines = [
        "# Tracker", "",
        f"_Updated {iso()} UTC. Full table: [data/tracker.csv](data/tracker.csv)._", "",
        f"**Emailed:** {len(emailed)} · **Sent:** {len(sent)} · **Replies:** {len(replied)} ({rate}) · "
        f"**Interviews:** {counts['interview']} · **API spend:** ${spend:.2f}", "",
        "| Status | Count |", "|---|---|",
        *[f"| {s} | {counts[s]} |" for s in STATUSES if counts[s]], "",
        "## Reply rate by angle", "", "| Angle | Sent | Replies |", "|---|---|---|",
        *[f"| {a} | {n} | {replied_angle[a]} |" for a, n in by_angle.most_common()], "",
        "## Replies", "",
        *([f"- **{l['company']}** — {(l.get('contact') or {}).get('name', '')} "
           f"<{(l.get('contact') or {}).get('email', '')}> replied {l['replied_at'][:10]}" for l in replied]
          or ["_None yet._"]), "",
        "## Latest 25 emails", "", "| Date | Company | Contact | Angle | Status |", "|---|---|---|---|---|",
        *[f"| {l['email']['delivered_at'][:10]} | {l['company']} | {(l.get('contact') or {}).get('email', '')} | "
          f"{(l.get('research') or {}).get('angle', '')} | {l['status']} |"
          for l in sorted(emailed, key=lambda l: l["email"]["delivered_at"], reverse=True)[:25]],
        "",
    ]
    (ROOT / "TRACKER.md").write_text("\n".join(lines))
    from . import dashboard
    dashboard.write(leads)
