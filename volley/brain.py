"""The two model-backed steps: analyze a researched lead (Haiku), write the email (Sonnet)."""
from typing import Literal

import yaml
from pydantic import BaseModel

from . import humanize, llm
from .config import profile, prompt

Region = Literal["india", "asia", "europe", "africa", "middle_east", "us", "americas", "other"]


class Person(BaseModel):
    name: str
    role: str


class Analysis(BaseModel):
    relevant: bool
    summary: str
    stack: list[str]
    hook: str
    angle: str
    angle_reason: str
    fit: int
    region: Region
    people: list[Person]
    contact_email: str
    contact_name: str
    contact_role: str
    red_flags: list[str]


class Email(BaseModel):
    subject: str
    body: str


def candidate_brief(p=None):
    """The candidate block shared by every prompt. Stable text, so it caches."""
    p = p or profile()
    brief = {
        "name": p["identity"]["name"], "school": p["identity"]["school"],
        "graduating": p["identity"]["grad_year"], "based_in": p["identity"]["location"],
        "looking_for": p["ask"], "skills": p["skills"],
        "experience": p["experience"],
        "projects": [{k: v for k, v in pr.items() if k != "repo"} for pr in p["projects"]],
    }
    return yaml.safe_dump(brief, sort_keys=False, allow_unicode=True)


def analyze(lead, s):
    candidates = "\n".join(
        f"- {e} (found on: {v['where']}; type: {v['kind']}"
        + (f"; commit author: {v['name']}, {v['commits']} commits" if v.get("name") else "") + ")"
        for e, v in lead["candidates"].items()
    ) or "(none found)"
    user = (
        f"# Company: {lead['company']}\nWebsite: {lead.get('website') or 'unknown'}\n"
        f"Location: {lead.get('location') or 'unknown'}\nSource: {lead['source']}\n\n"
        f"## Job posting: {lead.get('title', '')}\n{lead.get('description', '')[:3500]}\n\n"
        f"## Website text\n{lead.get('site') or '(could not fetch)'}\n\n"
        f"## Email candidates\n{candidates}\n\n"
        f"Valid angle ids: {', '.join(p['id'] for p in profile()['projects'])}"
    )
    system = prompt("analyst.md").replace("{candidate}", candidate_brief())
    return llm.structured("analyst", s["models"]["analyst"], system, user, Analysis,
                          s["daily_budget_usd"], max_tokens=1500)


def write(lead, s):
    p = profile()
    r, c = lead["research"], lead["contact"]
    project = next((pr for pr in p["projects"] if pr["id"] == r["angle"]), p["projects"][0])
    first = (c.get("name") or "").split(" ")[0] if c.get("name") else ""
    user = (
        f"Company: {lead['company']}\nWhat they do: {r['summary']}\nTheir stack: {', '.join(r['stack']) or 'unknown'}\n"
        f"Role posted: {lead.get('title') or 'none listed'}\n"
        f"Recipient: {c.get('name') or 'unknown'} ({c.get('role') or 'role unknown'})\n"
        f"Greeting to use: {'Hi ' + first + ',' if first else 'Hi ' + lead['company'] + ' team,'}\n\n"
        f"Opener (research note): {r['hook']}\n"
        f"Project to feature: {project['name']} — {project['one_liner']}\n"
        f"Why it's relevant: {r['angle_reason']}\n"
        + (f"The one link to include: {project['link']}\n" if project.get("link")
           else "No link in the body for this one (the signature has GitHub and the portfolio).\n")
    )
    system = prompt("writer.md").replace("{candidate}", candidate_brief(p))
    email = llm.structured("writer", s["models"]["writer"], system, user, Email,
                           s["daily_budget_usd"], max_tokens=800)
    issues = humanize.scan(email.subject, email.body)
    if issues:
        fix_user = (f"## Automated scan\n" + "\n".join(f"- {i}" for i in issues)
                    + f"\n\n## Draft\nSubject: {email.subject}\n\n{email.body}")
        email = llm.structured("fixer", s["models"]["fixer"], prompt("fixer.md"), fix_user, Email,
                               s["daily_budget_usd"], max_tokens=800)
        issues = humanize.scan(email.subject, email.body)
    return email, issues


def signature(p=None, attached=False):
    p = p or profile()
    i = p["identity"]
    lines = [i["name"], f"{i['school'].split(',')[0]} '{str(i['grad_year'])[2:]}, {i['location']}"]
    if attached:
        lines.append(f"Resume attached · GitHub: {i['github']}")
    elif i.get("resume_url"):
        lines.append(f"Resume: {i['resume_url']} · GitHub: {i['github']}")
    else:
        lines.append(f"GitHub: {i['github']}")
    if i.get("portfolio"):
        lines.append(f"Portfolio: {i['portfolio']}")
    return "\n".join(lines)


def followup(lead, n, p=None):
    """Follow-ups are templated: no API spend, and short is what works."""
    p = p or profile()
    c = lead.get("contact") or {}
    first = (c.get("name") or "").split(" ")[0]
    hi = f"Hi {first}," if first else "Hi,"
    if n == 1:
        others = [pr for pr in p["projects"] if pr["id"] != (lead.get("research") or {}).get("angle")
                  and pr["id"] != "basepair"]
        extra = others[0] if others else None
        line = (f" In case it's useful, I also built {extra['name']}: {extra['one_liner']} ({extra['link']})."
                if extra else "")
        return (f"{hi}\n\nBumping this in case it got buried.{line}"
                f"\n\nEven a quick \"not right now\" helps.")
    return (f"{hi}\n\nLast note from me. If the timing's off, no worries at all. Is there someone else at "
            f"{lead['company']} I should talk to about engineering internships or junior roles, "
            f"or should I check back in a few months?")
