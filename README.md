# volley

Finds startups that are hiring, works out who to email and what to say, and sends 15-20 short, researched cold emails a day asking for an internship or entry-level role. Follow-ups, reply tracking and a progress tracker included. Runs free on GitHub Actions; the only cost is a few cents a day of Claude API.

**Progress:** [TRACKER.md](TRACKER.md) (summary) · [data/tracker.csv](data/tracker.csv) (every lead, sortable on GitHub)

## How a day works

```
 sources (9 free feeds)  ->  score & dedupe  ->  crawl site + GitHub  ->  Haiku: analyst  ->  Sonnet: writer  ->  humanizer check  ->  Gmail
      HN, YC, Unstop,        no LLM, cheap       emails, team pages,     who to email, fit,    60-90 word       regex scan, Haiku     drafts or send,
      Simplify, RemoteOK...   filters             commit authors          hook, which project   first email       fixes if flagged      follow-ups, replies
```

1. **Sync:** checks Gmail for drafts you sent, replies (from the contact or anyone at their domain), and bounces.
2. **Follow-ups:** day 4 (adds your second project and GitHub/portfolio) and day 10 (a polite "who should I talk to?"). Same thread, templated, no API cost. Threads close 7 days after the last one.
3. **Discover:** pulls fresh postings from HN Who is Hiring, YC hiring companies, Unstop (India), Simplify internships + new grad, RemoteOK, Remotive, Arbeitnow, Jobicy, Himalayas. Drops senior/non-engineering/US-only roles, scores the rest on entry-level signals, stack overlap, recency, India/remote, and whether an email is right there in the post.
4. **Research:** for the top leads only, finds the company domain if the posting lacks one (Clearbit autocomplete), reads the homepage and about/team/careers/contact pages (Jina Reader for JS sites), collects every email on them, and pulls commit-author emails from the company's GitHub org. Every address is MX-checked.
5. **Analyst (Claude Haiku 4.5):** decides if it's worth it, picks the best person (posting address > founder/CTO > engineer at a small startup > careers@), finds one specific true observation to open with, picks which of your projects fits, scores fit 1-10, and guesses the recipient's region.
6. **Writer (Claude Sonnet 5):** writes the email following [prompts/writer.md](prompts/writer.md), which encodes what cold-email studies show works (under 100 words, one specific observation, one project, one link, one low-effort question, no attachment).
7. **Humanizer:** [volley/humanize.py](volley/humanize.py) scans for AI tells (vocabulary, clichés, "not just X but Y", triads, em-dash glue, length, links). If anything is flagged, Haiku fixes just those lines.
8. **Deliver:** emails wait for their region's send window (03:00 UTC India/Asia, 08:00 Europe, 14:00 US) so they land around 9-11am local, Mon-Fri, capped at `daily_new` per day.

## Setup

Secrets (repo → Settings → Secrets and variables → Actions):

| Name | Kind | What |
|---|---|---|
| `ANTHROPIC_API_KEY` | secret | from console.anthropic.com → API keys |
| `GMAIL_ADDRESS` | secret | your Gmail address |
| `GMAIL_APP_PASSWORD` | secret | Google Account → Security → 2-Step Verification → App passwords |
| `VOLLEY_MODE` | variable | `draft` (emails land in Gmail Drafts) or `send` |
| `VOLLEY_DAILY_NEW` | variable | optional, overrides `daily_new` |

Fill `identity.resume_url` and `identity.portfolio` in [config/profile.yaml](config/profile.yaml). Everything the writer may say about you comes from that file.

Run it: Actions → volley → Run workflow (tick **dry_run** the first time to get a preview artifact without touching Gmail).

Local:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill it, then: set -a; . ./.env; set +a
.venv/bin/python -m volley discover          # see what's out there, no API cost
.venv/bin/python -m volley run --dry-run     # write emails into data/preview.md
.venv/bin/python -m volley run               # the real thing
.venv/bin/python -m volley status
.venv/bin/python -m volley mark acme interview "call Tue 4pm"
```

## Cost

Per day at 18 emails: ~29 Haiku analyses + 18 Sonnet emails + a few Haiku fixes ≈ $0.20-0.35. `daily_budget_usd` (default $0.60) is a hard stop, and every call is logged in `data/usage.json`. $25 lasts roughly 3 months.

## Tuning

`config/settings.yaml`: `mode`, `daily_new`, `min_fit`, `followup_days`, send windows, models, sources on/off.
`config/profile.yaml`: projects (the "angles"), target roles, exclusions.
Check the "reply rate by angle" table in TRACKER.md after ~100 sends and adjust the projects or the writer prompt.
