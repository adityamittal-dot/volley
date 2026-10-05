"""Resume tailoring, only where the signals say it pays off.

A tailored resume is worth it when someone will actually screen it against a
specific job description, and the master resume would read wrong for that
role (missing the JD's exact terms, or framed for a different role family).
Everywhere else the master resume link is the better choice: tailoring a
resume nobody reads against a JD costs credits and risks errors.

eligibility() is the hard gate (code, not judgment). verify() checks a
tailored resume stays truthful against the master before it's ever sent.
"""
import json
import pathlib
import re
import shutil
import subprocess
import tempfile

from .config import ROOT

MASTER = ROOT / "config" / "resume.json"
MASTER_PDF = ROOT / "resume" / "Aditya_Mittal_Resume.pdf"   # attached when the posting asks for email applications (settings.attach_resume)
RESUME_ASK = re.compile(r"\b(resume|résumé|cv)\b", re.I)
MIN_POSTING_CHARS = 600
MIN_FIT = 8
MIN_SKILLS = 15   # a tailored skills section is trimmed to what the job needs, never gutted


def master():
    return json.loads(MASTER.read_text())


def master_text(m=None):
    m = m or master()
    return json.dumps(m, ensure_ascii=False).lower()


def eligibility(lead, analysis, contact):
    """(eligible, reason). All must hold: a real JD, strong fit, and a reader who screens resumes."""
    desc = lead.get("description") or ""
    reasons = []
    if lead.get("source") == "yc_hiring" or len(desc) < MIN_POSTING_CHARS:
        reasons.append(f"no detailed job description ({len(desc)} chars)")
    if (analysis.get("fit") or 0) < MIN_FIT:
        reasons.append(f"fit {analysis.get('fit')} < {MIN_FIT}")
    screened = contact.get("confidence") in ("posting", "hiring") or RESUME_ASK.search(desc) \
        or re.search(r"recruit|talent|hiring|hr\b|people", contact.get("role") or "", re.I)
    if not screened:
        reasons.append("recipient isn't screening resumes against this JD")
    return (not reasons, "; ".join(reasons) or "detailed JD, strong fit, resume will be screened")


def mentions(text, term):
    """Whole-word, case-insensitive: "Go" must not match "Google"."""
    return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", text.lower()) is not None


def gap_terms_ok(terms, lead):
    """Each claimed gap term must be in the JD and absent from the master resume."""
    desc, mt = lead.get("description") or "", master_text()
    return [t for t in terms if not mentions(desc, t) or mentions(mt, t)]


def _numbers(text):
    return set(re.findall(r"\d+(?:\.\d+)?\+?", text))


def _base(skill):
    """'AWS (S3, EC2)' -> 'AWS'"""
    return re.sub(r"\s*\(.*\)$", "", skill).strip()


def _subs(skill):
    """'AWS (S3, EC2)' -> {'S3', 'EC2'}"""
    m = re.search(r"\((.*)\)$", skill)
    return {x.strip() for x in m.group(1).split(",")} if m else set()


def verify(tailored, gap_terms, profile_text, posting=""):
    """Return a list of truthfulness problems (empty = OK)."""
    m = master()
    problems = []
    mt = master_text(m)
    # Identity, education, and every job's company/role/dates are fixed.
    for key in ("name", "email", "phone", "github", "linkedin"):
        if tailored.get("identity", {}).get(key) != m["identity"].get(key):
            problems.append(f"identity.{key} changed")
    if tailored.get("education") != m["education"]:
        problems.append("education changed")
    mexp = {(e["company"], e["role"], e["dates"]) for e in m["experience"]}
    texp = {(e.get("company"), e.get("role"), e.get("dates")) for e in tailored.get("experience", [])}
    if texp != mexp:
        problems.append("experience entries must keep the same companies, roles and dates")
    # Skills: a focused subset of the master (a long list reads as padding), plus the declared gap terms.
    allowed_new = {t.lower() for t in gap_terms}
    mskills = {_base(s): _subs(s) for items in m["skills"].values() for s in items}
    tlist = [s for items in tailored.get("skills", {}).values() for s in items]
    for s in tlist:
        base, subs = _base(s), _subs(s)
        if base in mskills:
            if extra := subs - mskills[base] - allowed_new:
                problems.append(f"{s}: {sorted(extra)} not in the master's {base} list")
        elif s.lower() not in allowed_new and not mentions(mt, s):
            problems.append(f"new skill not in master or declared gap terms: {s}")
    if len(tlist) < MIN_SKILLS:
        problems.append(f"only {len(tlist)} skills left; keep at least {MIN_SKILLS}")
    kept = {_base(s) for s in tlist}
    shown = json.dumps([tailored.get("headline")] + [x for sec in ("experience", "projects")
                        for item in tailored.get(sec, []) for x in [item.get("stack", "")] + item.get("bullets", [])])
    for base, subs in mskills.items():
        if base in kept:
            continue
        names = [base] + sorted(subs)
        if posting and any(mentions(posting, n) for n in names):
            problems.append(f"dropped {base}, but the posting asks for it")
        elif mentions(shown, base):
            problems.append(f"dropped {base}, but the resume's own bullets/stacks use it")
    # No new numbers anywhere in bullets.
    known = _numbers(json.dumps(m) + profile_text)
    headline = tailored.get("headline") or ""
    if len(headline) > 50 or headline.count(" | ") > 1:
        problems.append("headline must be a short role title like the master's (one ' | ', max 50 chars)")
    tt = json.dumps(tailored, ensure_ascii=False)
    for t in gap_terms:
        if not mentions(tt, t):
            problems.append(f"declared gap term not used in the resume: {t}")
    lines = [headline] + [b for section in ("experience", "projects")
                                                for item in tailored.get(section, []) for b in item.get("bullets", [])]
    for line in lines:
        if new := _numbers(line) - known:
            problems.append(f"new number {sorted(new)} in: {line[:60]}")
    mprojects = {p["name"]: p for p in m["projects"]}
    for p in tailored.get("projects", []):
        if p.get("name") not in mprojects:
            problems.append(f"unknown project {p.get('name')}")
        elif p.get("live") and p["live"] != mprojects[p["name"]].get("live"):
            problems.append(f"{p['name']} live URL changed")
        elif len(p.get("stack", "")) > len(mprojects[p["name"]]["stack"]) + 4:
            problems.append(f"{p['name']} stack is longer than the master's (the title line won't fit)")
        for tool in (p.get("stack") or "").split(", "):
            if tool and not mentions(mt, tool):
                problems.append(f"{p.get('name')} stack names a tool not in the master resume: {tool}")
    return problems


# ---------- rendering: the Overleaf template, compiled with pdfLaTeX ----------

TEMPLATE = ROOT / "resume" / "template.tex"
TEX_SPECIAL = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
               "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def tex(text):
    """Escape plain text for LaTeX; **bold** becomes \\textbf{bold}, as in the Overleaf source."""
    out = "".join(TEX_SPECIAL.get(c, c) for c in text)
    return re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", out)


def body(r):
    """The document body, in the same commands and spacing as the Overleaf resume."""
    i = r["identity"]
    L = [r"\begin{center}",
         rf"    {{\Huge \scshape {tex(i['name'])}}} \\ \vspace{{4pt}}"]
    if r.get("headline"):
        L.append(rf"    {{\large \scshape {' $|$ '.join(tex(h) for h in r['headline'].split(' | '))}}} \\ \vspace{{2pt}}")
    L += [rf"    {tex(i['location'])} \\ \vspace{{2pt}}",
          r"    \small",
          rf"    {tex(i['phone'])} ~ $|$ ~",
          rf"    \href{{mailto:{i['email']}}}{{\underline{{{tex(i['email'])}}}}} ~ $|$ ~",
          rf"    \href{{{i['github']}}}{{\underline{{GitHub}}}} ~ $|$ ~",
          rf"    \href{{{i['linkedin']}}}{{\underline{{LinkedIn}}}}",
          r"\end{center}", ""]

    L += [r"\section{EDUCATION}", r"\resumeSubHeadingListStart"]
    for ed in r["education"]:
        L += [r"  \resumeSubheading",
              f"    {{{tex(ed['school'])}}}", f"    {{{tex(ed['dates'])}}}",
              f"    {{{tex(ed['degree'])}}}", f"    {{{tex(ed['location'])}}}"]
    L += [r"\resumeSubHeadingListEnd", ""]

    def items(bullets):
        return ([r"  \resumeItemListStart"] + [rf"    \resumeItem{{{tex(b)}}}" for b in bullets]
                + [r"  \resumeItemListEnd"])

    L += [r"\section{EXPERIENCE}", r"\resumeSubHeadingListStart"]
    for n, e in enumerate(r["experience"]):
        L += [r"  \resumeSubheading",
              f"    {{{tex(e['company'])}}}", f"    {{{tex(e['dates'])}}}",
              f"    {{{tex(e['role'])}}}", f"    {{{tex(e['location'])}}}"] + items(e["bullets"])
        if n < len(r["experience"]) - 1:
            L.append(r"  \vspace{2pt}")
    L += [r"\resumeSubHeadingListEnd", ""]

    L += [r"\section{PROJECTS}", r"\resumeSubHeadingListStart"]
    for n, p in enumerate(r["projects"]):
        title = rf"\textbf{{\large {tex(p['name'])}}} $|$ \large{{{tex(p['stack'])}}}"
        if p.get("live"):
            title += rf" $|$ \href{{{p['live']}}}{{\underline{{Live Demo}}}}"
        L += [r"  \resumeProjectHeading", f"    {{{title}}}", f"    {{{tex(p['dates'])}}}"] + items(p["bullets"])
        if n < len(r["projects"]) - 1:
            L.append(r"  \vspace{-10pt}")
    L += [r"\resumeSubHeadingListEnd", ""]

    L += [r"\section{TECHNICAL SKILLS}", r"\small{"]
    L += [rf"\textbf{{{tex(cat)}:}} {tex(', '.join(xs))} \\" for cat, xs in r["skills"].items() if xs]
    L += ["}", ""]

    if r.get("extracurricular"):
        L += [r"\section{EXTRACURRICULAR}", r"\small{", " \\\\\n".join(tex(x) for x in r["extracurricular"]), "}"]
    return "\n".join(L)


def render(json_path, pdf_path):
    """Compile a resume JSON into the Overleaf template; returns the page count or raises."""
    pdf_path = pathlib.Path(pdf_path)
    src = TEMPLATE.read_text().replace("%%BODY%%", body(json.loads(pathlib.Path(json_path).read_text())))
    with tempfile.TemporaryDirectory() as tmp:
        (pathlib.Path(tmp) / "resume.tex").write_text(src)
        run = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                             capture_output=True, text=True, cwd=tmp)
        log = (pathlib.Path(tmp) / "resume.log").read_text(errors="ignore") if run.returncode == 0 else run.stdout
        pages = re.search(r"Output written on resume\.pdf \((\d+) page", log)
        if run.returncode != 0 or not pages:
            err = [l for l in log.splitlines() if l.startswith("!")]
            raise RuntimeError("pdflatex failed: " + ("; ".join(err[:3]) or log[-500:]))
        # Table rows (titles, dates) don't wrap: anything past the margin is text running off the page.
        wide = [float(w) for w in re.findall(r"Overfull \\hbox \(([\d.]+)pt too wide", log)]
        if max(wide, default=0) > 1:
            raise RuntimeError(f"a line runs {max(wide):.0f}pt past the margin (shorten a title/stack/date)")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(pathlib.Path(tmp) / "resume.pdf", pdf_path)
    return int(pages.group(1))
