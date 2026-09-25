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
]
STRUCTURE = {
    "negative parallelism": r"\bnot (just|only|merely) [^.]{1,60}\b(but|it's)\b|\bit'?s not about\b",
    "exclamation mark": r"!",
    "placeholder": r"\[[A-Z][^\]]{1,30}\]|\{[a-z_ ]+\}",
    "markdown": r"\*\*|^\s*[-*] |^#",
    "sign-off in body": r"\n\s*(best|thanks|regards|cheers|sincerely|warm regards)[,!]?\s*\n?\s*\w*\s*$",
}


def scan(subject, body, max_words=110, max_links=1):
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
    issues += subject_issues(subject)
    return issues


def subject_tail(p=None):
    i = (p or profile())["identity"]
    return f" – {i['name']}"


def subject_issues(subject, p=None):
    """First-touch subjects have one professional shape: '<Role> – <Name>',
    e.g. 'Full Stack Development Internship – Aditya Mittal'."""
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
