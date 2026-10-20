"""The daily run: sync mailbox -> follow-ups -> discover -> research/write -> deliver -> export."""
import math
import os
import random
import re
import time
from datetime import timedelta

import anthropic

from . import brain, llm, mailer, research, sources, store, tailor
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


_GOOD_SIGNAL_SOURCES = ("github_orgs", "funding_news", "portfolio")
# How often a source's researched leads end in a sent email (25 Sep - 5 Oct): github_orgs 23/86, yc_hiring 51/188,
# hn_hiring 16/81, jobicy 2/10, internshala 3/30, unstop 8/86, funding_news 0/25 (no address on the article:
# now India-hub rounds get the blind founder guess, see heuristic), arbeitnow 0/13, simplify 1/167. Simplify and
# Arbeitnow list large companies behind ATS forms with no reachable person, so they only fill what's left.
# Replies per sent email (Sep 29 - Oct 12, 183 sends): hn_hiring 4/21, github_orgs 9/57, yc_hiring 6/88. A real
# posting got 20% vs 10% with none, India 20% vs US 7%: YC's directory (mostly US, no posting) ranks last
# among the good sources unless research finds a YC job posting (research.find_people).
SOURCE_YIELD = {"manual": 3, "github_orgs": 2, "hn_hiring": 2, "funding_news": 1, "portfolio": 1, "unstop": -1, "internshala": -1,
                "yc_hiring": -1, "arbeitnow": -3, "simplify": -4}
YC_BATCH = re.compile(r"\b(?:Winter|Summer|Spring|Fall|[WSXF])\s?'?(\d{2}|\d{4})\b")
US_LOC = re.compile(r"\b(usa|united states|u\.s\.|america / canada|canada)\b")
INDIA = re.compile(r"\b(india|bangalore|bengaluru|hyderabad|pune|mumbai|chennai|kolkata|ahmedabad|jaipur|"
                   r"chandigarh|indore|kochi)\b")


def _priority_note(ncr_city, pay, raw):
    parts = []
    if ncr_city:
        parts.append(ncr_city.title())
    if pay:
        parts.append(f"{pay['max'] / 100000:g} LPA" if pay["period"] == "year" else f"₹{pay['max']:,}/mo")
    if raw.get("yc"):
        parts.append("YC")
    if raw["source"] == "github_orgs":
        parts.append("active GitHub org, no posting")
    if raw["source"] == "funding_news":
        parts.append("recently funded")
    return " · ".join(parts)


def heuristic(raw, p, s, max_age):
    """Cheap pre-LLM priority. None means drop. Also sets raw['priority']/raw['priority_note']."""
    t, pr = p["target"], s["priority"]
    title = (raw.get("title") or "").lower()
    desc = (raw.get("description") or "").lower()
    head = title + " " + desc[:400]
    if any(x in f" {title} " for x in t["exclude"]):
        return None
    entry = any(x in head for x in t["entry_signals"])
    role = any(x in (head if raw["source"] in ("hn_hiring", "yc_hiring") else title) for x in t["roles"])
    role = role or raw["source"] in _GOOD_SIGNAL_SOURCES   # these have no title to match: they always count
    if not (entry or role):
        return None
    years = [int(m.group(1)) for m in EXPERIENCE.finditer(desc)]
    if years and min(years) >= 3 and not entry:
        return None

    pay, target = raw.get("pay"), None
    if pay:
        floor = pr["min_stipend"] if pay["period"] == "month" else pr["min_lpa"] * 100000
        target = pr["target_stipend"] if pay["period"] == "month" else pr["target_lpa"] * 100000
        if pay["max"] < floor:
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
    ncr_city = next((c for c in pr["cities"] if c in loc), None)
    india_hub = not ncr_city and bool(INDIA.search(loc))
    if ncr_city:
        score += 5
    elif india_hub:
        score += 2
    elif re.search(r"\b(remote|anywhere|worldwide|global|work from home)\b", loc):
        score += 1
    if US_ONLY.search(loc + desc):
        score -= 3
    m = re.search(r"team size: (\d+)", desc)
    if m:
        size = int(m.group(1))
        score += 1 if size <= 50 else 0
        score -= min(size, 200) / 400   # tie-break: smaller teams first (founder reads the email)
    if raw.get("yc"):
        score += 2
        # YC's company list is mostly US startups with no posted opening: the yc_hiring pool had ~300 leads
        # all scored 8.0. Rank by what matters instead: a real posting, outside the US, a recent batch.
        if raw["source"] == "yc_hiring" and not ncr_city:
            score -= 2   # no posting, just "hiring" on YC's directory
        b = YC_BATCH.search(raw.get("title") or "")
        if b:
            year = int(b.group(1)) % 100
            score += 1 if year >= 24 else 0 if year >= 20 else -1
    if not ncr_city and not india_hub and US_LOC.search(loc):
        # US startups rarely hire an intern in India; remote-friendly ones a little more often.
        score -= 1 if re.search(r"\bremote\b", loc) else 2
    score += SOURCE_YIELD.get(raw["source"], 0)
    if raw.get("website") and not research.is_job_board(raw["website"]):
        score += 1   # a company site to crawl is where contacts come from
    if pay:
        score += 6 if pay["max"] >= target else 3

    good_signal = raw.get("yc") or raw["source"] in _GOOD_SIGNAL_SOURCES or \
                  (raw["source"] in ("unstop", "internshala") and pay is not None)
    if not ncr_city:
        # Funded or actively-shipping startups in India's other hubs: worth the blind founder guess too.
        tier = 1 if india_hub and raw["source"] in _GOOD_SIGNAL_SOURCES else 0
    elif pay and pay["max"] >= target:
        tier = 2
    elif pay or good_signal:
        tier = 1
    else:
        tier = 0
    raw["priority"] = tier
    raw["priority_note"] = _priority_note(ncr_city, pay, raw)
    return round(score, 1)


def discover(leads, s, p):
    raw_leads = sources.fetch_all(s["sources"], log=log, priority=s["priority"],
                                  known_urls={l.get("url") for l in leads.values()})
    added = 0
    for raw in raw_leads:
        raw["company"] = (raw.get("company") or "").strip()
        if not raw["company"] or raw["company"].lower() == "unknown":
            continue
        score = heuristic(raw, p, s, s["lead_max_age_days"])
        if score is None:
            continue
        domain = lead_domain(raw)
        key = store.company_key(raw["company"], domain)
        existing = leads.get(key)
        if existing:
            # Same company again: keep the richer posting if we haven't worked it yet.
            if existing["status"] == "new" and score > existing.get("score", 0):
                existing.update(title=raw["title"], url=raw["url"], description=raw["description"],
                                emails=sorted(set(existing.get("emails", [])) | set(raw["emails"])), score=score,
                                pay=raw.get("pay"), yc=raw.get("yc", False),
                                priority=raw["priority"], priority_note=raw["priority_note"])
            continue
        leads[key] = {
            "id": key, "company": raw["company"], "title": raw["title"], "url": raw["url"],
            "website": raw["website"] or (f"https://{domain}" if domain else ""), "domain": domain,
            "location": raw["location"], "description": raw["description"][:2000], "emails": raw["emails"],
            "source": raw["source"], "posted_at": raw["posted_at"], "found_at": store.iso(),
            "score": score, "status": "new", "pay": raw.get("pay"), "yc": raw.get("yc", False),
            "priority": raw["priority"], "priority_note": raw["priority_note"],
        }
        added += 1
    # Rescore the whole pool: freshness decays, and scoring rules change. Leads that no longer qualify go.
    for l in [l for l in leads.values() if l["status"] == "new"]:
        rescored = heuristic(l, p, s, s["lead_max_age_days"])
        if rescored is None:
            del leads[l["id"]]
        else:
            l["score"] = rescored
    # Unworked leads are cheap to rediscover; keep only the best ones so leads.json stays small.
    fresh = sorted((l for l in leads.values() if l["status"] == "new"),
                   key=lambda l: (l.get("priority", 0), l["score"]), reverse=True)
    for l in fresh[KEEP_NEW:]:
        del leads[l["id"]]
    log(f"discover: {len(raw_leads)} postings, {added} new leads, {min(len(fresh), KEEP_NEW)} kept in the pool")


# no_contact leads worth a second research pass with the people finder (founders via YC, engineers via
# GitHub). Big-company boards (Simplify, Arbeitnow, remote boards) had no reachable person and stay out.
RECONTACT_SOURCES = {"portfolio", "linkedin", "hasjob", "reddit", "yc_hiring", "github_orgs", "hn_hiring", "funding_news", "unstop", "internshala", "ats_boards",
                     "manual"}


def recontact(leads, s):
    """Put up to `recontact_per_day` never-retried no_contact leads back in the pool, analyst-approved ones
    (fit >= min_fit, dropped only for want of an address or a slot) first. Returns how many."""
    pool = [l for l in leads.values() if l["status"] == "no_contact" and not l.get("recontacted")
            and l["source"] in RECONTACT_SOURCES]
    pool.sort(key=lambda l: ((l.get("research") or {}).get("fit") or 0, l.get("priority", 0), l.get("score", 0)),
              reverse=True)
    for l in pool[:s.get("recontact_per_day", 0)]:
        l["status"], l["recontacted"] = "new", store.iso()[:10]
    return min(len(pool), s.get("recontact_per_day", 0))


# ---------- research + writing ----------

BOUNCE_BRAKE = 0.04   # risky address kinds stop when more than 4% of the last 7 days' first emails bounced
# What bounced up to Oct 13: 7 of 10 were generic inboxes copied from a site (hello@/team@/contact@: 7 of 50
# sent, 14%), 1 a pattern guess. Site footers often list an inbox nobody set up.
RISKY_KINDS = {"guessed", "generic"}


def guess_brake(leads, s):
    """While the recent bounce rate is high, stop emailing guessed addresses and generic inboxes found on a
    site (an address the posting itself gives is still used): at 40 emails a day a bad week would cost the
    Gmail account its inbox placement. Mutates and returns s."""
    since = (store.now() - timedelta(days=7)).isoformat()
    recent = [l for l in leads.values() if (l.get("email") or {}).get("delivered_at", "") >= since[:19]]
    bounced = sum(1 for l in recent if l["status"] == "bounced")
    if len(recent) >= 20 and bounced / len(recent) > BOUNCE_BRAKE and s["allow_guessed_emails"]:
        log(f"bounce brake: {bounced}/{len(recent)} bounced in 7 days, guessed and generic site inboxes off")
        s["allow_guessed_emails"] = False
    return s


def pick_contact(lead, analysis, s):
    candidates = lead.get("candidates", {})
    chosen = analysis.contact_email.strip().lower()
    if chosen in candidates and not s["allow_guessed_emails"] and candidates[chosen]["kind"] in RISKY_KINDS \
            and candidates[chosen]["where"] != "job posting":
        chosen = ""
    if chosen in candidates:
        v = candidates[chosen]
        kind = "posting" if v["where"] == "job posting" else "commit" if "GitHub" in v["where"] else v["kind"]
        return {"email": chosen, "name": analysis.contact_name or v.get("name", ""),
                "role": analysis.contact_role, "confidence": kind, "found_on": v["where"]}
    mx = lead["domain_mx"] if "domain_mx" in lead else bool(lead.get("domain")) and research.has_mx(lead["domain"])
    if s["allow_guessed_emails"] and lead.get("domain") and mx:
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
    guess_brake(leads, s)
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
        if a.red_flags:
            lead["status"] = "low_fit"
            log(f"  red flags: {', '.join(a.red_flags)}")
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
    """Regions whose send window has opened today (2h early allowed). A late cron or a late routine then
    sends an email a little late instead of leaving it queued for a window that already passed."""
    hour = store.now().hour
    return {r for h, regions in s["windows"].items() if hour >= int(h) - 2 for r in regions}


def deliver(leads, s, p, mode, box, all_regions=False):
    today = store.iso()[:10]
    if store.now().weekday() not in s["send_days"]:
        log("deliver: not a send day")
        return
    cap = s["daily_new"] - store.delivered_on(leads, today)
    regions = None if all_regions else current_regions(s)
    queue = [l for l in leads.values() if l["status"] == "queued"
             and (regions is None or l["research"]["region"] in regions)
             and (l["email"].get("not_before") or "") <= store.iso()]   # hand-queued emails can wait for a date
    if regions is not None:
        waiting = sum(1 for l in leads.values() if l["status"] == "queued") - len(queue)
        if waiting:
            log(f"deliver: {waiting} queued emails wait for a later send window")
    queue.sort(key=lambda l: (l.get("priority", 0), l["research"]["fit"],
                              CONFIDENCE_RANK.get(l["contact"]["confidence"], 0), l["score"]),
               reverse=True)
    answered = answered_companies(leads)
    for l in queue:
        if _company(l) in answered:   # e.g. the referral ask to an engineer after the founder already replied
            l["status"], l["note"] = "skipped", "someone at the company already replied"
    queue = [l for l in queue if l["status"] == "queued"]
    sig = brain.signature(p)
    for lead in queue[:max(cap, 0)]:
        e = lead["email"]
        pdf = store.ROOT / e["resume_pdf"] if e.get("resume_pdf") else None
        if not (pdf and pdf.exists()):
            pdf = None
            # A PDF on a cold first touch costs inbox placement (attachments bounce/filter ~3x more often), so the
            # master resume goes as the signature link unless the posting itself asked for applications by email.
            if (s.get("attach_resume", "screening") == "always" or lead["contact"]["confidence"] == "posting") \
                    and tailor.MASTER_PDF.exists():
                pdf = tailor.MASTER_PDF
        extra = [store.ROOT / x for x in e.get("attachments", [])]   # e.g. a cover letter on a hand-written email
        body_sig = brain.signature(p, attached=bool(pdf or extra)) if pdf or extra else sig
        msg = mailer.build(p["identity"]["name"], lead["contact"]["email"], e["subject"],
                           f"{e['body']}\n\n{body_sig}", attachment=pdf, extra=extra)
        try:
            if mode == "send":
                e["message_id"] = mailer.send(msg)
                lead["status"] = "sent"
                e["sent_at"] = store.iso()
            else:
                e["message_id"] = box.draft(msg)
                lead["status"] = "drafted"
        except Exception as ex:
            log(f"deliver failed for {lead['company']}: {ex}")
            continue
        e["delivered_at"] = store.iso()
        e["mode"] = mode
        store.save(leads)   # a run killed mid-loop must not send this one again
        log(f"{lead['status']}: {lead['company']} <{lead['contact']['email']}> — {e['subject']}")
        if mode == "send":
            time.sleep(random.uniform(10, 30))


def sync(leads, box):
    """Pick up drafts the user sent, replies, and bounces. Returns ids whose check failed."""
    failed = set()
    for lead in leads.values():
        e, c = lead.get("email") or {}, lead.get("contact") or {}
        if lead["status"] not in store.ACTIVE or not c.get("email"):
            continue
        since = (store.parse_iso(e["delivered_at"]) - timedelta(days=1)).strftime("%Y/%m/%d")
        try:
            if lead["status"] != "drafted" and not e.get("message_id"):   # state recovered without it
                e["message_id"] = box.sent_to(c["email"], since)
            if lead["status"] == "drafted":
                mid = box.sent_to(c["email"], since)
                if not mid:
                    continue
                e["message_id"], e["sent_at"], lead["status"] = mid, store.iso(), "sent"
                log(f"sync: {lead['company']} draft was sent")
            if box.bounced(c["email"], since):
                lead["status"] = "bounced"
                log(f"sync: {lead['company']} bounced")
            elif box.replied(c["email"], max(since, lead.get("ignore_replies_before", ""))) or \
                    box.thread_replied([e.get("message_id")] + [f.get("message_id") for f in lead.get("followups", [])],
                                       max(since, lead.get("ignore_replies_before", ""))):   # e.g. an auto-ack
                lead["status"], lead["replied_at"] = "replied", store.iso()
                log(f"sync: {lead['company']} REPLIED")
        except Exception as ex:
            failed.add(lead["id"])
            log(f"sync failed for {lead['company']}: {ex}")
    return failed


def answered_companies(leads):
    """Companies where anyone has ever replied: from then on the user talks to them, volley sends nothing more."""
    return {_company(l) for l in leads.values()
            if l.get("replied_at") or l["status"] in ("replied", "interview", "offer", "rejected")}


def _company(lead):
    return lead.get("domain") or re.sub(r"[^a-z0-9]", "", lead["company"].lower())


def followups(leads, s, p, mode, box, unsynced=()):
    if store.now().weekday() not in s["send_days"]:
        return
    d1, d2 = s["followup_days"]
    sig = brain.signature(p)
    answered = answered_companies(leads)
    for lead in leads.values():
        e = lead.get("email") or {}
        if lead["status"] not in ("sent", "followup_1") or not e.get("sent_at"):
            continue
        if _company(lead) in answered:   # someone there answered (e.g. the founder, while we also asked an engineer)
            continue
        if lead["id"] in unsynced:   # couldn't check for a reply or bounce: never follow up blind
            log(f"follow-up held for {lead['company']}: mailbox sync failed")
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
        store.save(leads)   # a run killed mid-loop must not send this one again
        log(f"follow-up #{n}: {lead['company']}")
        if mode == "send":
            time.sleep(random.uniform(5, 15))
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
        followups(leads, s, p, mode, box, unsynced=sync(leads, box))
        store.save(leads)
    if not no_discover:
        discover(leads, s, p)
        store.save(leads)
    today = store.iso()[:10]
    backlog = sum(1 for l in leads.values() if l["status"] == "queued")
    slots = s["daily_new"] - store.delivered_on(leads, today) - backlog
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("ANTHROPIC_API_KEY not set: skipping research/writing")
        slots = 0
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
