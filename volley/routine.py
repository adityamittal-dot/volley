"""The Claude-routine engine: Actions prepares a batch, the routine thinks, Actions delivers.

  prepare  (Actions, 00:17 UTC)  sync Gmail, follow-ups, discover, crawl the top leads,
                                 write work/<date>/batch-N.json
  routine  (claude.ai/code)      OUTREACH.md: Haiku sub-agents analyze each batch file,
                                 the main session writes emails (+ tailored resumes), runs `check`,
                                 pushes branch claude/outreach-<date>
  ingest   (Actions, on push)    validate again, render resumes, queue emails, deliver this window
"""
import json
import math
import re
import shutil
from types import SimpleNamespace

from . import brain, humanize, mailer, pipeline, research, store, tailor
from .config import ROOT, profile, settings

WORK = ROOT / "work"
CHUNK = 10
SITE_CHARS = 4000
DESC_CHARS = 2500
ANALYSIS_KEYS = {"id": str, "relevant": bool, "summary": str, "stack": list, "hook": str, "angle": str,
                 "angle_reason": str, "fit": int, "region": str, "people": list, "contact_email": str,
                 "contact_name": str, "contact_role": str, "red_flags": list}
REGIONS = {"india", "asia", "europe", "africa", "middle_east", "us", "americas", "other"}
log = pipeline.log


def today():
    return store.iso()[:10]


def _mailbox():
    try:
        return mailer.Mailbox()
    except Exception as e:
        log(f"gmail unavailable ({e}); skipping sync/follow-ups/delivery")
        return None


# ---------- prepare (Actions) ----------

def prepare():
    s, p = settings(), profile()
    leads = store.load()
    box = _mailbox()
    if box:
        pipeline.followups(leads, s, p, s["mode"], box, unsynced=pipeline.sync(leads, box))
        box.close()
    store.save(leads)
    # prepare runs twice (a backup cron covers a dropped or late Actions run); the batch is built once a day.
    if sorted((WORK / today()).glob("batch-*.json")):
        store.export(leads)
        log(f"prepare: today's batch already exists, only synced and sent follow-ups")
        return
    pipeline.discover(leads, s, p)
    log(f"recontact: {pipeline.recontact(leads, s)} no_contact leads back in the pool for the people finder")

    # Leads from a batch the routine never finished go back into the pool.
    for l in leads.values():
        if l["status"] == "researching" and l.get("batch") != today():
            l["status"] = "new"

    backlog = sum(1 for l in leads.values() if l["status"] == "queued")
    slots = s["daily_new"] - store.delivered_on(leads, today()) - backlog
    batch = research_batch(leads, s, slots) if today() <= s["credits_until"] else []
    write_batch(batch)
    for old in sorted(WORK.glob("20*"))[:-14]:
        shutil.rmtree(old)
    store.save(leads)
    store.export(leads)
    log(f"prepare: {len(batch)} leads in today's batch for {slots} open slots")


def _name(company):
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\b(inc|llc|ltd|pvt|gmbh|corp|co)\b\.?", "", company.lower()))


def _emailed_names(leads):
    """The same company can reach the pool twice under different domains (its site vs a job board's)."""
    return {_name(l["company"]) for l in leads.values() if (l.get("email") or {}).get("delivered_at")}


def research_batch(leads, s, slots):
    if slots <= 0:
        return []
    emailed = _emailed_names(leads)
    for l in leads.values():
        if l["status"] == "new" and _name(l["company"]) in emailed:
            l["status"], l["note"] = "skipped", "already emailed under another listing"
    want = math.ceil(slots * s["research_factor"])
    # Retried no_contact leads go ahead of fresh ones of the same tier: most were already rated a good fit.
    retry = today()
    pool = sorted((l for l in leads.values() if l["status"] == "new"),
                  key=lambda l: (l.get("priority", 0), l.get("recontacted") == retry, l["score"]), reverse=True)
    batch = []
    for lead in pool:
        if len(batch) >= want:
            break
        log(f"research: {lead['company']} (score {lead['score']})")
        research.research(lead)
        # Stored so the routine's sandbox and Actions resolve the same contact without DNS.
        lead["domain_mx"] = bool(lead.get("domain")) and research.has_mx(lead["domain"])
        # No address anywhere almost never ends in an email (tested: 0 of 3), so don't spend credits on it —
        # unless it's a priority NCR lead with a real site and a working mailserver: the analyst can still
        # name a founder on the site for a pattern guess.
        keep_blind = lead.get("priority", 0) >= 1 and lead.get("site") and lead["domain_mx"]
        if not lead["candidates"] and not keep_blind:
            lead["status"] = "no_contact"
            lead.pop("site", None)
            continue
        lead["status"], lead["batch"] = "researching", today()
        batch.append(lead)
        ref = referral_split(lead, s)
        if ref:
            leads[ref["id"]] = ref
            batch.append(ref)
    return batch


def _is_engineer(v):
    return v.get("where", "").startswith(("GitHub", "pattern guess (GitHub")) and v["kind"] in ("personal", "guessed")


def referral_split(lead, s):
    """A company with both a decision-maker/hiring address and an engineer's address gets two emails: the
    usual one, and a referral ask to the engineer (commit authors got the best reply rate, 5 of 31). The
    engineer's addresses move to a sibling lead `<id>-ref` so each thread is tracked on its own."""
    if not s.get("referrals") or lead.get("referral"):
        return None
    eng = {e: v for e, v in lead["candidates"].items() if _is_engineer(v)}
    rest = {e: v for e, v in lead["candidates"].items() if e not in eng}
    if not eng or not any(v["kind"] in ("personal", "hiring", "guessed", "generic") for v in rest.values()):
        return None
    lead["candidates"] = rest
    ref = {k: v for k, v in lead.items() if k not in ("site", "research", "contact", "email")}
    ref.update(id=lead["id"] + "-ref", candidates=eng, referral=True, site=(lead.get("site") or ""))
    return ref


def write_batch(batch):
    day = WORK / today()
    day.mkdir(parents=True, exist_ok=True)
    for f in day.glob("batch-*.json"):
        f.unlink()
    for n in range(0, len(batch), CHUNK):
        chunk = []
        for l in batch[n:n + CHUNK]:
            chunk.append({
                "id": l["id"], "company": l["company"], "website": l.get("website", ""),
                "title": l.get("title", ""), "location": l.get("location", ""), "source": l["source"],
                "posted_at": l.get("posted_at"), "posting": (l.get("description") or "")[:DESC_CHARS],
                "site_text": (l.pop("site", "") or "")[:SITE_CHARS],
                "email_candidates": [{"email": e, **v} for e, v in l.get("candidates", {}).items()],
                "pay": l.get("pay"), "priority": l.get("priority", 0), "priority_note": l.get("priority_note", ""),
                "referral": bool(l.get("referral")),
            })
        (day / f"batch-{n // CHUNK + 1}.json").write_text(json.dumps({"leads": chunk}, indent=1, ensure_ascii=False))


# ---------- helpers the routine runs ----------

def brief():
    """What the routine sees first: is there work today, and where."""
    day = WORK / today()
    files = sorted(day.glob("batch-*.json")) if day.exists() else []
    if not files:
        print(f"NO BATCH for {today()} — nothing to do today. Stop here without pushing.")
        return
    if (day / "emails.json").exists():
        print(f"ALREADY DONE for {today()} — today's emails were written and ingested. Stop here without pushing.")
        return
    n = sum(len(json.loads(f.read_text())["leads"]) for f in files)
    s, leads = settings(), store.load()
    backlog = sum(1 for l in leads.values() if l["status"] == "queued")
    slots = s["daily_new"] - store.delivered_on(leads, today()) - backlog
    print(f"date: {today()}\nleads: {n} in {len(files)} files\nemails to write: up to {slots}")
    for f in files:
        print(f"  {f.relative_to(ROOT)}  ->  write {f.with_name(f.name.replace('batch', 'analysis')).relative_to(ROOT)}")


def _load(day, pattern):
    out = {}
    for f in sorted((WORK / day).glob(pattern)):
        for item in json.loads(f.read_text()).get("leads", []):
            out[item["id"]] = item
    return out


def check_analysis(day):
    batch, analysis = _load(day, "batch-*.json"), _load(day, "analysis-*.json")
    problems = []
    for lid, b in batch.items():
        a = analysis.get(lid)
        if not a:
            problems.append(f"{b['company']}: no analysis")
            continue
        for key, typ in ANALYSIS_KEYS.items():
            if not isinstance(a.get(key), typ) or (typ is int and isinstance(a.get(key), bool)):
                problems.append(f"{b['company']}: `{key}` missing or not {typ.__name__}")
        if a.get("region") not in REGIONS:
            problems.append(f"{b['company']}: region must be one of {sorted(REGIONS)}")
        if a.get("relevant") and a.get("angle") not in {pr["id"] for pr in profile()["projects"]}:
            problems.append(f"{b['company']}: unknown angle {a.get('angle')}")
        cands = {c["email"] for c in b["email_candidates"]}
        if a.get("contact_email") and a["contact_email"].lower() not in cands:
            problems.append(f"{b['company']}: contact_email {a['contact_email']} is not in email_candidates "
                            "(leave it empty; people[] is used for a pattern guess)")
    return problems


def shortlist(day=None, show=True):
    """Leads worth an email today, best first, with the contact resolved and tailoring eligibility."""
    day = day or today()
    s = settings()
    leads = store.load()
    batch, analysis = _load(day, "batch-*.json"), _load(day, "analysis-*.json")
    used = {(l.get("contact") or {}).get("email") for l in leads.values() if l["status"] != "researching"}
    emailed = _emailed_names(leads)
    rows = []
    for lid, a in analysis.items():
        lead = leads.get(lid)
        if not lead or lid not in batch or not a.get("relevant") or a.get("fit", 0) < s["min_fit"]:
            continue
        if a.get("red_flags"):
            continue
        if _name(lead["company"]) in emailed:
            continue
        contact = pipeline.pick_contact(lead, _AnalysisView(a), s)
        if not contact or contact["email"] in used:
            continue
        used.add(contact["email"])
        ok, why = tailor.eligibility(lead, a, contact)
        rows.append({"id": lid, "company": lead["company"], "fit": a["fit"], "angle": a["angle"],
                     "referral": bool(lead.get("referral")),
                     "subject_style": humanize.subject_style(lid),
                     "contact": contact, "tailor_eligible": ok, "tailor_note": why,
                     "priority": lead.get("priority", 0), "priority_note": lead.get("priority_note", ""),
                     "pay": lead.get("pay")})
    rank = pipeline.CONFIDENCE_RANK
    rows.sort(key=lambda r: (r["priority"], r["fit"], not r["referral"], rank.get(r["contact"]["confidence"], 0)),
              reverse=True)
    backlog = sum(1 for l in leads.values() if l["status"] == "queued")
    rows = rows[:max(0, s["daily_new"] - store.delivered_on(leads, day) - backlog)]
    if show:
        for r in rows:
            a, c = analysis[r["id"]], r["contact"]
            pay = f"{r['pay']['max'] / 100000:g} LPA" if r["pay"] and r["pay"]["period"] == "year" \
                else f"₹{r['pay']['max']:,}/mo" if r["pay"] else "?"
            print(f"\n## {r['id']} | {r['company']}{' | REFERRAL ASK' if r['referral'] else ''} | fit {r['fit']} | angle {r['angle']} | "
                  f"priority {r['priority']} ({r['priority_note'] or '-'}) | pay {pay}\n"
                  f"to: {c.get('name') or '?'} <{c['email']}> ({c.get('role') or '?'}; {c['confidence']})\n"
                  f"role posted: {batch[r['id']]['title']}\nwhat they do: {a['summary']}\n"
                  f"stack: {', '.join(a['stack']) or '?'}\nhook: {a['hook']}\nwhy this angle: {a['angle_reason']}\n"
                  f"subject style: {r['subject_style']}\n"
                  f"tailor: {'ELIGIBLE' if r['tailor_eligible'] else 'no'} ({r['tailor_note']})")
    return rows


def _AnalysisView(a):
    """pick_contact() expects the API engine's Analysis object; adapt the routine's dicts."""
    people = [SimpleNamespace(name=p["name"], role=p.get("role") or "") for p in a.get("people", []) if p.get("name")]
    return SimpleNamespace(contact_email=a.get("contact_email", ""), contact_name=a.get("contact_name", ""),
                           contact_role=a.get("contact_role", ""), people=people)


def check(day=None):
    """Everything must pass before the routine pushes. Returns problems (empty = pass)."""
    day = day or today()
    problems = check_analysis(day)
    path = WORK / day / "emails.json"
    if not path.exists():
        return problems + ["work/<date>/emails.json missing"]
    emails = {e["id"]: e for e in json.loads(path.read_text()).get("emails", [])}
    short = {r["id"]: r for r in shortlist(day, show=False)}
    links = {pr["link"] for pr in profile()["projects"] if pr.get("link")}
    leads = store.load()
    profile_text = json.dumps(profile())
    for lid, r in short.items():
        e = emails.get(lid)
        if not e:
            problems.append(f"{r['company']}: shortlisted but no email written")
            continue
        for issue in humanize.scan(e.get("subject", ""), e.get("body", ""), style=r["subject_style"],
                                   company=r["company"]):
            problems.append(f"{r['company']}: {issue}")
        body_links = set(re.findall(r"https?://[^\s)]+", e.get("body", "")))
        if body_links - links:
            problems.append(f"{r['company']}: body links must be one of the project links")
        t = e.get("tailor") or {}
        if t.get("decision"):
            if not r["tailor_eligible"]:
                problems.append(f"{r['company']}: tailoring not allowed ({r['tailor_note']})")
            terms = t.get("gap_terms") or []
            if len(terms) < 3:
                problems.append(f"{r['company']}: tailoring needs >= 3 gap_terms")
            if bad := tailor.gap_terms_ok(terms, leads[lid]):
                problems.append(f"{r['company']}: gap_terms must appear in the posting and NOT in the master "
                                f"resume: {bad}")
            rpath = WORK / day / "resumes" / f"{lid}.json"
            if not rpath.exists():
                problems.append(f"{r['company']}: tailored resume {rpath.relative_to(ROOT)} missing")
            else:
                for pr in tailor.verify(json.loads(rpath.read_text()), terms, profile_text,
                                        leads[lid].get("description") or ""):
                    problems.append(f"{r['company']} resume: {pr}")
    for lid in emails:
        if lid not in short:
            problems.append(f"{lid}: email written for a lead that isn't on the shortlist")
    return problems


# ---------- ingest (Actions, after the routine pushes) ----------

def ingest(day=None):
    day = day or today()
    problems = check(day)
    if problems:
        log("check failed, not ingesting:\n  " + "\n  ".join(problems))
        raise SystemExit(1)
    s = settings()
    leads = store.load()
    batch, analysis = _load(day, "batch-*.json"), _load(day, "analysis-*.json")
    short = {r["id"]: r for r in shortlist(day, show=False)}
    emails = {e["id"]: e for e in json.loads((WORK / day / "emails.json").read_text())["emails"]}
    for lid in batch:
        lead, a = leads.get(lid), analysis.get(lid, {})
        if not lead or lead["status"] != "researching":
            continue
        lead["research"] = {k: a.get(k) for k in ("summary", "stack", "hook", "angle", "angle_reason",
                                                  "fit", "region", "people")}
        if lid not in short:
            relevant = a.get("relevant") and (a.get("fit") or 0) >= s["min_fit"]
            lead["status"] = "no_contact" if relevant else "low_fit"
            continue
        e = emails[lid]
        lead["contact"] = short[lid]["contact"]
        lead["email"] = {"subject": e["subject"].strip(), "body": e["body"].strip(), "lint": []}
        t = e.get("tailor") or {}
        if t.get("decision"):
            pdf = WORK / day / "resumes" / f"{lid}.pdf"
            try:
                pages = tailor.render(WORK / day / "resumes" / f"{lid}.json", pdf)
            except RuntimeError as ex:
                pages = f"failed ({ex})"
            if pages == 1:
                lead["email"]["resume_pdf"] = str(pdf.relative_to(ROOT))
                lead["email"]["tailor_reason"] = t.get("reason", "")
            else:
                log(f"{lead['company']}: tailored resume render {pages if isinstance(pages, str) else 'overflowed one page'}, "
                    f"sending the master link instead")
        lead["status"] = "queued"
    store.save(leads)
    log(f"ingest: {sum(1 for l in leads.values() if l['status'] == 'queued')} queued")
    try:   # keep the attached master PDF in step with config/resume.json
        tailor.render(tailor.MASTER, tailor.MASTER_PDF)
    except RuntimeError as ex:
        log(f"master resume render failed ({ex}); attaching the last committed PDF")
    deliver()


def deliver(all_regions=False):
    s, p = settings(), profile()
    leads = store.load()
    box = _mailbox()
    if box:
        pipeline.sync(leads, box)
        pipeline.deliver(leads, s, p, s["mode"], box, all_regions)
        box.close()
    store.save(leads)
    store.export(leads)


# ---------- test-send (manual, Actions) ----------

def test_send(to, case):
    """Run one hand-written case through the real checks, render and message build, and send it to `to`
    instead of the company. `case` holds email.json ({id, fit, angle, contact_email, subject, body, tailor}) and, when
    tailoring, resume.json."""
    if case.startswith("lead:"):   # a queued lead's exact message (attachments included), sent to `to` instead
        lead = store.load()[case[5:]]
        e, p = lead["email"], profile()
        extra = [ROOT / x for x in e.get("attachments", [])]
        pdf = ROOT / e["resume_pdf"] if e.get("resume_pdf") else None
        msg = mailer.build(p["identity"]["name"], to, f"[volley test -> {lead['company']}] {e['subject']}",
                           f"{e['body']}\n\n{brain.signature(p, attached=bool(pdf or extra))}",
                           attachment=pdf, extra=extra)
        mailer.send(msg)
        log(f"test sent to {to}: queued email for {lead['company']} with {len(extra) + bool(pdf)} attachment(s)")
        return
    case = ROOT / case
    e = json.loads((case / "email.json").read_text())
    s, p = settings(), profile()
    lead = store.load()[e["id"]]
    a = {"fit": e["fit"], "angle": e["angle"], "contact_email": e["contact_email"], "people": []}
    contact = pipeline.pick_contact(lead, _AnalysisView(a), s)
    problems = [str(i) for i in humanize.scan(e["subject"], e["body"])]
    pdf = None
    t = e.get("tailor") or {}
    if t.get("decision"):
        ok, why = tailor.eligibility(lead, a, contact)
        problems += [] if ok else [f"tailoring not allowed ({why})"]
        terms = t.get("gap_terms") or []
        if bad := tailor.gap_terms_ok(terms, lead):
            problems.append(f"gap_terms must appear in the posting and NOT in the master resume: {bad}")
        problems += tailor.verify(json.loads((case / "resume.json").read_text()), terms, json.dumps(p),
                                  lead.get("description") or "")
        if not problems:
            pdf = case / "resume.pdf"
            if (pages := tailor.render(case / "resume.json", pdf)) != 1:
                problems.append(f"tailored resume is {pages} pages")
    if problems:
        raise SystemExit("test case fails the checks:\n  " + "\n  ".join(problems))
    if not pdf and settings().get("attach_resume", "screening") == "always" and tailor.MASTER_PDF.exists():
        pdf = tailor.MASTER_PDF   # same as delivery: otherwise the master goes as the signature link
    sig = brain.signature(p, attached=bool(pdf))
    msg = mailer.build(p["identity"]["name"], to,
                       f"[volley test -> {lead['company']} <{contact['email']}>] {e['subject']}",
                       f"{e['body']}\n\n{sig}", attachment=pdf)
    mailer.send(msg)
    log(f"test sent to {to}: {lead['company']} ({contact['email']}, {contact['confidence']}), "
        f"{'resume attached' if pdf else 'master resume link'}")
