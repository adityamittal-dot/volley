"""Deterministic humanizer scan for cold emails.

Catches the AI tells readers notice (vocabulary clusters, cliché openers,
negative parallelism, stacked triads, em-dash glue) plus cold-email hard rules
(length, link count, placeholders). A non-empty result sends the draft to the
fixer model once.
"""
import re

from .config import profile

BANNED = [
    r"\bdelve", r"\bleverag", r"\butiliz", r"\bseamless", r"\brobust\b", r"\bcutting[- ]edge", r"\binnovative",
    r"\bpassionate", r"\bsynerg", r"\bthrilled", r"\bexcited to\b", r"\bjourney\b", r"\blandscape\b", r"\brealm\b",
    r"\btapestry", r"\btestament\b", r"\bgame[- ]changer", r"\belevate", r"\bunlock", r"\bempower", r"\bdive into",
    r"\bdeep dive", r"\bresonat", r"\bvalue[- ]add", r"\bfast[- ]paced", r"\bdynamic team", r"\bgreat fit\b",
    r"\bperfect fit\b", r"\bin today's\b", r"\bever[- ]evolving", r"\bnavigat", r"\bharness", r"\bspearhead",
    r"\bstreamlin", r"\bpivotal", r"\bcrucial\b", r"\bfoster", r"\bmeticulous", r"\bshowcas",
]
CLICHES = [
    r"hope (this|you).{0,20}(finds|well)", r"\bI wanted to reach out", r"\breaching out\b", r"\bI came across",
    r"\btouch base", r"\bcircle back", r"look(ing)? forward to hearing", r"\bjust following up",
    r"\bjust checking in", r"\bsorry to bother", r"\bI am writing to\b", r"\bmy name is\b",
    r"\bI'?m confident (that )?I", r"\balign(s|ed)? (with|perfectly)", r"\bwould love the opportunity",
    # Opener stock phrases: by Oct 2026 most volley openers were "X caught my eye" / "X stood out", which reads
    # as a template the moment a founder sees two of them.
    r"\bcaught my (eye|attention)", r"\bstood out\b", r"\bgenuinely\b", r"\breally impressed\b",
    r"\bI'?m a (big|huge) fan\b",
]
STRUCTURE = {
    "negative parallelism": r"\bnot (just|only|merely) [^.]{1,60}\b(but|it's)\b|\bit'?s not about\b",
    "exclamation mark": r"!",
    "placeholder": r"\[[A-Z][^\]]{1,30}\]|\{[a-z_ ]+\}",
    "markdown": r"\*\*|^\s*[-*] |^#",
    "sign-off in body": r"\n\s*(best|thanks|regards|cheers|sincerely|warm regards)[,!]?\s*\n?\s*\w*\s*$",
}


def scan(subject, body, max_words=110, max_links=1, style="role", company=""):
    issues = []
    text = f"{subject}\n{body}"
    for pattern in BANNED:
        m = re.search(pattern, text, re.I)
        if m:
            issues.append(f"AI-tell word: '{m.group(0)}'")
    for pattern in CLICHES:
        m = re.search(pattern, text, re.I)
        if m:
            issues.append(f"cliché phrase: '{m.group(0)}'")
    for name, pattern in STRUCTURE.items():
        if re.search(pattern, body, re.I | re.M):
            issues.append(name)
    words = len(body.split())
    if words > max_words:
        issues.append(f"too long: {words} words (max {max_words})")
    if body.count("—") > 1:
        issues.append(f"{body.count('—')} em dashes (max 1)")
    links = re.findall(r"https?://\S+", body)
    if len(links) > max_links:
        issues.append(f"{len(links)} links in body (max {max_links})")
    if len(re.findall(r"\b\w+, \w+,? (and|or) \w+\b", body)) > 1:
        issues.append("stacked lists of three")
    if body.count("?") > 2:
        issues.append("too many questions")
    issues += subject_issues(subject, style=style, company=company)
    return issues


def subject_tail(p=None):
    i = (p or profile())["identity"]
    return f" – {i['name']}"


SUBJECT_STYLES = ("role", "short")


def subject_style(lead_id):
    """A/B test on first-touch subjects, fixed per lead so a re-run picks the same one. 'role' is the formal
    '<Role> – <Name>'; 'short' is '<role> at <Company>', under 45 characters (student-outreach data: subjects
    under 30 characters open ~14 points better than long ones, and identical subjects read as automated)."""
    return SUBJECT_STYLES[int(lead_id[-2:], 16) % 2] if re.fullmatch(r".*[0-9a-f]{2}", lead_id or "") else "role"


def style_of(subject, p=None):
    return "role" if subject.endswith(subject_tail(p)) else "short"


def subject_issues(subject, p=None, style="role", company=""):
    """First-touch subjects take one of two shapes (see subject_style):
    role:  'Full Stack Development Internship – Aditya Mittal'
    short: 'Backend internship at Quill'"""
    if style == "short":
        return _short_subject_issues(subject, company)
    tail = subject_tail(p)
    if not subject.endswith(tail):
        return [f"subject must be '<Role>{tail}'"]
    role = subject[:-len(tail)]
    issues = []
    if not 2 <= len(role.split()) <= 6 or len(role) > 45:
        issues.append(f"subject role '{role}' must be 2-6 words, max 45 characters")
    if not re.search(r"\b(Intern|Internship|Engineer|Developer|Role|Position)s?$", role):
        issues.append(f"subject role '{role}' must end in Internship/Intern/Engineer/Developer/Role/Position")
    if re.search(r"[+?!:@#()\[\]]|https?:|\b(opportunity|application|urgent|re)\b", role, re.I):
        issues.append(f"subject role '{role}' has a symbol or spammy word")
    small = {"and", "of", "in", "for", "the", "to", "&", "/", "-", "–"}
    if any(w[0].islower() and w not in small for w in role.split()):
        issues.append(f"subject role '{role}' must be in Title Case")
    return issues


def _short_subject_issues(subject, company):
    issues = []
    if len(subject) > 45 or not 3 <= len(subject.split()) <= 7:
        issues.append(f"short subject '{subject}' must be 3-7 words, max 45 characters")
    if not re.search(r"\b(intern|internship|engineer|engineering|developer|role|sde|backend|frontend|full[- ]stack)\b",
                     subject, re.I):
        issues.append(f"short subject '{subject}' must name the role (e.g. 'Backend internship at {company or 'Acme'}')")
    word = re.sub(r"\s*\(.*\)", "", company or "").split()
    if word and word[0].lower().strip(".,") not in subject.lower():
        issues.append(f"short subject '{subject}' must name the company ('{word[0]}')")
    if re.search(r"[+!:@#\[\]–—]|https?:|\b(opportunity|application|urgent|re|quick question)\b", subject, re.I):
        issues.append(f"short subject '{subject}' has a symbol or spammy word")
    return issues
