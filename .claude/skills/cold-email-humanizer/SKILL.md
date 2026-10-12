---
name: cold-email-humanizer
description: Write or fix a short cold email from a student engineer to a startup so it reads like a real person wrote it. Use for every volley outreach email before running `python -m volley check`.
---

# Cold email humanizer

Each email goes to one real person who gets dozens of these. It should read like the candidate wrote it
himself in five minutes because he actually looked at their company.

## Shape (evidence below; `volley check` enforces the hard parts)
- **Body 50-80 words** (`check` rejects over 95), 3 short paragraphs of 1-2 sentences. Our own sends to
  Oct 12: 60-79 words got 13% replies, 80-99 got 10%. Founders triage on a phone and read three lines.
- **Greeting:** `Hi <first name>,` whenever the shortlist gives a name, else `Hi <Company> team,`. Emails to
  a named person got 18% replies vs 10% without (57 vs 126 sends), so never drop a name you have.
- **Opener = observation + why now.** One specific, true thing about their product, a technical choice, a
  launch, or a line in the posting, said plainly as a fact or a reaction, not as praise. Where it's true,
  add the honest reason you're writing now: they posted the role, just raised, or are in Noida/Gurgaon/Delhi.
  ("Saw you just raised your seed" is fine and honest; fake familiarity like "been a fan for years" is not.)
  Never open with "X caught my eye", "X stood out", "really impressed", "genuinely" — `check` rejects them;
  they were in most October openers and read as a template by the second email.
- **Proof:** one sentence on the chosen project with one concrete detail and its link (the only link). Tie it
  to what they do in a few words. Proof of work is what founders say they reply to; a claim with nothing to
  click is not proof (the old no-link Basepair angle got 0 replies from 37).
- **Ask:** say plainly he's looking for an internship or entry-level role and can join immediately. Never
  mention a start date, a graduation timeline or "from mid-2027": the company decides when he starts. Where it fits, add the
  `credential` clause from `config/profile.yaml` (the Basepair internship) in the same sentence. End with
  **one** question they can answer in a line:
  - a real posting: name it ("Is the Backend Intern role still open?"). Both of the replies
    that asked about a specific opening (Xero, Uncountable) got answers.
  - no posting (GitHub org, funding news, YC directory): ask if they take interns ("Does the team take on an
    engineering intern?"). Never imply a posting exists.
- **Subject:** `<Role> – Aditya Mittal`. The A/B test is over: this style got 19 replies from 144, the short
  `<role> at <Company>` style 3 from 39. `<Role>` is Title Case, 2-6 words, ending in Internship, Intern,
  Engineer, Developer, Role or Position: the posting's title when it's a clean role name ("Full Stack
  Development Internship"), otherwise "Software Engineering Internship". Never the feed's noise
  ("Engineering (YC Summer 2016)", "See posting"). No hook, pun, "+", emoji, colon, "Opportunity",
  "Application", "Quick question" or "RE:".
- **Real posting first.** Emails that named a real opening got 20% replies vs 10% for "do you take interns".
  When the shortlist's `role posted` is a real title (a YC job posting counts), name it in the question.

## Referral asks (shortlist says `REFERRAL ASK`)
The recipient is an engineer at the company (found through their commits or the GitHub org), not the
person who hires. The company's founder or hiring inbox gets the normal email the same day, so this one
must not read as a second application. Same rules and length, but:
- Open with something specific from *their* work where the research gives it (a repo, the part of the
  product their commits touch), else the company's product.
- The ask is for a pointer, not a job: "Would you be open to passing my name to whoever hires interns, or
  telling me who that is?" One question only.
- Don't flatter or claim to know them. No "I know you're busy".
- Never end a sentence right after a link with a period: `check` reads it as part of the URL.
```
Subject: Software Engineering Internship – Aditya Mittal      (REFERRAL ASK, engineer from commits)

Hi Karan,

Saw your commits on the sync worker in the open-source SDK; batching writes per tenant is a neat fix.

I built MedVault, a Django backend with a Gemini pipeline and a fallback parser for bad model output:
https://medvault-xor.vercel.app

I just finished an SDET internship at Basepair and can join immediately.

Would you be open to passing my name to whoever hires interns at Acme, or telling me who that is?
```
- No sign-off, no signature, no mention or link of resume/GitHub/portfolio/LinkedIn (the signature adds them
  and carries the resume link; a PDF is attached only when the posting asked for email applications).

## Voice (the humanizer; `volley check` rejects violations)
- Plain, direct, a little informal. Contractions are fine. Vary sentence length naturally.
- Banned words: delve, leverage, utilize, seamless, robust, cutting-edge, innovative, passionate, synergy,
  thrilled, excited to, journey, landscape, realm, tapestry, testament, game-changer, elevate, unlock,
  empower, dive into, deep dive, resonate, value-add, fast-paced, dynamic team, great/perfect fit,
  navigate, harness, spearhead, streamline, pivotal, crucial, foster, meticulous, showcase, ever-evolving.
- Banned phrases: "I hope this email finds you well", "I wanted to reach out", "reaching out",
  "I came across", "touch base", "circle back", "look forward to hearing", "I am writing to", "my name is",
  "I'm confident I", "align with", "would love the opportunity", "caught my eye/attention", "stood out",
  "genuinely", "really impressed", "big fan".
- No "not just X but Y" / "it's not about X, it's about Y". No rule-of-three lists. No exclamation marks.
  At most one em dash. No markdown, bullets, or bold. At most two question marks.
- Never swap a banned word for a synonym from the same family; rewrite the sentence plainly.
- Never invent facts about the company or the candidate. If the hook is thin, a specific line from the
  posting is fine.

## Examples
```
Subject: Software Engineering Internship – Aditya Mittal      (a GitHub org with no posting)

Hi Maya,

Your post about moving code search to tree-sitter queries is the same cross-file resolution problem I
hit last month, and you're in Noida too.

I built Canopy, which parses a repo in 10 languages into a call graph and marks unresolved calls as
external instead of guessing: https://canopy-v7hb.onrender.com

I'm a final-year CS student, just finished an SDET internship at Basepair, and can join immediately.
Does the team take on an engineering intern?
```
```
Subject: Backend Engineering Internship – Aditya Mittal      (a real posting)

Hi Rishi,

Your posting says the SQL editor now streams results row by row, which is where most BI tools still
block the UI.

I built MedVault, a Django backend that runs lab reports through a Gemini pipeline with a fixed JSON
schema and a fallback parser for bad model output: https://medvault-xor.vercel.app

I just finished an SDET internship at Basepair and can join immediately. Is the Backend Intern role still
open?
```
