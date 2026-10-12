"""Company research and contact discovery, no LLM involved.

For each lead: read the homepage plus a few about/team/careers/contact pages,
collect every email address on them, and pull commit-author emails from the
company's GitHub org. The analyst model then picks the best person from these
candidates; nothing here guesses.
"""
import os
import re
import time
from urllib.parse import urljoin, urlparse

import dns.resolver
import requests
from bs4 import BeautifulSoup

from .sources import extract_emails
from .store import domain_of

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
PAGE_HINTS = re.compile(r"about|team|company|founder|people|careers|jobs|join|contact|hiring|blog", re.I)
GENERIC_LOCAL = {"info", "hello", "hi", "contact", "support", "help", "team", "admin", "sales", "press",
                 "media", "privacy", "legal", "security", "billing", "abuse", "noreply", "no-reply",
                 "careers", "jobs", "hiring", "hr", "recruiting", "talent", "founders", "people", "office",
                 "partnerships", "partners", "partner", "marketing", "events", "community", "feedback",
                 "compliance", "grievance", "dpo", "investors", "ir", "accounts", "finance", "enquiries",
                 "inquiries", "enquiry", "business", "bd", "care", "customercare", "service", "orders",
                 "webmaster", "postmaster", "mail", "email", "contactus", "career", "recruitment", "recruiter",
                 "dev", "developer", "developers", "engineering", "tech", "it", "git", "github", "ops", "devops"}
HIRING_LOCAL = {"careers", "career", "jobs", "hiring", "recruiting", "recruitment", "recruiter", "talent",
                "founders", "hr", "people"}
JOB_BOARD_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "wellfound.com", "linkedin.com",
                   "ycombinator.com", "news.ycombinator.com", "remoteok.com", "remotive.com", "himalayas.app",
                   "jobicy.com", "arbeitnow.com", "unstop.com", "simplify.jobs", "github.com", "notion.site",
                   "google.com", "forms.gle", "typeform.com", "breezy.hr", "smartrecruiters.com", "bamboohr.com",
                   "internshala.com",
                   # funding_news links the article, never the company's own site
                   "entrackr.com", "inc42.com", "indiatimes.com", "yourstory.com")
# Mailboxes that never lead to a hiring conversation; dropped before the analyst sees them.
BLOCKED_LOCAL = {"support", "help", "privacy", "legal", "security", "billing", "abuse", "noreply", "no-reply",
                 "press", "media", "sales", "ir", "investors", "compliance", "grievance", "dpo", "accounts",
                 "finance", "orders", "care", "customercare", "service", "webmaster", "postmaster", "feedback",
                 "marketing", "partnerships", "partners", "partner", "events", "donate", "billing"}
FREEMAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "proton.me", "protonmail.com", "icloud.com"}

session = requests.Session()
session.headers["User-Agent"] = UA


def _get(url, timeout=15):
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        if r.ok and "text/html" in r.headers.get("content-type", ""):
            return r
    except requests.RequestException:
        pass
    return None


def _text(soup):
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "form"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def _jina(url):
    """JS-rendered sites return an empty shell; Jina Reader renders them for free."""
    try:
        r = session.get(f"https://r.jina.ai/{url}", timeout=30)
        return r.text if r.ok else ""
    except requests.RequestException:
        return ""


def is_job_board(url):
    host = domain_of(url)
    return any(host == h or host.endswith("." + h) for h in JOB_BOARD_HOSTS)


def has_mx(domain, _cache={}):
    if domain not in _cache:
        try:
            dns.resolver.resolve(domain, "MX", lifetime=8)
            _cache[domain] = True
        except Exception:
            _cache[domain] = False
    return _cache[domain]


def classify(email, domain):
    local, _, host = email.partition("@")
    if local in HIRING_LOCAL:
        return "hiring"
    if local in GENERIC_LOCAL:
        return "generic"
    if domain and (host == domain or host.endswith("." + domain)):
        return "personal"
    return "personal" if host in FREEMAIL else "other"


def crawl_site(website, max_pages=5):
    """Return (pages: [(url, text)], emails: {email: where}, github_orgs: set)."""
    pages, emails, orgs = [], {}, set()
    home = _get(website)
    if not home:
        text = _jina(website)
        if text:
            pages.append((website, text[:8000]))
            for e in extract_emails(text):
                emails.setdefault(e, website)
        return pages, emails, orgs

    base = home.url
    queue, seen = [home], {base}
    soup = BeautifulSoup(home.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"].strip())
        if href.startswith("mailto:"):
            continue
        if domain_of(href) == domain_of(base) and PAGE_HINTS.search(urlparse(href).path) and href not in seen:
            seen.add(href)
            if len(seen) <= max_pages:
                r = _get(href)
                if r:
                    queue.append(r)
            time.sleep(0.3)

    for r in queue:
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                addr = href[7:].split("?")[0].strip().lower()
                if "@" in addr:
                    emails.setdefault(addr, r.url)
            m = re.match(r"https?://(?:www\.)?github\.com/([A-Za-z0-9-]+)/?$", href)
            if m and m.group(1).lower() not in ("features", "about", "sponsors", "orgs", "topics"):
                orgs.add(m.group(1))
        raw = r.text
        text = _text(soup)
        if len(text) < 300 and r is home:
            text = _jina(r.url) or text
        for e in extract_emails(raw + " " + text):
            emails.setdefault(e, r.url)
        pages.append((r.url, text[:5000]))
    return pages, emails, orgs


def github_emails(org, domain, limit=40):
    """Commit-author emails from the org's most recently pushed repos."""
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    found = {}
    try:
        repos = session.get(f"https://api.github.com/orgs/{org}/repos?sort=pushed&per_page=3",
                            headers=headers, timeout=15)
        if not repos.ok:
            repos = session.get(f"https://api.github.com/users/{org}/repos?sort=pushed&per_page=3",
                                headers=headers, timeout=15)
        for repo in (repos.json() if repos.ok else [])[:3]:
            if repo.get("fork"):
                continue
            commits = session.get(f"https://api.github.com/repos/{org}/{repo['name']}/commits?per_page={limit}",
                                  headers=headers, timeout=15)
            for c in commits.json() if commits.ok else []:
                a = (c.get("commit") or {}).get("author") or {}
                email, name = (a.get("email") or "").lower(), a.get("name") or ""
                if not email or "noreply" in email or "[bot]" in name or "bot@" in email:
                    continue
                entry = found.setdefault(email, {"name": name, "commits": 0})
                entry["commits"] += 1
    except (requests.RequestException, ValueError):
        pass
    # Company-domain committers are the strongest signal; personal addresses only if frequent.
    return {e: v for e, v in found.items() if (domain and e.endswith("@" + domain)) or v["commits"] >= 3}


LEGAL_SUFFIX = re.compile(r"\b(private|pvt\.?|limited|ltd\.?|llp|llc|inc\.?|corp\.?|gmbh|technologies|technology"
                          r"|solutions|labs|software|services|india|co\.?)\b", re.I)


def find_domain(company):
    """Company name -> homepage domain via Clearbit's free autocomplete. Only accepts a name match."""
    name = LEGAL_SUFFIX.sub(" ", company).strip(" .,-")
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    if len(key) < 3:
        return ""
    try:
        r = session.get("https://autocomplete.clearbit.com/v1/companies/suggest", params={"query": name}, timeout=10)
        hits = r.json() if r.ok else []
    except (requests.RequestException, ValueError):
        return ""
    for hit in hits[:3]:
        hkey = re.sub(r"[^a-z0-9]", "", (hit.get("name") or "").lower())
        if hit.get("domain") and hkey in (key, key + "ai", key + "hq", key + "app", key + "labs"):
            return hit["domain"]
    return ""


def research(lead):
    """Fill lead['site'] with page text and lead['candidates'] with contact candidates."""
    if not lead.get("domain"):
        found = find_domain(lead["company"])
        if found:
            lead["domain"], lead["website"] = found, f"https://{found}"
    website = lead.get("website") or ""
    domain = lead.get("domain") or domain_of(website)
    candidates = {}
    for e in lead.get("emails", []):
        candidates[e] = {"where": "job posting", "kind": classify(e, domain)}

    pages, orgs = [], set()
    if website:
        pages, site_emails, orgs = crawl_site(website)
        for e, where in site_emails.items():
            candidates.setdefault(e, {"where": where, "kind": classify(e, domain)})
    if lead.get("source") == "github_orgs" and lead.get("url", "").startswith("https://github.com/"):
        orgs.add(lead["url"].rstrip("/").rsplit("/", 1)[1])   # the org the lead came from, even if the site doesn't link it
    for org in sorted(orgs, key=lambda o: o not in (lead.get("url") or ""))[:2]:
        for e, v in github_emails(org, domain).items():
            candidates.setdefault(e, {"where": f"GitHub commits ({org})", "kind": classify(e, domain),
                                      "name": v["name"], "commits": v["commits"]})

    # Drop junk and anything that can't receive mail.
    clean = {}
    for e, v in candidates.items():
        local, host = e.split("@", 1)
        if local in BLOCKED_LOCAL or local.startswith(("support", "noreply", "no-reply", "privacy")):
            continue
        if v["kind"] == "other" and "job posting" not in v["where"]:
            continue
        if has_mx(host):
            clean[e] = v
    lead["candidates"] = clean
    lead["site"] = "\n\n".join(f"[{u}]\n{t}" for u, t in pages)[:14000]
    lead["github_orgs"] = sorted(orgs)
    # No person on the site: look up founders (YC) and engineers (GitHub org) by name.
    if not any(v["kind"] in ("personal", "hiring") for v in clean.values()):
        find_people(lead)
    return lead


def guess_email(name, domain, known):
    """Infer the company's address pattern from a known personal address, else first@domain."""
    import unicodedata
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    parts = [p for p in re.sub(r"[^a-z ]", "", name.lower().replace("-", "")).split() if p]
    if not parts or not domain:
        return None
    first, last = parts[0], parts[-1] if len(parts) > 1 else ""
    patterns = {"first": first, "first.last": f"{first}.{last}", "firstlast": f"{first}{last}",
                "flast": f"{first[0]}{last}", "f.last": f"{first[0]}.{last}"}
    for addr in known:
        local, _, host = addr.partition("@")
        if host != domain:
            continue
        for kname, pattern in patterns.items():
            if not last and kname != "first":
                continue
            # Recognize the pattern by shape, e.g. "jane.doe" -> first.last
            if (kname == "first.last" and re.fullmatch(r"[a-z]+\.[a-z]+", local)) or \
               (kname == "f.last" and re.fullmatch(r"[a-z]\.[a-z]+", local)) or \
               (kname == "first" and re.fullmatch(r"[a-z]+", local) and local not in GENERIC_LOCAL):
                return f"{pattern}@{domain}"
    return f"{first}@{domain}"


# ---------- people: who actually works there ----------
# A named person replies about twice as often as a role inbox (Sep 29-Oct 12: 18% vs 10% of 183 sends), and
# most no_contact leads had a site but no address on it. These find names, and only then guess an address.

ENG_YC_ROLE = re.compile(r"\b(eng|engineer|developer|software|backend|frontend|full[- ]?stack|ml|ai|data)\b", re.I)
JUNIOR_YC = re.compile(r"new grad|intern|any|0|1 year", re.I)


def yc_profile(url):
    """Founders (name, title, LinkedIn) and engineering job postings from a ycombinator.com/companies page."""
    m = re.match(r"https?://(?:www\.)?ycombinator\.com/companies/([a-z0-9-]+)", url or "")
    if not m:
        return [], []
    r = _get(f"https://www.ycombinator.com/companies/{m.group(1)}")
    page = re.search(r'data-page="([^"]+)"', r.text) if r else None
    if not page:
        return [], []
    import html
    import json
    try:
        props = json.loads(html.unescape(page.group(1)))["props"]
    except (ValueError, KeyError):
        return [], []
    founders = [{"name": f["full_name"], "role": f.get("title") or "Founder", "source": "YC profile",
                 "linkedin": f.get("linkedin_url") or ""}
                for f in (props.get("company") or {}).get("founders") or [] if f.get("full_name")]
    jobs = [{"title": j["title"].strip(), "url": "https://www.ycombinator.com" + j["url"],
             "location": j.get("location") or "", "experience": j.get("minExperience") or "",
             "visa": j.get("visa") or ""}
            for j in props.get("jobPostings") or []
            if ENG_YC_ROLE.search(f"{j.get('role')} {j.get('title')}") and JUNIOR_YC.search(j.get("minExperience") or "")]
    return founders, jobs


def github_members(org, domain, limit=5):
    """Public members of the company's GitHub org: engineers who could refer. Their profile email is
    used when it's on the company domain; otherwise only the name comes back."""
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    people = []
    try:
        r = session.get(f"https://api.github.com/orgs/{org}/public_members?per_page={limit}", headers=headers,
                        timeout=15)
        for m in (r.json() if r.ok else [])[:limit]:
            u = session.get(f"https://api.github.com/users/{m['login']}", headers=headers, timeout=15)
            u = u.json() if u.ok else {}
            if not u.get("name"):
                continue
            email = (u.get("email") or "").lower()
            people.append({"name": u["name"], "role": "Engineer", "source": f"GitHub member ({org})",
                           "email": email if domain and email.endswith("@" + domain) else ""})
    except (requests.RequestException, ValueError):
        pass
    return people


def known_pattern(domain, known):
    """The company's address shape if a person-looking address on its domain is already known."""
    for addr in known:
        local, _, host = addr.partition("@")
        if host != domain or local in GENERIC_LOCAL:
            continue
        if re.fullmatch(r"[a-z]+\.[a-z]+", local):
            return "first.last"
        if re.fullmatch(r"[a-z]\.[a-z]+", local):
            return "f.last"
        if re.fullmatch(r"[a-z]{2,}", local):
            return "first"
    return None


def find_people(lead):
    """Add named people (and, where safe, their addresses) to lead['candidates'] / lead['people'].
    Founders get a first@domain guess when the domain takes mail; engineers only when the company's
    address pattern is already known from a real address, since a wrong guess bounces."""
    domain = lead.get("domain") or domain_of(lead.get("website") or "")
    cands = lead.setdefault("candidates", {})
    people = []
    founders, jobs = yc_profile(lead.get("url"))
    people += founders
    if jobs and re.search(r"Engineering \(YC|no public opening", lead.get("title") or ""):
        j = next((j for j in jobs if re.search(r"intern|new grad|junior|graduate", j["title"], re.I)), jobs[0])
        lead["title"], lead["url"] = j["title"], j["url"]
        lead["description"] = (f"YC job posting: {j['title']} ({j['location']}; experience: {j['experience']}; "
                               f"{j['visa']}). " + (lead.get("description") or ""))[:2000]
    for org in (lead.get("github_orgs") or [])[:1]:
        people += github_members(org, domain)
    if not domain or not has_mx(domain):
        lead["people"] = people
        return lead
    pattern = known_pattern(domain, cands)
    for p in people:
        if p.get("email"):
            cands.setdefault(p["email"], {"where": p["source"], "kind": "personal", "name": p["name"],
                                          "role": p["role"]})
            continue
        founder = re.search(r"founder|ceo|cto", p["role"], re.I)
        if not founder and not pattern:
            continue
        guess = guess_email(p["name"], domain, cands.keys())
        if guess and guess not in cands:
            cands[guess] = {"where": f"pattern guess ({p['source']})", "kind": "guessed", "name": p["name"],
                            "role": p["role"]}
    lead["people"] = people
    return lead
