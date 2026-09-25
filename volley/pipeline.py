"""The daily run: sync mailbox -> follow-ups -> discover -> research/write -> deliver -> export."""
import math
import random
import re
import time
from datetime import timedelta

import anthropic

from . import brain, llm, mailer, research, sources, store
from .config import profile, settings

# Config/credential errors: stop the run instead of burning through leads.
FATAL = (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError,
         anthropic.BadRequestError)

KEEP_NEW = 400  # size of the unworked lead pool
CONFIDENCE_RANK = {"posting": 3, "personal": 3, "commit": 2, "hiring": 2, "guessed": 1, "generic": 0}
EXPERIENCE = re.compile(r"(\d+)\s*\+?\s*(?:-\s*\d+\s*)?years?(?:'|’)?\s*(?:of\s+)?(?:professional\s+|industry\s+|relevant\s+)?experience", re.I)
US_ONLY = re.compile(r"\b(us|usa|u\.s\.|united states)[- ]?(only|based only)\b|must (be|reside) (based )?in the (us|united states)"
                     r"|\bus citizens?\b|security clearance", re.I)


def log(*a):
    print(*a, flush=True)


# ---------- discovery ----------

def lead_domain(raw):
    if raw.get("website") and not research.is_job_board(raw["website"]):
        return store.domain_of(raw["website"])
    for e in raw.get("emails", []):
        host = e.split("@")[1]
        if host not in mailer.FREEMAIL and not research.is_job_board("https://" + host):
            return host
    if raw.get("url") and not research.is_job_board(raw["url"]):
        return store.domain_of(raw["url"])
    return ""


def heuristic(raw, p, max_age):
    """Cheap pre-LLM priority. None means drop."""
    t = p["target"]
    title = (raw.get("title") or "").lower()
    desc = (raw.get("description") or "").lower()
    head = title + " " + desc[:400]
    if any(x in f" {title} " for x in t["exclude"]):
        return None
    entry = any(x in head for x in t["entry_signals"])
    role = any(x in (head if raw["source"] in ("hn_hiring", "yc_hiring") else title) for x in t["roles"])
    if not (entry or role):
        return None
    years = [int(m.group(1)) for m in EXPERIENCE.finditer(desc)]
    if years and min(years) >= 3 and not entry:
        return None
    score = (4 if entry else 0) + (2 if role else 0)
    score += min(3, 0.5 * sum(1 for k in t["stack_keywords"] if k in head + desc[:2000]))
    posted = store.parse_iso(raw.get("posted_at"))
    if posted:
        age = (store.now() - posted).days
        if age > max_age:
            return None
        score += 3 if age <= 1 else 2 if age <= 3 else 1 if age <= 7 else 0
    else:
        score += 0.5
    if raw.get("emails"):
        score += 3
    loc = (raw.get("location") or "").lower() + " " + desc[:600]
    if "india" in loc or "bangalore" in loc or "bengaluru" in loc or "noida" in loc or "gurgaon" in loc:
        score += 2
    elif re.search(r"\b(remote|anywhere|worldwide|global)\b", loc):
        score += 1
    if US_ONLY.search(loc + desc):
        score -= 3
    m = re.search(r"team size: (\d+)", desc)
    if m and int(m.group(1)) <= 50:
        score += 1
    return round(score, 1)


def discover(leads, s, p):
    raw_leads = sources.fetch_all(s["sources"], log=log)
    added = 0
    for raw in raw_leads:
        raw["company"] = (raw.get("company") or "").strip()
        if not raw["company"] or raw["company"].lower() == "unknown":
            continue
        score = heuristic(raw, p, s["lead_max_age_days"])
        if score is None:
            continue
        domain = lead_domain(raw)
        key = store.company_key(raw["company"], domain)
        existing = leads.get(key)
        if existing:
            # Same company again: keep the richer posting if we haven't worked it yet.
            if existing["status"] == "new" and score > existing.get("score", 0):
                existing.update(title=raw["title"], url=raw["url"], description=raw["description"],
                                emails=sorted(set(existing.get("emails", [])) | set(raw["emails"])), score=score)
            continue
        leads[key] = {
            "id": key, "company": raw["company"], "title": raw["title"], "url": raw["url"],
            "website": raw["website"] or (f"https://{domain}" if domain else ""), "domain": domain,
            "location": raw["location"], "description": raw["description"][:2000], "emails": raw["emails"],
            "source": raw["source"], "posted_at": raw["posted_at"], "found_at": store.iso(),
            "score": score, "status": "new",
        }
        added += 1
    # Unworked leads are cheap to rediscover; keep only the best ones so leads.json stays small.
    fresh = sorted((l for l in leads.values() if l["status"] == "new"), key=lambda l: l["score"], reverse=True)
    for l in fresh[KEEP_NEW:]:
        del leads[l["id"]]
    log(f"discover: {len(raw_leads)} postings, {added} new leads, {min(len(fresh), KEEP_NEW)} kept in the pool")


# ---------- research + writing ----------

def pick_contact(lead, analysis, s):
    candidates = lead.get("candidates", {})
    chosen = analysis.contact_email.strip().lower()
    if chosen in candidates:
        v = candidates[chosen]
        kind = "posting" if v["where"] == "job posting" else "commit" if "GitHub" in v["where"] else v["kind"]
        return {"email": chosen, "name": analysis.contact_name or v.get("name", ""),
                "role": analysis.contact_role, "confidence": kind, "found_on": v["where"]}
    if s["allow_guessed_emails"] and lead.get("domain") and research.has_mx(lead["domain"]):
        ranked = sorted(analysis.people, key=lambda x: 0 if re.search(r"founder|ceo|cto", x.role, re.I)
                        else 1 if re.search(r"engineer|tech|recruit|talent|hiring", x.role, re.I) else 2)
        for person in ranked[:1]:
            guess = research.guess_email(person.name, lead["domain"], candidates.keys())
            if guess:
                return {"email": guess, "name": person.name, "role": person.role,
                        "confidence": "guessed", "found_on": "pattern guess"}
    return None


def process(leads, s, p, slots):
    """Research and write emails for the top leads until `slots` emails are queued."""
    if slots <= 0:
        return 0
    emailed = {(l.get("contact") or {}).get("email") for l in leads.values() if l.get("contact")}
    pool = sorted((l for l in leads.values() if l["status"] == "new"), key=lambda l: l["score"], reverse=True)
    budget = math.ceil(slots * s["research_factor"])
    queued = 0
    for lead in pool[:budget * 2]:
        if queued >= slots or budget <= 0:
            break
        log(f"research: {lead['company']} (score {lead['score']})")
        research.research(lead)
        if not lead["candidates"] and not lead.get("site"):
            lead["status"] = "no_contact"
            lead.pop("site", None)
            continue
        budget -= 1
        try:
            a = brain.analyze(lead, s)
        except llm.BudgetExceeded as e:
            log(f"stop: {e}")
            lead.pop("site", None)
            break
        except FATAL:
            lead.pop("site", None)
            raise
        except Exception as e:
            log(f"  analyst failed: {e}")
            lead["status"] = "skipped"
            lead.pop("site", None)
            continue
        lead.pop("site", None)
        lead["research"] = {"summary": a.summary, "stack": a.stack, "hook": a.hook, "angle": a.angle,
                            "angle_reason": a.angle_reason, "fit": a.fit, "region": a.region,
                            "people": [x.model_dump() for x in a.people]}
        if not a.relevant or a.fit < s["min_fit"]:
            lead["status"] = "low_fit"
            log(f"  low fit ({a.fit})")
            continue
        contact = pick_contact(lead, a, s)
        if not contact or contact["email"] in emailed:
            lead["status"] = "no_contact"
            log("  no usable contact")
            continue
        lead["contact"] = contact
        try:
            email, issues = brain.write(lead, s)
        except (llm.BudgetExceeded, *FATAL) as e:
            log(f"stop: {e}")
            lead["status"] = "new"
            break
        except Exception as e:
            log(f"  writer failed: {e}")
            lead["status"] = "skipped"
            continue
        lead["email"] = {"subject": email.subject.strip(), "body": email.body.strip(), "lint": issues}
        lead["status"] = "queued"
        emailed.add(contact["email"])
        queued += 1
        log(f"  queued -> {contact['email']} ({contact['confidence']}, fit {a.fit}, angle {a.angle})")
    return queued


# ---------- delivery ----------

def current_regions(s):
    hour = store.now().hour
    for h, regions in s["windows"].items():
        if abs(int(h) - hour) <= 2:  # Actions cron can start late
            return set(regions)
    return None  # outside every window (manual run): all regions


def deliver(leads, s, p, mode, box, all_regions=False):
    today = store.iso()[:10]
    if store.now().weekday() not in s["send_days"]:
        log("deliver: not a send day")
        return
    cap = s["daily_new"] - store.delivered_on(leads, today)
    regions = None if all_regions else current_regions(s)
    queue = [l for l in leads.values() if l["status"] == "queued"
             and (regions is None or l["research"]["region"] in regions)]
    queue.sort(key=lambda l: (l["research"]["fit"], CONFIDENCE_RANK.get(l["contact"]["confidence"], 0), l["score"]),
               reverse=True)
    sig = brain.signature(p)
    for lead in queue[:max(cap, 0)]:
        e = lead["email"]
        msg = mailer.build(p["identity"]["name"], lead["contact"]["email"], e["subject"], f"{e['body']}\n\n{sig}")
        try:
            if mode == "send":
                e["message_id"] = mailer.send(msg)
                lead["status"] = "sent"
                e["sent_at"] = store.iso()
                time.sleep(random.uniform(20, 60))
            else:
                e["message_id"] = box.draft(msg)
                lead["status"] = "drafted"
        except Exception as ex:
            log(f"deliver failed for {lead['company']}: {ex}")
            continue
        e["delivered_at"] = store.iso()
        e["mode"] = mode
        log(f"{lead['status']}: {lead['company']} <{lead['contact']['email']}> — {e['subject']}")


def sync(leads, box):
    """Pick up drafts the user sent, replies, and bounces."""
    for lead in leads.values():
        e, c = lead.get("email") or {}, lead.get("contact") or {}
        if lead["status"] not in store.ACTIVE or not c.get("email"):
            continue
        since = (store.parse_iso(e["delivered_at"]) - timedelta(days=1)).strftime("%Y/%m/%d")
        try:
            if lead["status"] == "drafted":
                mid = box.sent_to(c["email"], since)
                if not mid:
                    continue
                e["message_id"], e["sent_at"], lead["status"] = mid, store.iso(), "sent"
                log(f"sync: {lead['company']} draft was sent")
            if box.bounced(c["email"], since):
                lead["status"] = "bounced"
                log(f"sync: {lead['company']} bounced")
            elif box.replied(c["email"], since):
                lead["status"], lead["replied_at"] = "replied", store.iso()
                log(f"sync: {lead['company']} REPLIED")
        except Exception as ex:
            log(f"sync failed for {lead['company']}: {ex}")


def followups(leads, s, p, mode, box):
    if store.now().weekday() not in s["send_days"]:
        return
    d1, d2 = s["followup_days"]
    sig = brain.signature(p)
    for lead in leads.values():
        e = lead.get("email") or {}
        if lead["status"] not in ("sent", "followup_1") or not e.get("sent_at"):
            continue
        age = (store.now() - store.parse_iso(e["sent_at"])).days
        n = 1 if lead["status"] == "sent" else 2
        if age < (d1 if n == 1 else d2):
            continue
        body = brain.followup(lead, n, p)
        subject = e["subject"] if e["subject"].lower().startswith("re:") else f"Re: {e['subject']}"
        msg = mailer.build(p["identity"]["name"], lead["contact"]["email"], subject, f"{body}\n\n{sig}",
                           reply_to_id=e.get("message_id"))
        try:
            mid = mailer.send(msg) if mode == "send" else box.draft(msg)
        except Exception as ex:
            log(f"follow-up failed for {lead['company']}: {ex}")
            continue
        lead.setdefault("followups", []).append({"n": n, "at": store.iso(), "message_id": mid, "mode": mode})
        lead["status"] = f"followup_{n}"
        log(f"follow-up #{n}: {lead['company']}")
    # Close out threads that went quiet after the last follow-up.
    for lead in leads.values():
        fu = lead.get("followups") or []
        if lead["status"] == "followup_2" and fu and (store.now() - store.parse_iso(fu[-1]["at"])).days >= 7:
            lead["status"] = "closed"


def run(dry_run=False, all_regions=False, no_discover=False):
    s, p = settings(), profile()
    mode = s["mode"]
    leads = store.load()
    log(f"volley: mode={mode} dry_run={dry_run} spent_today=${llm.spent_today():.3f}")
    box = None
    if not dry_run:
        box = mailer.Mailbox()
        sync(leads, box)
        followups(leads, s, p, mode, box)
        store.save(leads)
    if not no_discover:
        discover(leads, s, p)
        store.save(leads)
    today = store.iso()[:10]
    backlog = sum(1 for l in leads.values() if l["status"] == "queued")
    slots = s["daily_new"] - store.delivered_on(leads, today) - backlog
    try:
        n = process(leads, s, p, slots)
        log(f"process: {n} emails queued (backlog now {backlog + n})")
    finally:
        store.save(leads)
    if dry_run:
        preview(leads)
    else:
        deliver(leads, s, p, mode, box, all_regions)
        box.close()
    store.save(leads)
    store.export(leads)
    log(f"done. spent today ${llm.spent_today():.3f}")


def preview(leads):
    sig = brain.signature()
    out = []
    for l in leads.values():
        if l["status"] != "queued":
            continue
        c, e = l["contact"], l["email"]
        out.append(f"## {l['company']} — fit {l['research']['fit']}, {l['research']['angle']}, {l['research']['region']}\n"
                   f"To: {c.get('name') or ''} <{c['email']}> ({c.get('role') or '?'}, {c['confidence']})\n"
                   f"Subject: {e['subject']}\n\n{e['body']}\n\n{sig}\n"
                   + (f"\n_lint: {', '.join(e['lint'])}_\n" if e.get("lint") else ""))
    (store.DATA / "preview.md").write_text("# Queued emails\n\n" + "\n---\n\n".join(out))
    log(f"preview: {len(out)} queued emails written to data/preview.md")
