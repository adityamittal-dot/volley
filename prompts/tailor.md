# Resume tailoring rules

You are adapting the candidate's master resume (`config/resume.json`) for one specific job posting. You
reorder, re-emphasize and rephrase true material; you never write fiction. A false claim can cost him an offer.

1. Never invent a skill, technology, employer, title, date, or metric that isn't in `config/resume.json` or
   `config/profile.yaml`. `volley check` rejects new numbers, changed jobs/dates/identity/education, and
   unknown projects.
2. Never attach a technology to a claim that doesn't name it (an "OAuth race condition via DB-level row
   locking" stays that; it never becomes "PostgreSQL row locking").
3. Never change a number. You may change which bullet leads, not what it claims.
4. Mirror the JD's exact wording where his real experience matches it: this is the point of tailoring.
   Each of your declared `gap_terms` must end up verbatim in the resume (skills line or a bullet).
5. Technical Skills keeps every item from the master (reorder categories freely). The only additions
   allowed are the declared `gap_terms`, and only when they are learnable add-on tools/services the JD
   names (a cloud service, database, library, protocol). Never add a primary programming language or
   framework he hasn't used.
6. Keep both jobs (same company, role and dates, verbatim) and both projects (MedVault, Canopy, with
   their `live` URLs unchanged). Keep education and extracurricular.
7. `headline` is the short role title under the name (master: `Full Stack Engineer | AI Integration`). You
   may reframe it for the role family (e.g. `Backend Engineer | AI Integration`) only if his real work
   supports it: at most one ` | `, 50 characters, no numbers.
8. One page, same shape as the master: 2 bullets for Basepair, 1 for Spelll, 2 per project. Cut whole
   bullets, never words mid-sentence. A project's `name | stack | Live Demo | dates` must fit on one line,
   so keep each `stack` at most as long as the master's. Reorder it or swap in another tool that project
   really used (see `config/profile.yaml`); never a tool it didn't use.
9. Wrap the few phrases a recruiter should see in a 6-second skim in `**double asterisks**` (rendered bold,
   as in the master); 1-3 per bullet. Plain text otherwise, date ranges with a spaced en dash
   (`June 2026 – August 2026`). The PDF is compiled from the candidate's Overleaf template
   (`resume/template.tex`); a line that runs past the margin or a second page is rejected and the master
   link is sent instead.

Output: the full resume JSON in exactly the `config/resume.json` schema, saved to
`work/<date>/resumes/<id>.json`.
