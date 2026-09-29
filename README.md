# volley

Finds startups that are hiring, works out who to email and what to say, and sends ~30 short, researched cold emails a day asking for an internship or entry-level role. Follow-ups, reply tracking, a progress tracker, and resume tailoring when (and only when) it pays off.

**NCR priority:** the candidate is based in Noida, so Delhi NCR (Gurgaon/Gurugram, Noida, Delhi) leads that pay well are pushed to the front — `config/settings.yaml`'s `priority` block sets the pay floor/target (₹8/10 LPA, ₹25k/40k stipend). This includes niche startups that never post an opening: GitHub orgs based in NCR that are still actively shipping, and NCR startups that just closed a funding round.

**Progress:** [TRACKER.md](TRACKER.md) (summary) · [data/tracker.csv](data/tracker.csv) (every lead, sortable on GitHub)

## How a day works

```
00:17 UTC  GitHub Actions: prepare (+01:47 backup)  (free)
           sync Gmail -> follow-ups -> 12 job feeds -> score & dedupe -> crawl sites + GitHub for contacts
           -> work/<date>/batch-N.json

10:00 IST  Claude routine: OUTREACH.md             (Claude credits; 15:00 IST fallback fire)
           Haiku sub-agents analyze each batch file (fit, who to email, the hook, which project)
           main session writes the emails with the cold-email-humanizer skill, tailors a resume only
           when the gates pass, runs `volley check`, pushes claude/outreach-<date>

on push    GitHub Actions: ingest                 (free)
           re-check, render tailored resumes (your Overleaf template, pdfLaTeX, one page), queue, deliver India/Asia now

08:23/14:23 UTC  GitHub Actions: deliver          (free)
           Europe / US send windows, so emails land ~9-11am local
```

- **Sources:** HN Who is Hiring, YC hiring companies (including NCR-based YC startups with no open listing), Unstop (India), Internshala (all of Delhi NCR plus remote, 7 categories, pay floor), GitHub orgs (startups that never post a job: all of NCR first, then India's other tech hubs, a different page of results each day; kept if they shipped in the last year and link a real site, student clubs and universities dropped), funding news from Entrackr, Inc42, ET and YourStory (Indian startups that just raised; NCR ones get priority), Simplify internships + new grad, RemoteOK, Remotive, Arbeitnow, Jobicy, Himalayas. Senior, non-engineering and US-only roles are dropped; the rest are scored on entry-level signals, stack overlap, recency, NCR/India/remote, whether an email is in the post, and how often that source has led to a sent email (Simplify and Arbeitnow rank last).
- **Contacts:** the posting's own address > founder/CTO > engineers at small startups (from GitHub commits) > careers@. Every domain is MX-checked; support@/privacy@/sales@ etc. are never used. Founder addresses may be pattern-guessed (flagged `guessed`).
- **Emails:** under 90 words, one specific observation, one project + link (none for the Basepair internship), one easy question, the resume PDF attached; the signature carries GitHub and the portfolio. `volley/humanize.py` rejects AI tells before anything is pushed.
- **Follow-ups:** day 4 (second project) and day 10 (who should I talk to?), same thread, templated, free. Replies from anyone at the company stop the sequence.
- **Resume tailoring** (`volley/tailor.py`, `prompts/tailor.md`): only for a detailed JD, fit ≥ 8, a recipient who screens resumes (posting address / recruiter / JD asks for a CV), and ≥ 3 exact JD terms the master resume lacks. `check` then verifies the tailored resume against `config/resume.json`: same jobs, dates, education and identity, no new numbers, new skills only from the declared JD terms. Skills are trimmed to what the job needs (a padded list reads as exaggeration), but never below 15 and never dropping one the posting or the resume itself uses. Every first email carries a resume PDF: the tailored one when tailoring ran, otherwise the master (`resume/Aditya_Mittal_Resume.pdf`, rebuilt from `config/resume.json` on every ingest). Follow-ups carry none.

## Setup

1. **Gmail:** 2-Step Verification on, then Google Account → Security → App passwords → create one. Repo → Settings → Secrets and variables → Actions:
   - `GMAIL_ADDRESS`: the Gmail account that sends
   - `GMAIL_APP_PASSWORD`: the app password
   - `SEND_AS` (optional): a different From address you've added in that Gmail under Settings → Accounts → "Send mail as"
2. **Profile:** set `resume_url` (public link to your resume PDF) and `portfolio` in `config/profile.yaml`.
3. **Claude GitHub App:** https://github.com/apps/claude → Configure → give it access to `volley`.
4. **Routine:** https://claude.ai/code/routines → New routine
   - Name `volley daily outreach`, repository `adityamittal-dot/volley`, environment Default, no connectors
   - Model **Sonnet** (writing is its job; the reading is delegated to Haiku sub-agents)
   - Schedule: cron `30 4,9 * * 1-5` (UTC: weekdays 10:00 IST, plus a 15:00 IST fallback that exits at once if the day is done)
   - Prompt: `Follow OUTREACH.md exactly and push today's outreach.`
5. **Mode:** variable `VOLLEY_MODE` = `draft` (emails land in Gmail Drafts, you press send). Switch to `send` once you trust them.

Test once by hand: Actions → volley → Run workflow → `prepare`, then press **Run now** on the routine, then watch Actions → ingest.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m volley discover        # what's out there (no cost)
.venv/bin/python -m volley status          # counts, replies
.venv/bin/python -m volley mark acme interview "call Tue 4pm"
```

## Volume and cost

25 new emails per weekday. Sep 29 - Nov 10 is 31 weekdays: ~775 first emails plus follow-ups. The routine reads ~50 leads/day with Haiku and writes ~25 emails with Sonnet, roughly $0.50-0.75 of credits a day. Everything else runs free on Actions. After `credits_until` (2026-11-10) no new batches are prepared; follow-ups and reply tracking keep running.

Fallback: set `engine: api` in `config/settings.yaml` plus an `ANTHROPIC_API_KEY` secret and the prepare run does the whole thing through the Claude API instead (Haiku analyst, Sonnet writer, hard daily budget cap).

## Tuning

`config/settings.yaml`: `mode`, `daily_new`, `min_fit`, `followup_days`, send windows, sources on/off.
`config/profile.yaml`: projects (the angles), target roles, exclusions. `config/resume.json`: master resume content; `resume/template.tex`: its layout (your Overleaf file). Change the look in the template, the words in the JSON.

Test the whole path on one case without touching any lead: Actions → test-email → Run workflow (sends `test/vidyalai` to an address you choose), or locally `python -m volley test-send you@example.com test/vidyalai` with the Gmail env vars and `pdflatex` installed.
TRACKER.md shows reply rate by angle; after ~100 sends, adjust the projects or the humanizer skill.
