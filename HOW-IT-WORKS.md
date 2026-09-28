# How volley works

Everything about volley in one place: what it does each day, where every piece lives, what it costs,
how to run and tune it, and what to do when something breaks.

## Contents
1. [The goal](#1-the-goal)
2. [The daily timeline](#2-the-daily-timeline)
3. [Step by step](#3-step-by-step)
4. [Who does what: models and cost](#4-who-does-what-models-and-cost)
5. [Resume tailoring](#5-resume-tailoring)
6. [The email itself](#6-the-email-itself)
7. [Follow-ups, replies, bounces](#7-follow-ups-replies-bounces)
8. [Where the data lives](#8-where-the-data-lives)
9. [Lead lifecycle (statuses)](#9-lead-lifecycle-statuses)
10. [Files in this repo](#10-files-in-this-repo)
11. [Setup](#11-setup)
12. [Daily use](#12-daily-use)
13. [Configuration reference](#13-configuration-reference)
14. [Safety rails](#14-safety-rails)
15. [Troubleshooting](#15-troubleshooting)
16. [Volume plan until 10 Nov 2026](#16-volume-plan-until-10-nov-2026)

---

## 1. The goal

Get interviews for internships and entry-level engineering roles (2027 grad) by emailing the people who
actually hire at startups (founders, CTOs, engineering leads, recruiters) with a short, specific,
human email, 25 new companies per weekday, and following up automatically.

## 2. The daily timeline

All times are weekdays. IST = UTC + 5:30.

| UTC | IST | What runs | Where | Cost |
|---|---|---|---|---|
| 00:17 | 05:47 | **prepare**: sync Gmail, send due follow-ups, fetch 9 job feeds, score, crawl the best ~40 leads, write `work/<date>/batch-N.json` | GitHub Actions (`volley.yml`) | free |
| 01:47 | 07:17 | **prepare (backup)**: syncs and sends due follow-ups; builds the batch only if the first run never did | GitHub Actions (`volley.yml`) | free |
| 04:30 | 10:00 | **routine**: follows `OUTREACH.md`: Haiku sub-agents analyze, Sonnet writes emails, `volley check`, push `claude/outreach-<date>` | Claude Code routine (claude.ai) | Claude credits |
| on push | | **ingest**: merge, re-check, render tailored resumes, queue emails, deliver India/Asia/other | GitHub Actions (`ingest.yml`) | free |
| 08:23 | 13:53 | **deliver**: Europe/Africa/Middle East window | GitHub Actions (`volley.yml`) | free |
| 09:30 | 15:00 | **routine (fallback)**: stops at once (`ALREADY DONE`) unless the batch was late, then does the day's work | Claude Code routine | a few cents when idle |
| 14:23 | 19:53 | **deliver**: US/Americas window | GitHub Actions (`volley.yml`) | free |

Emails wait in the queue (`status: queued`) until their region's window so they arrive around
9-11am local time, which is when cold emails get read. A window counts as open from 2 hours before
its hour until the end of the UTC day, so an email whose window already passed (a late cron, a
late routine) goes out on the next deliver run instead of waiting forever.

GitHub Actions schedules are best effort: runs at the top of the hour are often delayed by hours or
dropped. That is why every cron here is off the hour, `prepare` has a backup, and the routine has a
fallback fire.

## 3. Step by step

### 3.1 Discover (`volley/sources.py`, `pipeline.discover`)
Pulls every source, no keys needed:

| Source | What it gives |
|---|---|
| HN "Who is hiring?" (monthly) | startup postings, often with a founder's email right in the text |
| YC companies hiring (yc-oss) | ~1,400 YC startups with website and team size, plus any *Active* YC startup with an NCR office even if it isn't listed as hiring |
| Unstop | Indian internships and fresher jobs |
| Internshala | Indian internships/fresher jobs, filtered to NCR cities and the pay floor before the detail page is even fetched |
| GitHub orgs | niche NCR startups that never post an opening — found by org location, kept only if a repo shipped in the last 180 days and the org links a real site |
| Entrackr / Inc42 funding news | NCR startups that just closed a round — worth a cold email before they're flooded with applicants |
| Simplify (Summer 2027 internships, New Grad) | US-heavy internship/new-grad list, last 30 days only |
| RemoteOK, Remotive, Arbeitnow, Jobicy, Himalayas | remote and EU postings |

Reddit and Lever block automated access, so they're not used.

Each posting is scored without any LLM (`pipeline.heuristic`):
- dropped: senior/staff/lead/manager/II titles, non-engineering (sales, design, QA/SDET...), 3+ years
  required without an entry-level signal, postings older than 30 days, stated pay below the NCR floor;
- points for: entry-level words (intern, new grad, junior, fresher...) +4, engineering role +2, your
  stack keywords (up to +3), freshness (+3 today, +2 ≤3 days, +1 ≤7), an email in the post +3,
  a Delhi NCR city +5 (else India +2, remote +1), YC team ≤ 50 +1, YC +2, stated pay at/above target +6
  (else +3 if it clears the floor); US-only/clearance −3.

**NCR priority:** the candidate is based in Noida, so `config/settings.yaml`'s `priority` block (NCR cities,
pay floor/target in LPA and monthly stipend) sets a `priority` tier on every lead — 2 for an NCR lead
with stated pay at or above target, 1 for an NCR lead that clears the pay floor (or has no stated pay but
a good-company signal: YC, a shipping GitHub org, or fresh funding), 0 otherwise. The tier sorts ahead of
fit everywhere a lead queue is ordered (the batch, the shortlist, delivery).

Companies are deduplicated by domain (or name), so a company is never emailed twice. Only the best
400 unworked leads are kept; the rest are rediscovered tomorrow if still posted.

### 3.2 Research (`volley/research.py`)
For the top `30 × 2 = 60` leads (leads with no email address anywhere are dropped here, before any credits are spent — unless it's a priority NCR lead with a crawlable site and a working mailserver, kept for a founder pattern-guess):
1. Find the company domain if the posting has none (Clearbit's free name → domain lookup, exact name match only).
2. Crawl the homepage plus up to 5 about/team/careers/contact/blog pages (Jina Reader for JavaScript sites).
3. Collect every email: `mailto:` links, visible text, obfuscated `name [at] company [dot] com`.
4. Find the company's GitHub org and read commit-author emails from its 3 most recently pushed repos.
5. Drop mailboxes that never lead to hiring (support@, privacy@, sales@, ir@...), and any domain with no MX record.

The result, trimmed to about 1.5k tokens per lead, goes into `work/<date>/batch-N.json` (10 leads per file).

### 3.3 Analyze (Haiku sub-agents in the routine, `prompts/analyst.md`)
One Haiku sub-agent per batch file decides for each company:
- `relevant`: not a staffing agency, job board, crypto/gambling, giant enterprise, or senior-only;
- `summary`, `stack`, and the **hook**: one specific, true observation to open with;
- `angle`: which of your projects fits (Canopy / MedVault / Basepair internship) and why;
- `fit` 1-10 and the recipient's `region`;
- `people` named on the site and the best `contact_email` from the candidates, in this order:
  address the posting says to email > founder/CTO/eng lead > engineer's company email at a startup < 50 people > careers@/jobs@/founders@ > hello@ (tiny startups only).
- `red_flags`: any fee/deposit/bond ask, a staffing agency hiring "for a client", discriminatory wording,
  unpaid/commission-only, no real product, or stated fresher pay below ₹8 LPA. A company with any flag is
  never emailed, whatever else looks good.

### 3.4 Shortlist (`volley shortlist`, code)
Keeps leads with `relevant`, fit ≥ 6, and no `red_flags`, resolves the contact (if no address was found, a
founder's address is guessed from the company's email pattern, e.g. `first@domain`, marked `guessed`),
removes anyone already emailed, ranks by priority tier first, then fit and contact quality, and cuts to
today's open slots. It also marks each lead tailoring-**ELIGIBLE** or not (see section 5).

### 3.5 Write (the routine's main session, Sonnet, `.claude/skills/cold-email-humanizer`)
One email per shortlisted lead into `work/<date>/emails.json`. See section 6 for the rules.

### 3.6 Check (`volley check`, code)
Nothing is pushed until this prints `CHECK PASSED`:
- every batch lead has a well-formed analysis; contact emails were copied from the real candidates;
- every shortlisted lead has an email and no email exists for anyone else;
- the humanizer scan finds nothing (banned words/phrases, length, links, em dashes, triads, subject rules);
- the only link in the body is one of your project links;
- every tailoring decision passes the gates and the tailored resume passes the truth checks (section 5).

### 3.7 Ingest and deliver (`volley ingest`, `volley deliver`)
Actions merges the routine's branch, runs the same check again, renders tailored resumes to PDF
(`resume/template.tex`, your Overleaf template compiled with pdfLaTeX; one page, nothing past the margin), marks emails `queued`, and sends the ones
whose region window is open. The signature is appended automatically:

```
Aditya Mittal
B.Tech CSE '27, Noida, India
Resume: <resume_url>            (or "Resume attached · GitHub: ..." when a tailored PDF is attached)
```

In `draft` mode emails go to your Gmail Drafts; the next sync notices when you've sent them.
In `send` mode volley sends them itself, 10-30 seconds apart (so 30 sends fit the send window).

## 4. Who does what: models and cost

| Task | Who | Why |
|---|---|---|
| Fetch feeds, score, dedupe, crawl sites, find emails, MX checks | GitHub Actions (Python, no LLM) | free; deterministic work needs no model |
| Read ~60 companies, pick contact, hook, angle, fit, red flags | **Haiku** sub-agents | reading and extraction is what cheap models are good at |
| Write ~25 emails, decide/tailor resumes | **Sonnet** (routine main session) | the one thing people actually read |
| Humanizer scan, truth checks, rendering, sending, follow-ups, reply tracking | code | free and exact |

Rough credit use: $0.60-0.90 per weekday → ~$20-29 for 32 weekdays. Watch real
usage at claude.ai; if it runs hot, lower `daily_new` or `research_factor`.

The optional **API engine** (`engine: api` + `ANTHROPIC_API_KEY`) runs the same analysis/writing through
the Claude API from Actions instead (Haiku analyst, Sonnet writer, Haiku fixer) with a hard
`daily_budget_usd` stop and every call logged in `data/usage.json`.

## 5. Resume tailoring

Default: **no tailoring**. Everyone gets the master resume PDF attached. Tailoring costs credits and adds risk,
so it's used only when it clearly helps.

**Gate 1, code (`tailor.eligibility`), all required:**
- a detailed job description (≥ 600 chars, not a generic YC company listing);
- fit ≥ 8;
- a recipient who screens resumes against that JD: the posting's own address, a hiring/careers inbox,
  a recruiter/talent person, or a JD that asks for a resume/CV.

**Gate 2, the writer's judgment (`OUTREACH.md` §4), all required:**
- the JD names ≥ 3 concrete requirements you genuinely have whose exact wording is missing from
  `config/resume.json` (ATS parsers match exact strings), or the role family differs (frontend,
  DevOps/cloud, data) and there are still ≥ 3 such terms;
- reordering/rephrasing true material would visibly change a 6-second recruiter skim.

**Gate 3, truth checks (`tailor.verify`):** the declared gap terms must appear in the JD and not in the
master resume; the tailored resume must keep identity, education, both jobs (company, role, dates)
and projects (live URLs) exactly; trim skills to what the job needs (at least 15, never one the posting
or the resume's own bullets use); add new skills only from the declared gap terms; introduce no new numbers anywhere. It must also render to exactly one page, otherwise the
master link is sent instead.

Tailored resumes are **attached** in place of the master PDF (they go to people screening against a JD, where an attachment is
expected); the rules for writing them are in `prompts/tailor.md`. Expect 0-3 per day.

## 6. The email itself

Based on studies of thousands of student cold emails (under 100 words: ~12% replies; 100-200: ~2%;
personalized: +142%; 42% of replies come from follow-ups).

```
Subject: Software Engineering Internship – Aditya Mittal

Hi Maya,

Your post about moving code search to tree-sitter queries caught my eye, since I hit the same
cross-file resolution problem last month.

I built Canopy, which parses a repo in 10 languages into a call graph and marks unresolved calls as
external instead of guessing: https://canopy-v7hb.onrender.com

I'm a 2027 CS grad looking for an internship or junior role. Is anyone on the search team taking on an
intern this year?

Aditya Mittal
B.Tech CSE '27, Noida, India
Resume: https://...
```

Rules (enforced by `volley/humanize.py`): 60-90 words, one specific observation, one project with one
link, one easy question, a professional subject in one fixed shape (`<Role> – Aditya Mittal`), plain text, the resume PDF attached (tailored or master), no
AI-tell vocabulary or clichés, no "not just X but Y", no triads, no exclamation marks, ≤ 1 em dash.

## 7. Follow-ups, replies, bounces

- **Day 4:** "Bumping this in case it got buried" + your *other* flagship project + GitHub/portfolio.
- **Day 10:** "Last note from me... is there someone else I should talk to, or should I check back later?"
- Both are sent as replies in the same Gmail thread, templated (no credits), only on send days.
- **Reply detection:** any email from the contact, or from anyone at their company domain (newsletters
  and promotions excluded), marks the lead `replied` and stops follow-ups. **You answer replies yourself.**
- **Bounces:** a mailer-daemon message mentioning the address marks the lead `bounced`.
- Threads close 7 days after follow-up #2 (`closed`).

## 8. Where the data lives

| File | What |
|---|---|
| `data/leads.json` | the database: every worked company, its research, contact, email, follow-ups, status |
| `data/tracker.csv` | the same as a table (GitHub renders it, searchable, works on phone) |
| `TRACKER.md` | summary: counts by status, reply rate, reply rate by angle, replies, latest 25 emails |
| `work/<date>/` | the day's batch files, analyses, emails, tailored resumes (JSON + PDF); kept 14 days |
| `data/usage.json` | API-engine spend log (only when `engine: api`) |

Every workflow commits these back to `main`, so the repo history is also an audit log.

## 9. Lead lifecycle (statuses)

```
new ──prepare──> researching ──routine/ingest──> queued ──deliver──> drafted ──(you send)──> sent
 │                   │                              │                                          │
 └> no_contact       ├> low_fit                     └ waits for its region window     day 4 ─> followup_1
                     └> no_contact                                                   day 10 ─> followup_2
                                                                             +7 days, no reply ─> closed
                                                        any time: replied / bounced / interview (manual)
```

`researching` leads whose routine never finished go back to `new` the next morning.

## 10. Files in this repo

| Path | Purpose |
|---|---|
| `OUTREACH.md` | the routine's instructions (what the Claude session does each morning) |
| `.claude/skills/cold-email-humanizer/SKILL.md` | writing rules the routine loads for every email |
| `prompts/analyst.md` | instructions for the Haiku analysts |
| `prompts/tailor.md` | resume tailoring rules |
| `prompts/writer.md`, `prompts/fixer.md` | API-engine writer and humanizer-fix prompts |
| `config/profile.yaml` | you: links, projects (angles), facts the writer may use, target roles, exclusions |
| `config/resume.json` | master resume (source of truth for tailoring) |
| `config/settings.yaml` | mode, volume, windows, sources, models |
| `volley/sources.py` | the 9 feeds |
| `volley/research.py` | domain lookup, site crawl, email discovery, GitHub commits, MX checks, pattern guess |
| `volley/pipeline.py` | scoring, discovery, contact choice, delivery, sync, follow-ups, API-engine run |
| `volley/routine.py` | prepare / brief / shortlist / check / ingest / deliver for the routine engine |
| `volley/tailor.py` | tailoring gates, truth checks, PDF rendering |
| `volley/humanize.py` | the AI-tell / cold-email linter |
| `volley/mailer.py` | Gmail SMTP send, IMAP drafts, sent/reply/bounce detection |
| `volley/brain.py`, `volley/llm.py` | API engine: Claude calls, model routing, budget cap |
| `volley/store.py` | leads.json, tracker.csv, TRACKER.md |
| `resume/template.tex` | your Overleaf resume template; `volley/tailor.py` fills the body from `config/resume.json` |
| `test/<case>/` | hand-written test cases for `volley test-send` / the manual `test-email` workflow |
| `.github/workflows/volley.yml` | prepare (00:17, backup 01:47) and deliver (08:23, 14:23), plus manual runs |
| `.github/workflows/ingest.yml` | runs when the routine pushes |

## 11. Setup

1. **Gmail app password:** turn on 2-Step Verification for the sending Gmail account → Google Account →
   Security → App passwords → create one.
2. **Secrets** (repo → Settings → Secrets and variables → Actions → Secrets):
   `GMAIL_ADDRESS` (the Gmail account that logs in), `GMAIL_APP_PASSWORD`, and optionally `SEND_AS`
   (a different From address added in that Gmail under Settings → Accounts → "Send mail as").
3. **Variables:** `VOLLEY_MODE` = `draft` (already set) or `send`; optional `VOLLEY_DAILY_NEW`.
4. **Profile:** set `resume_url` (a public link to your resume PDF, e.g. Google Drive "anyone with the
   link") and `portfolio` in `config/profile.yaml`.
5. **Claude GitHub App:** https://github.com/apps/claude → Configure → add the `volley` repository.
6. **Routine:** https://claude.ai/code/routines → New routine:
   name `volley daily outreach` · repository `adityamittal-dot/volley` · environment Default ·
   no connectors · model **Sonnet** · schedule cron `30 4,9 * * 1-5` (UTC: 10:00 IST, plus a 15:00 IST fallback) · prompt:
   `Follow OUTREACH.md exactly and push today's outreach.`
7. **First test:** Actions → volley → Run workflow → command `prepare`; when it's green, press
   **Run now** on the routine; then watch Actions → ingest, and check your Gmail Drafts.

## 12. Daily use

- Morning: open Gmail Drafts (while in `draft` mode), skim, send. After a few good days switch to
  `send`: `gh variable set VOLLEY_MODE --body send -R adityamittal-dot/volley`.
- Reply to anyone who answers, yourself, quickly.
- Record progress: `python -m volley mark <company> interview "note"` (or edit the status in
  `data/leads.json`).
- Weekly: read `TRACKER.md`. If one angle gets replies and another doesn't, change the projects in
  `config/profile.yaml` or the rules in the humanizer skill.

## 13. Configuration reference

`config/settings.yaml` (any scalar can be overridden with an env var `VOLLEY_<NAME>`):

| Key | Default | Meaning |
|---|---|---|
| `mode` | draft | `draft` = Gmail Drafts, `send` = send automatically |
| `daily_new` | 25 | new first emails per day |
| `send_days` | Mon-Fri | days that send (0 = Monday) |
| `followup_days` | [4, 10] | follow-up schedule |
| `min_fit` | 6 | analyst fit below this is skipped |
| `research_factor` | 2.0 | leads analyzed per open slot |
| `lead_max_age_days` | 30 | ignore older postings |
| `allow_guessed_emails` | true | founder pattern guesses when nothing better |
| `windows` | 3/8/14 UTC | send windows per region |
| `engine` | routine | `routine` (Claude credits) or `api` (API key) |
| `credits_until` | 2026-11-10 | no new batches after this date |
| `models`, `daily_budget_usd` | | API engine only |
| `sources` | all on | toggle each feed |
| `priority` | cities, pay floor/target | Delhi NCR cities and INR pay floor/target (LPA, monthly stipend) that drive the priority tier |

## 14. Safety rails

- Never the same company or address twice; generic support/sales/privacy inboxes are never emailed.
- Every domain is MX-checked; bounces are detected and stop the thread.
- Hard cap of `daily_new` first emails per day, Mon-Fri only, spaced out when sending.
- `volley check` blocks any email with AI tells or foreign links, and any tailored resume that changes
  facts, adds numbers, or adds skills the JD didn't ask for.
- The routine may only write under `work/<date>/`; Actions re-runs the same check before ingesting.
- Draft mode by default, so nothing goes out without you until you switch it.

## 15. Troubleshooting

| Symptom | Fix |
|---|---|
| A red ✗ in Actions | open it → the red step → read the last lines; `gmail unavailable` = secrets missing/wrong |
| `ingest` failed with `check failed` | the routine pushed something invalid; the leads return to the pool tomorrow. Re-run the routine, or run ingest by hand after fixing `work/<date>/` |
| No drafts appearing | check `VOLLEY_MODE`, the Gmail secrets, and that the time is inside a send window (or run `deliver` with `all_regions`) |
| Routine says `NO BATCH` | `prepare` didn't run today (weekend, or it failed); run it by hand |
| Too few emails a day | raise `research_factor`, lower `min_fit`, or turn `allow_guessed_emails` on |
| Replies marked wrongly | edit the status in `data/leads.json` or use `volley mark` |

## 16. Volume plan until 10 Nov 2026

- 32 weekdays from Mon 28 Sep to Tue 10 Nov.
- 30 first emails/day → **~960 companies** contacted.
- Follow-ups (free): ~600 day-4 + ~480 day-10 minus replies/bounces → **~1,000 more emails**.
- Total ≈ **1,650 emails**, with Claude credits used only for the ~640 first emails (+ analysis).
- After 10 Nov no new batches are prepared; follow-ups and reply tracking keep running until the
  last threads close.
