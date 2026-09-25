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
import re
import subprocess

from .config import ROOT

MASTER = ROOT / "config" / "resume.json"
RESUME_ASK = re.compile(r"\b(resume|résumé|cv)\b", re.I)
MIN_POSTING_CHARS = 600
MIN_FIT = 8


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


def verify(tailored, gap_terms, profile_text):
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
    # Skills: every master skill kept; new ones only from the declared JD gap terms.
    mskills = {s for items in m["skills"].values() for s in items}
    tskills = {s for items in tailored.get("skills", {}).values() for s in items}
    if missing := mskills - tskills:
        problems.append(f"dropped master skills: {sorted(missing)[:5]}")
    allowed_new = {t.lower() for t in gap_terms}
    for s in tskills - mskills:
        if s.lower() not in allowed_new and not mentions(mt, s):
            problems.append(f"new skill not in master or declared gap terms: {s}")
    # No new numbers anywhere in bullets.
    known = _numbers(json.dumps(m) + profile_text)
    lines = [tailored.get("summary") or ""] + [b for section in ("experience", "projects")
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
    return problems


def render(json_path, pdf_path):
    """Render with the Node/Playwright renderer; returns page count or raises."""
    r = subprocess.run(["node", str(ROOT / "resume" / "render.mjs"), str(json_path), str(pdf_path)],
                       capture_output=True, text=True, cwd=ROOT)
    if r.returncode not in (0, 2):
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return 1 if r.returncode == 0 else 2
