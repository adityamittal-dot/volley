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

from . import ats
from .sources import extract_emails, remember_boards
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
    """Return (pages: [(url, text)], emails: {email: where}, github_orgs: set). Job-board links seen on
    the way are collected in crawl_site.boards for the ATS registry."""
    pages, emails, orgs = [], {}, set()
    crawl_site.boards = set()
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
        crawl_site.boards |= ats.detect_boards(r.text)
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


FINDER = 2   # bumped when the people finder gets a new way in; no_contact leads below it are retried


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
        if crawl_site.boards:
            lead["ats_boards"] = sorted(f"{k}:{s}" for k, s in crawl_site.boards)
            remember_boards(crawl_site.boards, lead["company"], website)
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
    lead["finder"] = FINDER
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
    shape, _ = known_pattern(domain, {k for k in known if k.partition("@")[2] == domain})
    if shape and (last or shape == "first"):
        return f"{patterns[shape]}@{domain}"
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
            people.append({"name": u["name"], "role": "Engineer", "source": f"GitHub member ({org})", "login": m["login"],
                           "email": email if domain and email.endswith("@" + domain) else ""})
    except (requests.RequestException, ValueError):
        pass
    return people


def _gh_headers():
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return headers


def _company_key(company):
    return re.sub(r"[^a-z0-9]", "", LEGAL_SUFFIX.sub(" ", company or "").lower())


def _same_company(profile, company, domain):
    """A GitHub profile is this person at this company only if its company/blog/bio/email names it."""
    text = " ".join(str(profile.get(k) or "") for k in ("company", "blog", "bio", "email")).lower()
    key, base = _company_key(company), (domain or "").split(".")[0]
    return bool((domain and domain in text) or (len(key) >= 4 and key in re.sub(r"[^a-z0-9]", "", text))
                or (len(base) >= 4 and base in re.sub(r"[^a-z0-9]", "", text)))


_searches = []


def _search_throttle(per_minute=25):
    """GitHub's search API allows 30 a minute; past that it answers 403 and the lookup would just miss."""
    now = time.monotonic()
    _searches[:] = [t for t in _searches if now - t < 60]
    if len(_searches) >= per_minute:
        time.sleep(60 - (now - _searches[0]) + 0.5)
    _searches.append(time.monotonic())


def github_person(name, company, domain, login=None, max_matches=5):
    """A named person's real address from GitHub: find their account (by login, or by full name and a profile
    that names the company), then the profile email or the author email on their own recent commits.
    GitHub dropped email search and commit emails in public events (checked Oct 20), so this is what's left.
    Returns {"email", "login", "on_domain"} or None."""
    headers = _gh_headers()
    try:
        if login:
            u = session.get(f"https://api.github.com/users/{login}", headers=headers, timeout=15)
            profiles = [u.json()] if u.ok else []   # an org member: already known to work there
        else:
            _search_throttle()
            r = session.get("https://api.github.com/search/users", params={"q": f'fullname:"{name}"',
                            "per_page": max_matches}, headers=headers, timeout=15)
            profiles = []
            for item in (r.json().get("items") or [] if r.ok else [])[:max_matches]:
                u = session.get(f"https://api.github.com/users/{item['login']}", headers=headers, timeout=15)
                if u.ok and _same_company(u.json(), company, domain):
                    profiles.append(u.json())
                    break
        if not profiles:
            return None
        prof = profiles[0]
        login = prof["login"]
        emails = [(prof.get("email") or "").lower()]
        repos = session.get(f"https://api.github.com/users/{login}/repos?sort=pushed&per_page=10",
                            headers=headers, timeout=15)
        for repo in [x for x in (repos.json() if repos.ok else []) if not x.get("fork")][:3]:
            c = session.get(f"https://api.github.com/repos/{login}/{repo['name']}/commits",
                            params={"author": login, "per_page": 20}, headers=headers, timeout=15)
            emails += [((x.get("commit") or {}).get("author") or {}).get("email", "").lower()
                       for x in (c.json() if c.ok else [])]
    except (requests.RequestException, ValueError, KeyError):
        return None
    emails = [e for e in dict.fromkeys(emails) if e and "@" in e and "noreply" not in e]
    base = (domain or "").split(".")[0]
    on = [e for e in emails if base and e.split("@")[1].split(".")[0] == base]
    pick = on[0] if on else next((e for e in emails if e.split("@")[1] in FREEMAIL), None)
    if not pick or not has_mx(pick.split("@")[1]):
        return None
    return {"email": pick, "login": login, "on_domain": bool(on)}


def known_pattern(domain, known):
    """(shape, mail host) of the company's addresses if a person-looking one is already known. The host can be a
    sibling of the site's domain: Geektrust's site is geektrust.com but people are @geektrust.in."""
    base = (domain or "").split(".")[0]
    for addr in sorted(known, key=lambda a: a.partition("@")[2] != domain):
        local, _, host = addr.partition("@")
        if not (host == domain or (len(base) >= 4 and host.split(".")[0] == base)) or local in GENERIC_LOCAL:
            continue
        if re.fullmatch(r"[a-z]\.[a-z]+", local):   # before first.last, which would also match j.doe
            return "f.last", host
        if re.fullmatch(r"[a-z]+\.[a-z]+", local):
            return "first.last", host
        if re.fullmatch(r"[a-z]{2,}", local):
            return "first", host
    return None, None


def pattern_example(pattern):
    """A stand-in address of a known shape, so guess_email() reads the shape back off it."""
    return {"first.last": "jane.doe", "f.last": "j.doe", "first": "jane"}[pattern]


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
    # Real addresses first: up to 4 named people (founders before engineers) looked up on GitHub.
    for p in sorted(people, key=lambda p: not re.search(r"founder|ceo|cto", p["role"], re.I))[:4]:
        if p.get("email"):
            continue
        hit = github_person(p["name"], lead.get("company"), domain, login=p.get("login"))
        if hit:
            p["email"], p["github"] = hit["email"], hit["login"]
            p["source"] += " · GitHub commits" if hit["on_domain"] else " · GitHub (personal address)"
    pattern, host = known_pattern(domain, set(cands) | {p["email"] for p in people if p.get("email")})
    for p in people:
        if p.get("email"):
            cands.setdefault(p["email"], {"where": p["source"], "kind": "personal", "name": p["name"],
                                          "role": p["role"]})
            continue
        founder = re.search(r"founder|ceo|cto", p["role"], re.I)
        if not founder and not pattern:
            continue
        if not has_mx(host or domain or "invalid."):
            continue
        guess = guess_email(p["name"], host or domain, set(cands) | ({f"{pattern_example(pattern)}@{host}"} if pattern else set()))
        if guess and guess not in cands:
            cands[guess] = {"where": f"pattern guess ({p['source']})", "kind": "guessed", "name": p["name"],
                            "role": p["role"]}
    lead["people"] = people
    return lead
