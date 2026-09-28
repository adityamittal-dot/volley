# Daily outreach brief (for the Claude routine)

You run volley's thinking step. GitHub Actions has already found today's leads, crawled their sites, and
collected email candidates into `work/<date>/batch-N.json`. Your job: analyze them, write today's cold
emails (and, rarely, a tailored resume), pass `check`, and push. Actions then renders, sends, and follows up.

The credits that pay for you are limited ($20 until 2026-11-10), so work lean: cheap sub-agents do the
reading, you do the writing, no exploring the codebase, no web browsing unless a step says so.

## 0. Setup and brief
```bash
pip install -q -r requirements.txt
python -m volley brief
```
If it prints `NO BATCH` or `ALREADY DONE`, stop now. Do not push anything. (The routine fires twice a day;
the second fire is a fallback for a late batch and normally ends here.)

## 1. Analysis: delegate to Haiku sub-agents
For each `work/<date>/batch-N.json`, launch one sub-agent **with model `haiku`**, all in parallel, with this
prompt (fill in N and the date):

> Read `prompts/analyst.md` (your instructions), `config/profile.yaml` (the candidate), and
> `work/<date>/batch-N.json`. For every lead in the batch file write one analysis object with exactly these
> keys: id, relevant (bool), summary, stack (list), hook, angle, angle_reason, fit (int 1-10), region,
> people (list of {name, role}), contact_email, contact_name, contact_role, red_flags (list, empty if none).
> contact_email must be copied exactly from that lead's email_candidates or be "". Save `{"leads": [...]}` to
> `work/<date>/analysis-N.json`. Do not browse the web. Reply only "done N".

When all are done, run `python -m volley check` once. Fix any **analysis** problems it lists yourself
(small JSON edits); ignore the "emails.json missing" line for now.

## 2. Shortlist
```bash
python -m volley shortlist
```
This is the final list for today: each lead with its resolved contact, the hook, the chosen project
(angle), priority tier and note (e.g. `priority 2 (Noida · 12 LPA)`), stated pay, and whether resume
tailoring is ELIGIBLE. Write an email for every lead on it, nothing else. Leads are already ordered
priority first — Delhi NCR leads that pay well come before everything else.

## 3. Write the emails yourself
Load the `cold-email-humanizer` skill (`.claude/skills/cold-email-humanizer/SKILL.md`) and follow it for
every email. Use `config/profile.yaml` for facts about the candidate (never invent anything) and the
project link for the chosen angle as the one link in the body.

For a Delhi NCR lead (priority >= 1), add one short line that the candidate is based in Noida and can work
from their office — don't force it if the email already reads naturally without it. For a lead with no
listed opening (source note says "no public opening" or "recently funded"), never claim there's a posting;
instead ask whether they take engineering interns or juniors.

Save `work/<date>/emails.json`:
```json
{"emails": [
  {"id": "<lead id>", "subject": "...", "body": "Hi Priya,\n\n...\n\n...?",
   "tailor": {"decision": false, "reason": "", "gap_terms": []}}
]}
```
The body has no signature (Actions appends it).

## 4. Resume tailoring: only when it clearly pays off
Default is **no**. Tailor only a lead marked `ELIGIBLE` in the shortlist (a detailed JD, fit >= 8, and a
recipient who screens resumes against that JD), and only if **all** of these are true:
- The JD names at least 3 concrete requirements the candidate genuinely has (per `config/profile.yaml` and
  `config/resume.json`) whose exact wording does not appear anywhere in `config/resume.json` (ATS matches
  exact strings). These are the `gap_terms`, copied verbatim from the posting.
- Or the role family differs from the master resume's backend/AI framing (e.g. frontend, DevOps/cloud,
  data) *and* you still have 3 such gap terms.
- Reordering and rephrasing true material would visibly change what a recruiter sees in a 6-second skim.

If yes: follow `prompts/tailor.md`, write the resume JSON to `work/<date>/resumes/<id>.json` (same schema
as `config/resume.json`), and set `"tailor": {"decision": true, "reason": "<one sentence>", "gap_terms": [...]}`.
Expect 0-3 tailored resumes on a typical day. When unsure, don't.

## 5. Check, fix, push
```bash
python -m volley check
```
Fix every problem it lists (rewrite the flagged email line, drop an unjustified tailoring decision) and
re-run until it prints `CHECK PASSED`. Then:
```bash
git checkout -b claude/outreach-<date>
git add work/<date>
git commit -m "outreach <date>"
git push -u origin claude/outreach-<date>
```
Only `work/<date>/` changes. Never edit code, config, prompts, or `data/`.
