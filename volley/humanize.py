"""Deterministic humanizer scan for cold emails.

Catches the AI tells readers notice (vocabulary clusters, cliché openers,
negative parallelism, stacked triads, em-dash glue) plus cold-email hard rules
(length, link count, placeholders). A non-empty result sends the draft to the
fixer model once.
"""
import re

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
    if len(subject.split()) > 8:
        issues.append(f"subject too long ({len(subject.split())} words)")
    if re.search(r"\b(opportunity|application|urgent|re:)\b", subject, re.I):
        issues.append("spammy subject word")
    return issues
