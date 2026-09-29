---
name: cold-email-humanizer
description: Write or fix a short cold email from a student engineer to a startup so it reads like a real person wrote it. Use for every volley outreach email before running `python -m volley check`.
---

# Cold email humanizer

Each email goes to one real person who gets dozens of these. It should read like the candidate wrote it
himself in five minutes because he actually looked at their company.

## Shape (evidence: <100 words gets ~12% replies, 100-200 gets ~2%)
- **Body 60-90 words**, 3-4 short paragraphs of 1-2 sentences.
- **Greeting:** `Hi <first name>,` if the shortlist gives a name, else `Hi <Company> team,`.
- **Opener:** the hook from the shortlist, in his own words. One specific, true observation about their
  product, a technical choice, a launch, or a line in the posting. No compliment, no "I came across".
- **Proof:** one sentence on the chosen project with one concrete detail and its link (the only link). The Basepair
  internship has no link: no link in those emails, never the GitHub profile instead.
  Tie it to what they do in a few words, don't explain the connection at length.
- **Ask:** say plainly he's a 2027 grad looking for an internship or entry-level role, then end with **one**
  low-effort question they can answer in a line ("Is anyone on the team taking on an intern this year?").
- **Subject:** always `<Role> – Aditya Mittal` (`volley check` enforces it). `<Role>` is
  the role you're asking for, cleaned up in Title Case, 2-6 words, ending in Internship, Intern, Engineer,
  Developer, Role or Position: use the posting's title when it's a clean role name ("Full Stack Development
  Internship", "Backend Engineer Intern"), otherwise the closest plain one ("Software Engineering
  Internship"). Never the feed's noise ("Engineering (YC Summer 2016)", a URL, "See posting"), never a
  hook, pun, question, "+", emoji, "Opportunity", "Application" or "RE:". The body carries the specifics.
- No sign-off, no signature, no mention or link of resume/GitHub/portfolio/LinkedIn (the signature adds them).

## Voice (the humanizer; `volley check` rejects violations)
- Plain, direct, a little informal. Contractions are fine. Vary sentence length naturally.
- Banned words: delve, leverage, utilize, seamless, robust, cutting-edge, innovative, passionate, synergy,
  thrilled, excited to, journey, landscape, realm, tapestry, testament, game-changer, elevate, unlock,
  empower, dive into, deep dive, resonate, value-add, fast-paced, dynamic team, great/perfect fit,
  navigate, harness, spearhead, streamline, pivotal, crucial, foster, meticulous, showcase, ever-evolving.
- Banned phrases: "I hope this email finds you well", "I wanted to reach out", "reaching out",
  "I came across", "touch base", "circle back", "look forward to hearing", "I am writing to", "my name is",
  "I'm confident I", "align with", "would love the opportunity".
- No "not just X but Y" / "it's not about X, it's about Y". No rule-of-three lists. No exclamation marks.
  At most one em dash. No markdown, bullets, or bold. At most two question marks.
- Never swap a banned word for a synonym from the same family; rewrite the sentence plainly.
- Never invent facts about the company or the candidate. If the hook is thin, a specific line from the
  posting is fine.

## Example
```
Subject: Software Engineering Internship – Aditya Mittal

Hi Maya,

Your post about moving code search to tree-sitter queries caught my eye, since I hit the same
cross-file resolution problem last month.

I built Canopy, which parses a repo in 10 languages into a call graph and marks unresolved calls as
external instead of guessing: https://canopy-v7hb.onrender.com

I'm a 2027 CS grad looking for an internship or junior role. Is anyone on the search team taking on an
intern this year?
```
