"""data/dashboard.json: everything the dashboard shows, rebuilt with the tracker on every run.

Cards for every company volley has emailed or is about to, the top of the pool for what comes next,
and the numbers behind the analytics (funnel, daily activity, reply rate by angle/source/region).
"""
import json
from collections import Counter, defaultdict
from datetime import timedelta

from . import store
from .config import settings

UP_NEXT_POOL = 40   # top-scored unresearched leads shown as "later"
SKIP = {"no_contact", "low_fit", "skipped"}


def _card(l):
    c, e, r = l.get("contact") or {}, l.get("email") or {}, l.get("research") or {}
    return {
        "id": l["id"], "company": l["company"], "title": l.get("title", ""), "location": l.get("location", ""),
        "source": l.get("source", ""), "url": l.get("url", ""), "website": l.get("website", ""),
        "status": l["status"], "score": l.get("score"), "posted_at": l.get("posted_at"), "found_at": l.get("found_at"),
        "pay": l.get("pay"), "priority": l.get("priority", 0), "priority_note": l.get("priority_note", ""),
        "batch": l.get("batch"), "posting": (l.get("description") or "")[:600],
        "contact": {k: c.get(k, "") for k in ("name", "email", "role", "confidence")} if c else None,
        "research": {k: r.get(k) for k in ("summary", "stack", "hook", "angle", "fit", "region")} if r else None,
        "email": {"subject": e.get("subject", ""), "body": e.get("body", ""), "delivered_at": e.get("delivered_at"),
                  "sent_at": e.get("sent_at"), "mode": e.get("mode"), "tailored": bool(e.get("resume_pdf")),
                  "tailor_reason": e.get("tailor_reason", "")} if e else None,
        "followups": [{"n": f["n"], "at": f["at"]} for f in l.get("followups", [])],
        "replied_at": l.get("replied_at"), "note": l.get("note", ""),
    }


def build(leads):
    s = settings()
    counts = Counter(l["status"] for l in leads.values())
    emailed = [l for l in leads.values() if (l.get("email") or {}).get("delivered_at")]
    sent = [l for l in emailed if l["email"].get("sent_at")]   # sent by volley, or a draft you sent
    replied = [l for l in leads.values() if l.get("replied_at")]

    # Daily activity for the last 30 days: first emails, follow-ups, replies.
    today = store.now().date()
    days = {(today - timedelta(days=i)).isoformat(): {"emails": 0, "followups": 0, "replies": 0} for i in range(29, -1, -1)}
    for l in leads.values():
        d = ((l.get("email") or {}).get("delivered_at") or "")[:10]
        if d in days:
            days[d]["emails"] += 1
        for f in l.get("followups", []):
            if f["at"][:10] in days:
                days[f["at"][:10]]["followups"] += 1
        if (l.get("replied_at") or "")[:10] in days:
            days[l["replied_at"][:10]]["replies"] += 1

    def rates(key):
        out = defaultdict(lambda: {"sent": 0, "replied": 0})
        for l in sent:
            k = key(l) or "unknown"
            out[k]["sent"] += 1
            out[k]["replied"] += bool(l.get("replied_at"))
        return [{"key": k, **v} for k, v in sorted(out.items(), key=lambda kv: -kv[1]["sent"])]

    pool = sorted((l for l in leads.values() if l["status"] == "new"), key=lambda l: l.get("score") or 0, reverse=True)
    cards = [_card(l) for l in leads.values() if l["status"] not in SKIP | {"new"}] + [_card(l) for l in pool[:UP_NEXT_POOL]]
    return {
        "generated_at": store.iso(), "mode": s["mode"], "daily_new": s["daily_new"], "credits_until": s["credits_until"],
        "followup_days": s["followup_days"],
        "counts": {st: counts[st] for st in store.STATUSES if counts[st]},
        "funnel": [
            {"stage": "Discovered", "n": len(leads)},
            {"stage": "Researched", "n": sum(1 for l in leads.values() if l["status"] != "new")},
            {"stage": "Emailed", "n": len(emailed)},
            {"stage": "Replied", "n": len(replied)},
            {"stage": "Interview", "n": counts["interview"]},
        ],
        "totals": {"emailed": len(emailed), "sent": len(sent), "replied": len(replied),
                   "interviews": counts["interview"], "queued": counts["queued"], "drafted": counts["drafted"],
                   "bounced": counts["bounced"], "followups": sum(len(l.get("followups", [])) for l in leads.values()),
                   "tailored": sum(1 for l in emailed if l["email"].get("resume_pdf")), "pool": counts["new"]},
        "daily": [{"date": d, **v} for d, v in days.items()],
        "by_angle": rates(lambda l: (l.get("research") or {}).get("angle")),
        "by_source": rates(lambda l: l.get("source")),
        "by_region": rates(lambda l: (l.get("research") or {}).get("region")),
        "cards": cards,
    }


def write(leads):
    (store.DATA / "dashboard.json").write_text(json.dumps(build(leads), ensure_ascii=False, separators=(",", ":")))
