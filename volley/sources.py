"""Fetch fresh job/lead postings from free, keyless sources for cold outreach.

Each source has its own fetch_<key>() function returning a list of lead
dicts with a fixed set of keys (see _lead()). fetch_all() runs the enabled
ones, isolating failures per source so one broken API doesn't kill the rest.
"""

import html
import json
import random
import urllib.parse
import xml.etree.ElementTree as ET
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

TIMEOUT = 25

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
})


def _get(url, **kwargs):
    """GET with a shared session, one retry on any request failure."""
    kwargs.setdefault("timeout", TIMEOUT)
    last_exc = None
    for _ in range(2):
        try:
            r = SESSION.get(url, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_exc = e
    raise last_exc


# ---------------------------------------------------------------- helpers --

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(s):
    """Unescape entities and strip tags, collapsing whitespace."""
    if not s:
        return ""
    s = html.unescape(s)
    s = _TAG_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def _desc(s):
    return strip_html(s)[:4000]


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_JUNK_DOMAINS = {"example.com", "example.org", "example.net", "sentry.io", "wixpress.com"}
_JUNK_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp")


def _is_junk_email(email):
    local, _, domain = email.partition("@")
    if not domain:
        return True
    if domain in _JUNK_DOMAINS:
        return True
    if "noreply" in local or "no-reply" in local:
        return True
    if domain.endswith(_JUNK_EXT):
        return True
    return False


def extract_emails(text):
    """Find emails in text, including HN-style obfuscation like
    "jane [at] acme [dot] com" or "jane (at) acme.com". Lowercased,
    deduped, junk (example.com, noreply, image-filename false positives)
    filtered out.
    """
    if not text:
        return []
    # normalize bracket/paren/brace obfuscation of "at"/"dot" -> @ / .
    # (plain " at "/" dot " word substitution is skipped: too many false
    # positives in ordinary prose like "join us at acme.com")
    t = re.sub(r"\s*[\(\[\{]\s*at\s*[\)\]\}]\s*", "@", text, flags=re.I)
    t = re.sub(r"\s*[\(\[\{]\s*dot\s*[\)\]\}]\s*", ".", t, flags=re.I)

    out = []
    seen = set()
    for m in _EMAIL_RE.findall(t):
        e = m.lower().strip(".,;:")
        if e in seen or _is_junk_email(e):
            continue
        seen.add(e)
        out.append(e)
    return out


# ------------------------------------------------------------------ pay ----

_LPA_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d{1,2}(?:\.\d+)?)\s*(?:(?:-|–|to)\s*(\d{1,2}(?:\.\d+)?)\s*)?"
                     r"(?:lpa|lakhs? per annum|lakhs?\s*(?:/|per)\s*(?:year|annum)|l\s*/\s*(?:yr|year|annum))", re.I)
_RUPEE_RE = re.compile(r"₹\s*([\d,]{4,})(?:\s*(?:-|–|to)\s*₹?\s*([\d,]{4,}))?\s*(?:/|per)\s*(year|annum|month)", re.I)


def pay(period, lo, hi=None):
    """{"period": "year"|"month", "min": INR, "max": INR}, or None when nothing usable was stated."""
    lo, hi = int(lo or 0), int(hi or lo or 0)
    if hi <= 0:
        return None
    return {"period": period, "min": lo or hi, "max": max(lo, hi)}


def parse_pay(text):
    """Stated Indian pay in free text: '₹ 10,00,000 - 12,00,000 /year', '₹ 40,000 /month', '8-12 LPA'."""
    if not text:
        return None
    m = _RUPEE_RE.search(text)
    if m:
        n = lambda s: int(s.replace(",", "")) if s else 0
        period = "month" if m.group(3).lower() == "month" else "year"
        return pay(period, n(m.group(1)), n(m.group(2)))
    m = _LPA_RE.search(text)
    if m:
        lo = float(m.group(1)); hi = float(m.group(2) or lo)
        if 1 <= lo <= hi <= 99:
            return pay("year", lo * 100000, hi * 100000)
    return None


def _lead(company, title, raw_description, url, website, location, posted_at, source, extra_email_text="",
          pay=None, yc=False):
    """Build a lead dict. raw_description is scanned for emails BEFORE
    html-stripping (so mailto: links inside <a href> survive), then
    stripped/truncated for the description field.
    """
    emails = extract_emails((raw_description or "") + " " + (extra_email_text or ""))
    return {
        "pay": pay or parse_pay(strip_html(raw_description)),
        "yc": yc,
        "company": company or "",
        "title": title or "",
        "description": _desc(raw_description),
        "url": url or "",
        "website": website or "",
        "location": location or "",
        "posted_at": posted_at,
        "source": source,
        "emails": emails,
    }


_UNSTOP_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} GMT[+-]\d{4}$")


def normalize_date(value):
    """Normalize a unix timestamp (int/float/numeric str) or an ISO-ish
    date string into ISO-8601 UTC. Returns None if unparseable/empty.
    """
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        v = str(value).strip()
        if not v:
            return None
        if v.isdigit():
            return datetime.fromtimestamp(int(v), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        if _UNSTOP_DATE_RE.match(v):
            dt = datetime.strptime(v, "%Y-%m-%d %H:%M:%S GMT%z")
            return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


# ------------------------------------------------------------------ HN ----

_HN_JOB_BOARD_HOSTS = (
    "news.ycombinator.com", "indeed.com", "linkedin.com", "glassdoor.com",
    "angel.co", "wellfound.com", "ashbyhq.com", "lever.co", "greenhouse.io",
    "workable.com", "breezy.hr",
)

_LOC_KEYWORDS = (
    "remote", "onsite", "on-site", "hybrid", "usa", "united states", "uk ",
    "canada", "india", "europe", "bay area", "nyc", "sf ", "san francisco",
    "new york", "london", "berlin", "toronto",
)


def _hn_looks_like_location(s):
    low = s.lower()
    if any(k in low for k in _LOC_KEYWORDS):
        return True
    if re.match(r"^[A-Za-z .]+,\s*[A-Z]{2}$", s.strip()):
        return True
    return False


def _hn_website_from_html(raw):
    for href in re.findall(r'href="([^"]+)"', raw):
        parsed = urlparse(href)
        host = parsed.netloc.lower()
        if not host or any(jb in host for jb in _HN_JOB_BOARD_HOSTS):
            continue
        return f"{parsed.scheme}://{host}"
    return ""


def fetch_hn_hiring():
    r = _get("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10")
    hits = r.json().get("hits", [])
    thread = next((h for h in hits if "who is hiring" in (h.get("title") or "").lower()), None)
    if not thread:
        return []

    r = _get(f"https://hn.algolia.com/api/v1/items/{thread['objectID']}")
    children = r.json().get("children", []) or []

    leads = []
    for c in children:
        raw = c.get("text")
        if not raw or len(raw) < 40:
            continue
        header_html = re.split(r"<p>", raw, maxsplit=1, flags=re.I)[0]
        header = strip_html(header_html)
        parts = [p.strip() for p in header.split("|")]
        company = parts[0] if parts and parts[0] else ""
        title = "See posting"
        if len(parts) > 1 and parts[1] and not _hn_looks_like_location(parts[1]):
            title = parts[1]
        location = ""
        for p in parts[2:]:
            if _hn_looks_like_location(p):
                location = p
                break
        url = f"https://news.ycombinator.com/item?id={c['id']}"
        website = _hn_website_from_html(raw)
        posted_at = normalize_date(c.get("created_at"))
        leads.append(_lead(company, title, raw, url, website, location, posted_at, "hn_hiring"))
    return leads


# ------------------------------------------------------------------ YC ----

_NCR_RE = re.compile(r"gurgaon|gurugram|noida|delhi", re.I)


def fetch_yc_hiring():
    r = _get("https://yc-oss.github.io/api/companies/hiring.json")
    companies = r.json()
    # YC startups with an office in Delhi NCR are worth a cold email even without an open listing.
    try:
        hiring = {c.get("id") for c in companies}
        companies += [c for c in _get("https://yc-oss.github.io/api/companies/all.json").json()
                      if c.get("id") not in hiring and c.get("status") == "Active"
                      and _NCR_RE.search(c.get("all_locations") or "")]
    except (requests.RequestException, ValueError):
        pass

    leads = []
    for c in companies:
        team_size = c.get("team_size")
        if team_size is not None and team_size > 200:
            continue
        batch = c.get("batch") or ""
        title = f"Engineering (YC {batch})" if batch else "Engineering (YC)"

        parts = []
        if team_size is not None:
            parts.append(f"Team size: {team_size}.")
        if c.get("one_liner"):
            parts.append(c["one_liner"])
        if c.get("long_description"):
            parts.append(c["long_description"])
        extra = ", ".join((c.get("tags") or []) + (c.get("industries") or []) + (c.get("regions") or []))
        if extra:
            parts.append(extra)
        description = " ".join(parts)

        url = c.get("url") or c.get("website") or ""
        website = c.get("website") or ""
        location = c.get("all_locations") or ", ".join(c.get("regions") or [])

        leads.append(_lead(c.get("name", ""), title, description, url, website, location, None, "yc_hiring",
                           yc=True))
    return leads


# -------------------------------------------------------------- remoteok --

def _fix_mojibake(s):
    # remoteok's API frequently double-encodes UTF-8 as Latin-1 (e.g.
    # "MecÃ¡nico" instead of "Mecánico"); repair it when detected.
    if not s or "Ã" not in s:
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def fetch_remoteok():
    r = _get("https://remoteok.com/api")
    items = r.json()

    leads = []
    for item in items:
        if "position" not in item:
            continue  # skip the legal-notice metadata entry
        desc = _fix_mojibake(item.get("description", ""))
        tags = item.get("tags") or []
        if tags:
            desc = f"{desc} Tags: {', '.join(tags)}"
        url = item.get("url") or item.get("apply_url") or ""
        posted_at = normalize_date(item.get("date"))
        leads.append(_lead(
            _fix_mojibake(item.get("company", "")), _fix_mojibake(item.get("position", "")), desc,
            url, "", _fix_mojibake(item.get("location", "")), posted_at, "remoteok",
        ))
    return leads


# -------------------------------------------------------------- remotive --

def fetch_remotive():
    r = _get("https://remotive.com/api/remote-jobs?category=software-dev&limit=150")
    jobs = r.json().get("jobs", [])

    leads = []
    for j in jobs:
        desc = j.get("description", "")
        tags = j.get("tags") or []
        if tags:
            desc = f"{desc} Tags: {', '.join(tags)}"
        posted_at = normalize_date(j.get("publication_date"))
        leads.append(_lead(
            j.get("company_name", ""), j.get("title", ""), desc,
            j.get("url", ""), "", j.get("candidate_required_location", ""),
            posted_at, "remotive",
        ))
    return leads


# ------------------------------------------------------------- arbeitnow --

def fetch_arbeitnow():
    r = _get("https://www.arbeitnow.com/api/job-board-api")
    jobs = r.json().get("data", [])

    leads = []
    for j in jobs:
        desc = j.get("description", "")
        extras = (j.get("tags") or []) + (j.get("job_types") or [])
        if extras:
            desc = f"{desc} {', '.join(extras)}"
        location = j.get("location", "")
        if j.get("remote"):
            location = f"{location} (Remote)".strip() if location else "Remote"
        posted_at = normalize_date(j.get("created_at"))
        leads.append(_lead(
            j.get("company_name", ""), j.get("title", ""), desc,
            j.get("url", ""), "", location, posted_at, "arbeitnow",
        ))
    return leads


# --------------------------------------------------------------- jobicy --

def fetch_jobicy():
    try:
        r = _get("https://jobicy.com/api/v2/remote-jobs?count=100&tag=developer")
        jobs = r.json().get("jobs") or []
        if not jobs:
            raise ValueError("empty jobs with tag filter")
    except Exception:
        r = _get("https://jobicy.com/api/v2/remote-jobs?count=100")
        jobs = r.json().get("jobs", [])

    leads = []
    for j in jobs:
        desc = j.get("jobDescription") or j.get("jobExcerpt") or ""
        level = j.get("jobLevel")
        if level:
            desc = f"Level: {level}. {desc}"
        posted_at = normalize_date(j.get("pubDate"))
        leads.append(_lead(
            j.get("companyName", ""), j.get("jobTitle", ""), desc,
            j.get("url", ""), "", j.get("jobGeo", ""), posted_at, "jobicy",
        ))
    return leads


# ------------------------------------------------------------- himalayas --

def fetch_himalayas():
    r = _get("https://himalayas.app/jobs/api?limit=100")
    jobs = r.json().get("jobs", [])

    leads = []
    for j in jobs:
        desc = j.get("description") or j.get("excerpt") or ""
        url = j.get("applicationLink") or j.get("guid") or ""
        locations = j.get("locationRestrictions") or []
        location = ", ".join(locations)
        posted_at = normalize_date(j.get("pubDate"))
        leads.append(_lead(
            j.get("companyName", ""), j.get("title", ""), desc,
            url, "", location, posted_at, "himalayas",
        ))
    return leads


# --------------------------------------------------------------- unstop --

_UNSTOP_OPPORTUNITIES = ("internships", "jobs")
_UNSTOP_TERMS = (
    "software engineer", "backend", "full stack", "python", "react",
    "ai engineer", "sde", "node", "software developer", "fresher", "graduate engineer",
    # added 2026-10-12: more of India's paid entry-level engineering roles
    "software intern", "machine learning", "data science", "frontend", "django", "devops", "sdet",
    "qa automation", "java developer", "golang", "generative ai", "android developer", "flutter", "web developer",
)


def _unstop_pay(jd):
    if not jd or jd.get("not_disclosed") or not jd.get("show_salary"):
        return None
    period = "month" if jd.get("pay_in") == "monthly" else "year"
    return pay(period, jd.get("min_salary"), jd.get("max_salary"))


def _unstop_experienced_only(it):
    """Jobs whose eligibility lists only 2+ years of experience and no fresher batch."""
    try:
        import json
        el = json.loads((it.get("regnRequirements") or {}).get("eligibility") or "{}")
    except ValueError:
        return False
    years = [int(m) for x in el.get("experience") or [] for m in re.findall(r"^(\d+)", str(x))]
    return bool(years) and min(years) >= 2 and not el.get("fresherPassoutYearsSelected")


def fetch_unstop():
    leads = []
    seen_ids = set()

    for opportunity in _UNSTOP_OPPORTUNITIES:
        for term in _UNSTOP_TERMS:
            url = (
                "https://unstop.com/api/public/opportunity/search-result"
                f"?opportunity={opportunity}&per_page=50&oppstatus=open&searchTerm={quote(term)}"
            )
            try:
                r = _get(url)
                items = (r.json().get("data") or {}).get("data") or []
            except Exception:
                time.sleep(0.3)
                continue  # one bad search term/page shouldn't kill the whole source

            for it in items:
                oid = it.get("id")
                if oid in seen_ids:
                    continue
                seen_ids.add(oid)

                job_detail = it.get("jobDetail") or {}
                if opportunity == "jobs" and _unstop_experienced_only(it):
                    continue
                paid_unpaid = job_detail.get("paid_unpaid")
                if paid_unpaid == "unpaid":
                    continue
                if paid_unpaid is None and it.get("isPaid") is False:
                    continue

                company = (it.get("organisation") or {}).get("name", "")
                post_url = it.get("seo_url") or (
                    f"https://unstop.com/{it['public_url']}" if it.get("public_url") else ""
                )
                locations = it.get("locations") or []
                cities = [l.get("city") for l in locations if l.get("city")]
                location = ", ".join(cities) if cities else "India"
                posted_at = normalize_date(it.get("approved_date") or it.get("updated_at"))

                title = it.get("title", "")
                if opportunity == "internships" and "intern" not in title.lower():
                    title += " (Internship)"
                leads.append(_lead(
                    company, title, it.get("details", ""),
                    post_url, "", location, posted_at, "unstop", pay=_unstop_pay(job_detail),
                ))
            time.sleep(0.3)

    return leads


# ----------------------------------------------------------- internshala --
# India's largest internship/fresher board. Listing cards carry the stated salary or stipend; only cards
# that clear the pay floor get their detail page fetched (full JD + the company's own website).

_INTERNSHALA_CITIES = ("delhi", "gurgaon", "noida", "greater-noida", "ghaziabad", "faridabad")
_INTERNSHALA_CATS = ("computer-science", "software-development", "web-development", "python-django",
                     "full-stack-development", "backend-development", "machine-learning",
                     # added 2026-10-12, each verified to list Delhi roles: more of NCR's paid listings
                     "javascript-development", "react-js-development", "node-js-development", "data-science",
                     "artificial-intelligence-ai", "front-end-development", "software-testing", "devops",
                     "flutter-development")
_INTERNSHALA_PAGES = (
    [f"https://internshala.com/{kind}/{cat}-{noun}-in-{city}/"
     for city in _INTERNSHALA_CITIES
     for kind, noun in (("fresher-jobs", "jobs"), ("internships", "internship"))
     for cat in _INTERNSHALA_CATS]
    # the biggest lists run past one page of 50
    + [f"https://internshala.com/{kind}/{cat}-{noun}-in-{city}/page-{n}/"
       for city in ("delhi", "gurgaon", "noida")
       for kind, noun in (("fresher-jobs", "jobs"), ("internships", "internship"))
       for cat in ("computer-science", "software-development", "web-development")
       for n in (2, 3)]
    # remote internships from anywhere in India, same pay floor
    + [f"https://internshala.com/internships/work-from-home-{cat}-internships/"
       for cat in ("computer-science", "software-development", "web-development", "python-django",
                   "machine-learning", "full-stack-development")]
)
_INTERNSHALA_FLOOR = {"year": 800000, "month": 25000}   # overridden by settings.priority via fetch_all
_AGO_RE = re.compile(r"(\d+|a|few)\s*(hour|day|week|month)s?\s*ago|just now|today", re.I)


def _internshala_posted(text):
    m = _AGO_RE.search(text or "")
    if not m:
        return None
    if not m.group(1):
        return normalize_date(int(time.time()))
    n = 1 if m.group(1).lower() in ("a", "few") else int(m.group(1))
    unit = {"hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400}[m.group(2).lower()]
    return normalize_date(int(time.time() - n * unit))


def fetch_internshala():
    from bs4 import BeautifulSoup
    cards, seen = [], set()
    for url in _INTERNSHALA_PAGES:
        try:
            soup = BeautifulSoup(_get(url).text, "html.parser")
        except requests.RequestException:
            continue
        for c in soup.select(".individual_internship[data-href]"):
            href = c["data-href"]
            if href in seen:
                continue
            seen.add(href)
            text = lambda sel: (c.select_one(sel).get_text(" ", strip=True) if c.select_one(sel) else "")
            money = text(".row-1-item .mobile") or text(".stipend")
            p = parse_pay(money + (" /month" if "internship" in url and "/" not in money else ""))
            if not p or p["max"] < _INTERNSHALA_FLOOR[p["period"]]:
                continue
            cards.append({"href": href, "title": text(".job-internship-name"), "company": text(".company-name"),
                          "location": text(".locations"), "pay": p, "internship": "/internship/" in href,
                          "posted": _internshala_posted(text(".status-info") or text(".status-inactive"))})
        time.sleep(0.5)

    leads = []
    for card in cards:
        detail_url = "https://internshala.com" + card["href"]
        desc, website = "", ""
        try:
            soup = BeautifulSoup(_get(detail_url).text, "html.parser")
            d = soup.select_one(".internship_details")
            desc = d.get_text(" ", strip=True) if d else ""
            a = soup.select_one(".website_link a")
            website = a["href"] if a and a.get("href", "").startswith("http") else ""
        except requests.RequestException:
            pass
        title = card["title"] + (" Internship" if card["internship"] and "intern" not in card["title"].lower() else "")
        leads.append(_lead(card["company"], title, desc, detail_url, website, card["location"], card["posted"],
                           "internshala", pay=card["pay"]))
        time.sleep(0.5)
    return leads


# ------------------------------------------------------------ github_orgs --
# Niche startups that never post an opening: found by org location on GitHub, kept only if they still
# ship (a repo pushed in the last year) and link a real company site. The best-converting source, so it
# searches all of Delhi NCR first, then India's other tech hubs, and pages through a different slice of
# the results each day (the first page alone returns the same orgs every morning).

_GH_NCR = ("noida", "gurgaon", "gurugram", "delhi", "new delhi", "ghaziabad", "faridabad", "ncr")
_GH_HUBS = ("bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "chennai", "india")
_GH_PAGES = 20           # rotate through all 20 pages (1000 orgs, the search API's cap) per location
_GH_ORG_CAP = 320        # org detail lookups per run (2 API calls each; Actions' token allows 1000/hour)
_GH_PUSH_CUTOFF_SECONDS = 365 * 86400
_KNOWN_URLS = set()      # lead URLs already in leads.json (set by fetch_all): skipped before any lookup
# Student clubs, universities, communities and public bodies also register orgs by city: never employers here.
_GH_NOT_A_COMPANY = re.compile(
    r"\b(iit|iiit|nit|nsut|dtu|igdtuw|jiit|amity|university|univ|college|institute|school|students?|"
    r"society|chapter|community|communities|meetup|gdsc|dsc|mlsa|acm|ieee|hackathon|hacktoberfest|conference|"
    r"py[a-z]*delhi|foss|department|dept|govt|government|ministry|army|police|ngo|foundation)\b", re.I)
_NOT_A_SITE_HOSTS = (
    "github.com", "github.io", "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "medium.com", "notion.site", "linktr.ee", "angel.co", "wellfound.com", "indeed.com", "glassdoor.com",
    "naukri.com", "crunchbase.com", "bit.ly", "t.me", "youtube.com",
)


def _gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _gh_is_real_site(url):
    if not url:
        return False
    host = urlparse(url if "//" in url else f"https://{url}").netloc.lower()
    return bool(host) and not any(j in host for j in _NOT_A_SITE_HOSTS)


def _gh_search(city, page, headers):
    url = (f"https://api.github.com/search/users?q=type:org+location:{quote(city)}+repos:%3E2"
           f"&per_page=50&page={page}")
    try:
        items = _get(url, headers=headers).json().get("items", [])
    except (requests.RequestException, ValueError):
        return []
    finally:
        time.sleep(2.1)   # search API: 30 requests/minute with a token
    return [i["login"] for i in items if i.get("login")]


def fetch_github_orgs():
    headers = _gh_headers()
    day = datetime.now(timezone.utc).timetuple().tm_yday
    ncr, hubs, seen = [], [], set()
    for cities, out in ((_GH_NCR, ncr), (_GH_HUBS, hubs)):
        for city in cities:
            # NCR gets three slices a day, the other hubs two: the whole list comes round in about a week.
            step = 3 if out is ncr else 2
            for page in sorted({1 + (day * step + k * _GH_PAGES // step) % _GH_PAGES for k in range(step)}):
                for login in _gh_search(city, page, headers):
                    if login not in seen and f"https://github.com/{login}" not in _KNOWN_URLS:
                        seen.add(login)
                        out.append(login)
    logins = ncr + hubs   # NCR first: the cap cuts from the other hubs

    leads = []
    cutoff = time.time() - _GH_PUSH_CUTOFF_SECONDS
    for login in logins[:_GH_ORG_CAP]:
        try:
            org = _get(f"https://api.github.com/orgs/{login}", headers=headers).json()
            website = org.get("blog") or ""
            if not _gh_is_real_site(website):
                continue
            if _GH_NOT_A_COMPANY.search(f"{login} {org.get('name') or ''} {org.get('description') or ''} {website}"):
                continue
            repos = _get(f"https://api.github.com/orgs/{login}/repos?sort=pushed&per_page=5",
                         headers=headers).json()
        except requests.RequestException:
            continue
        if not repos:
            continue
        pushed_at = normalize_date(repos[0].get("pushed_at"))
        try:
            pushed_secs = datetime.fromisoformat(pushed_at.replace("Z", "+00:00")).timestamp() if pushed_at else 0
        except ValueError:
            pushed_secs = 0
        if pushed_secs < cutoff:
            continue

        langs = sorted({r.get("language") for r in repos if r.get("language")})
        topics = sorted({t for r in repos for t in (r.get("topics") or [])})
        parts = [org.get("description") or ""]
        if langs:
            parts.append("Languages: " + ", ".join(langs))
        if topics:
            parts.append("Topics: " + ", ".join(topics))
        description = ". ".join(p for p in parts if p)
        website_url = website if website.startswith("http") else f"https://{website}"
        leads.append(_lead(
            org.get("name") or login, "Software Engineering (no public opening)", description,
            f"https://github.com/{login}", website_url, org.get("location") or "", None, "github_orgs",
            extra_email_text=org.get("email") or "", yc=False,
        ))
    return leads


# ------------------------------------------------------------- funding_news --
# Freshly-funded Indian startups: worth a cold email before they're flooded with applicants, even though
# they haven't posted an opening yet. Every Indian round counts; the NCR ones get the priority tier
# (the location is read from the article text, since headlines rarely name the city).

_FUNDING_FEEDS = (
    "https://entrackr.com/rss",                                               # full article text
    "https://inc42.com/feed/",
    "https://inc42.com/tag/funding/feed/",
    "https://economictimes.indiatimes.com/tech/funding/rssfeeds/78570540.cms",
    "https://yourstory.com/feed",
    # Google News search as a proxy for publishers with no working feed of their own (VCCircle, Moneycontrol,
    # ET Startups): verified 2026-10-12, ~50 current items a day. Links are Google redirects, so the article
    # isn't fetched; research finds the company's site by name.
    "https://news.google.com/rss/search?q=startup+raises+funding+india+when:2d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=(gurugram+OR+noida+OR+delhi)+startup+raises+when:7d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=site:vccircle.com+raises+when:3d&hl=en-IN&gl=IN&ceid=IN:en",
)
_FUNDING_PATTERN = re.compile(r"\b(raises|raised|secures|bags|nets|picks up|funding|seed round|pre-seed|series [a-e])\b", re.I)
# Not a startup round: IPOs, rights issues, acquisitions, VC funds closing, round-ups, "to raise" plans.
_FUNDING_SKIP = re.compile(r"\b(ipo|drhp|rights issue|acqui|stake|fund (i|ii|iii|iv|v)\b|first close|corpus|"
                           r"this week|funding and acquisitions|weekly|report|to raise|in talks|mutual fund|debt fund|new funds?\b|enters|"
                           r"universit|college|institute|government|ministry)", re.I)
_FUNDING_SPLIT = re.compile(r"\s+(raises|raised|secures|bags|nets|picks up)\b", re.I)
_FUNDING_ROUND_IN = re.compile(r"\b(?:round|funding|investment)\s+in\s+(.+)$", re.I)
_FUNDING_DESCRIPTOR = re.compile(
    r"^(?:exclusive:\s*)?(?:(?:gurgaon|gurugram|noida|new delhi|delhi|bengaluru|bangalore|mumbai|pune|hyderabad|"
    r"chennai)(?:-based|['’]s)?\s*)?(?:[\w&/-]+\s+){0,4}?(?:company|startup|platform|brand|firm|maker|provider|"
    r"marketplace|app|player|fintech|edtech|healthtech|saas|foodtech|agritech|d2c|deeptech|spacetech)\s+", re.I)
_FUNDING_CUTOFF_SECONDS = 45 * 86400
_FUNDING_CITY_RE = re.compile(r"\b(gurgaon|gurugram|noida|greater noida|new delhi|delhi|ghaziabad|faridabad|"
                              r"bengaluru|bangalore|mumbai|pune|hyderabad|chennai|kolkata|ahmedabad|jaipur|chandigarh)\b", re.I)
_NEWS_HOSTS = ("entrackr.com", "inc42.com", "indiatimes.com", "yourstory.com", "twitter.com", "x.com",
               "linkedin.com", "facebook.com", "instagram.com", "youtube.com", "t.me", "whatsapp.com",
               "google.com", "apple.com", "wp.com", "wordpress.com", "gravatar.com", "feedburner.com")
_CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"


def _funding_company(title):
    m = _FUNDING_ROUND_IN.search(title)
    if m:   # "Elevation Capital leads $6.7 Mn seed round in Dextr AI"
        name = m.group(1)
    else:
        m = _FUNDING_SPLIT.search(title)
        if not m:
            return ""
        name = title[:m.start()]
    name = re.sub(r"^(?:exclusive|funding|breaking)\s*:\s*", "", name.strip(), flags=re.I)
    name = re.sub(r"^.*?\b\w+['’]s\s+", "", name)   # "Cred alumni’s Sol", "Noida's Acme"
    while (shorter := _FUNDING_DESCRIPTOR.sub("", name, count=1)) != name:   # "D2C streetwear brand X"
        name = shorter
    name = name.strip(" :-’'")
    return name if 0 < len(name.split()) <= 5 and "," not in name else ""


def _funding_city(text):
    """The first Indian city named near a company, NCR spellings normalized to a priority city."""
    m = _FUNDING_CITY_RE.search(text)
    return m.group(1).title() if m else ""


def _funding_site(raw_html, company):
    """The company's own website if the article links it: an outbound link whose host shares a word
    with the company name."""
    words = [w for w in re.findall(r"[a-z0-9]+", company.lower()) if len(w) >= 3]
    for href in re.findall(r'href="(https?://[^"]+)"', raw_html or ""):
        host = urlparse(href).netloc.lower().removeprefix("www.")
        if not host or any(host == h or host.endswith("." + h) for h in _NEWS_HOSTS):
            continue
        if any(w in host.replace("-", "") for w in words):
            return f"https://{host}"
    return ""


def fetch_funding_news():
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    leads, seen = [], set()
    cutoff = time.time() - _FUNDING_CUTOFF_SECONDS
    for feed_url in _FUNDING_FEEDS:
        try:
            root = ET.fromstring(_get(feed_url).content)
        except (requests.RequestException, ET.ParseError):
            continue
        for item in root.findall(".//item"):
            title = strip_html(item.findtext("title") or "")
            if "news.google.com" in feed_url:
                title = re.sub(r"\s+-\s+[^-]+$", "", title)   # "Acme raises $5M - Inc42" -> drop the publisher
            if not title or not _FUNDING_PATTERN.search(title) or _FUNDING_SKIP.search(title):
                continue
            try:
                dt = parsedate_to_datetime(item.findtext("pubDate") or "")
            except (TypeError, ValueError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.timestamp() < cutoff:
                continue
            company = _funding_company(title)
            if not company or company.lower() in seen:
                continue
            seen.add(company.lower())
            link = (item.findtext("link") or "").strip()
            raw = (item.findtext(_CONTENT_NS) or "") + " " + (item.findtext("description") or "")
            text = strip_html(raw)
            if len(text) < 1200 and link and "news.google.com" not in link:   # a teaser only: read the article for the city and the site
                try:
                    raw = _get(link).text
                    text = strip_html(raw)
                    time.sleep(0.3)
                except requests.RequestException:
                    pass
            city = _funding_city(text[:6000])
            # Reporters' and publishers' addresses are not the company's: keep emails out of the lead.
            desc = _EMAIL_RE.sub("", f"{title}. {text[:3000]}")
            posted_at = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            leads.append(_lead(company, "Software Engineering (recently funded)", desc, link,
                               _funding_site(raw, company), f"{city}, India" if city else "India",
                               posted_at, "funding_news"))
    return leads


# ------------------------------------------------------------ ats_boards --
# Indian startups' public job-board APIs (verified 2026-10-05: each returned jobs, mostly in India). Real,
# dated openings; the heuristic then keeps the entry-level engineering ones. Add a board: slug -> website.
ATS_BOARDS = {
    "lever": {"100ms": "https://www.100ms.live", "cred": "https://cred.club", "epifi": "https://fi.money",
              "fampay": "https://famapp.in", "hevodata": "https://hevodata.com", "meesho": "https://meesho.com",
              "paytm": "https://paytm.com", "zeta": "https://zeta.tech", "mindtickle": "https://www.mindtickle.com"},
    "greenhouse": {"inmobi": "https://www.inmobi.com", "newton": "https://www.newtonschool.co",
                   "truecaller": "https://www.truecaller.com",
                   # added 2026-10-12, each verified with India openings
                   "groww": "https://groww.in", "highradius": "https://www.highradius.com",
                   "zenoti": "https://www.zenoti.com", "druva": "https://www.druva.com",
                   "hackerrank": "https://www.hackerrank.com", "observeai": "https://www.observe.ai"},
    "ashby": {"atlan": "https://atlan.com", "ditto": "https://joinditto.in", "leap": "https://leapscholar.com",
              "sarvam": "https://www.sarvam.ai", "signoz": "https://signoz.io", "spotdraft": "https://www.spotdraft.com"},
    "workable": {"leena-ai": "https://leena.ai"},
}
_ENG_TITLE = re.compile(r"engineer|developer|\bsde\b|software|backend|frontend|full[- ]?stack|\bml\b|machine learning|"
                        r"\bai\b|data scien|devops|\bsre\b|platform|programmer|technical staff", re.I)
_ATS_INDIA = re.compile(r"india|bengaluru|bangalore|gurgaon|gurugram|noida|delhi|mumbai|pune|hyderabad|chennai|remote", re.I)


def _iso_ts(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ats_jobs(kind, slug):
    """(title, location, url, posted_at, description) for every posting on one board."""
    if kind == "lever":
        return [(j["text"], (j.get("categories") or {}).get("location") or "", j.get("hostedUrl", ""),
                 _iso_ts(j["createdAt"] / 1000) if j.get("createdAt") else None, j.get("descriptionPlain") or "")
                for j in _get(f"https://api.lever.co/v0/postings/{slug}?mode=json").json()]
    if kind == "greenhouse":
        return [(j["title"], (j.get("location") or {}).get("name", ""), j.get("absolute_url", ""),
                 normalize_date(j.get("updated_at")), html.unescape(j.get("content") or ""))
                for j in _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true").json()["jobs"]]
    if kind == "ashby":
        return [(j["title"], " ".join([j.get("location") or ""] + [x.get("location", "") for x in j.get("secondaryLocations") or []]),
                 j.get("jobUrl", ""), normalize_date(j.get("publishedAt")), j.get("descriptionPlain") or "")
                for j in _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").json()["jobs"]]
    if kind == "workable":
        return [(j["title"], f"{j.get('city', '')}, {j.get('country', '')}", j.get("url", ""),
                 normalize_date(j.get("published_on")), "")
                for j in _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}").json()["jobs"]]
    return []


# Boards found on companies' own sites while researching them (research.crawl_site): {"kind:slug": {company,
# website, found}}. Committed with the rest of data/, so the list grows by itself every day.
ATS_REGISTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ats_boards.json")


def _registry():
    try:
        with open(ATS_REGISTRY) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def remember_boards(boards, company, website):
    reg = _registry()
    new = {f"{k}:{s}" for k, s in boards} - set(reg) - {f"{k}:{s}" for k, b in ATS_BOARDS.items() for s in b}
    if not new:
        return
    for key in new:
        reg[key] = {"company": company, "website": website, "found": normalize_date(int(time.time()))[:10]}
    with open(ATS_REGISTRY, "w") as f:
        json.dump(reg, f, indent=1, sort_keys=True)


def _all_boards():
    """(kind, slug, company, website): the hand-picked boards, then every discovered one."""
    for kind, boards in ATS_BOARDS.items():
        for slug, website in boards.items():
            yield kind, slug, urlparse(website).netloc.removeprefix("www.").split(".")[0].title(), website
    for key, v in _registry().items():
        kind, _, slug = key.partition(":")
        yield kind, slug, v.get("company") or slug.title(), v.get("website") or ""


def fetch_ats_boards():
    from . import ats
    leads = []
    for kind, slug, company, website in _all_boards():
        if kind == "darwinbox":   # behind a Cloudflare bot check: recorded, never readable without a browser
            continue
        try:
            jobs = _ats_jobs(kind, slug) if kind in ATS_BOARDS else ats.board_jobs(kind, slug)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            continue
        for title, loc, url, posted_at, desc in jobs:
            if _ATS_INDIA.search(loc) and _ENG_TITLE.search(title):
                leads.append(_lead(company, title, desc, url, website, loc, posted_at, "ats_boards"))
        time.sleep(0.2)
    return leads


# -------------------------------------------------------------- simplify --

_SIMPLIFY_CUTOFF_SECONDS = 30 * 86400


def _simplify_get(url):
    # these listings.json files are ~15MB; raw.githubusercontent.com
    # intermittently resets the connection mid-transfer on a large GET,
    # so give this one more attempts (with backoff) than the shared _get.
    last_exc = None
    for attempt in range(5):
        try:
            r = SESSION.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_exc = e
            time.sleep(0.5 * (attempt + 1))
    raise last_exc


def _simplify_fetch_list(primary, fallback=None):
    try:
        return _simplify_get(primary).json()
    except requests.RequestException:
        if fallback:
            return _simplify_get(fallback).json()
        raise


def _simplify_leads_from(items):
    leads = []
    cutoff = time.time() - _SIMPLIFY_CUTOFF_SECONDS
    for it in items:
        if not it.get("active"):
            continue
        if it.get("is_visible") is False:
            continue
        date_posted = it.get("date_posted")
        if not date_posted or date_posted < cutoff:
            continue

        parts = []
        if it.get("category"):
            parts.append(it["category"])
        terms = it.get("terms") or []
        if terms:
            parts.append("Terms: " + ", ".join(terms))
        if it.get("sponsorship"):
            parts.append(f"Sponsorship: {it['sponsorship']}")
        degrees = it.get("degrees") or []
        if degrees:
            parts.append("Degrees: " + ", ".join(degrees))
        description = ". ".join(parts)

        company_url = it.get("company_url") or ""
        website = company_url if company_url and "simplify.jobs" not in company_url else ""
        locations = it.get("locations") or []
        posted_at = normalize_date(date_posted)

        leads.append(_lead(
            it.get("company_name", ""), it.get("title", ""), description,
            it.get("url", ""), website, ", ".join(locations), posted_at, "simplify",
        ))
    return leads


def fetch_simplify():
    leads = []
    leads += _simplify_leads_from(_simplify_fetch_list(
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    ))
    leads += _simplify_leads_from(_simplify_fetch_list(
        "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    ))
    return leads


# ---------------------------------------- yc india / linkedin / hasjob / reddit --
# Added 2026-10-13, each live-tested: YC's own India+hiring search (51 companies), LinkedIn's guest job search
# (capped at 8 searches a run), Hasjob's feed, and hiring posts on Indian startup subreddits.

_YC_ALGOLIA_RE = re.compile(r'window\.AlgoliaOpts\s*=\s*(\{.*?\});', re.S)


def _yc_algolia_creds():
    r = _get("https://www.ycombinator.com/companies")
    m = _YC_ALGOLIA_RE.search(r.text)
    if not m:
        raise RuntimeError("could not find window.AlgoliaOpts on /companies page")
    opts = json.loads(m.group(1))
    return opts["app"], opts["key"]


def fetch_yc_india():
    app_id, api_key = _yc_algolia_creds()
    params = urllib.parse.urlencode({
        "facetFilters": json.dumps([["regions:India"], ["isHiring:true"]]),
        "hitsPerPage": 200,
    })
    body = {"requests": [{"indexName": "YCCompany_production", "params": params}]}
    r = SESSION.post(
        f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/*/queries",
        headers={
            "X-Algolia-API-Key": api_key,
            "X-Algolia-Application-Id": app_id,
            "Content-Type": "application/json",
        },
        json=body, timeout=TIMEOUT,
    )
    r.raise_for_status()
    hits = r.json()["results"][0].get("hits", [])

    leads = []
    for h in hits:
        slug = h.get("slug", "")
        batch = h.get("batch") or ""
        title = f"Engineering (YC {batch})" if batch else "Engineering (YC)"
        parts = []
        if h.get("one_liner"):
            parts.append(h["one_liner"])
        if h.get("long_description"):
            parts.append(h["long_description"])
        extra = ", ".join((h.get("tags") or []) + (h.get("industries") or []))
        if extra:
            parts.append(extra)
        desc = " ".join(parts)
        url = f"https://www.ycombinator.com/companies/{slug}" if slug else ""
        leads.append(_lead(
            h.get("name", ""), title, desc, url, h.get("website", ""),
            h.get("all_locations", ""), None, "yc_hiring", yc=True,
        ))
    return leads


# ---------------------------------------------------------------- 2. LinkedIn --
# Guest (unauthenticated) job search API: no login needed, but aggressively rate
# limited, so this stays well under budget (hard cap below) with randomized sleeps
# and a single 429 retry.

_LI_KEYWORDS = ("software engineer intern", "software developer fresher", "backend intern", "python developer intern")
_LI_LOCATIONS = ("Noida, Uttar Pradesh, India", "Gurugram, Haryana, India", "New Delhi, Delhi, India", "India")
# A bare "Delhi" matched Delhi Township, Ohio in testing: full names, and anything outside India is dropped.
_LI_INDIA = re.compile(r"india|noida|gurugram|gurgaon|delhi|bengaluru|bangalore|mumbai|pune|hyderabad|chennai|remote", re.I)
_LI_MAX_REQUESTS = 8
_LI_MAX_DETAIL_FETCHES = 15
_LI_TRACKING_PARAMS = ("position", "pageNum", "refId", "trackingId", "trk")


def _li_clean_url(url):
    if not url:
        return ""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _li_search(keywords, location):
    url = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={quote(keywords)}&location={quote(location)}&f_TPR=r604800&start=0"
    )
    try:
        r = _get(url)
    except requests.RequestException as e:
        if getattr(e, "response", None) is not None and e.response.status_code == 429:
            time.sleep(20)
            r = _get(url)
        else:
            raise
    return BeautifulSoup(r.text, "html.parser").select(".base-card")


def _li_detail_description(job_id):
    try:
        r = _get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}")
    except requests.RequestException:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    d = soup.select_one(".description__text")
    return d.decode_contents() if d else ""


def fetch_linkedin():
    leads = []
    seen_urls = set()
    requests_made = 0
    detail_fetches = 0

    combos = [(k, loc) for k in _LI_KEYWORDS for loc in _LI_LOCATIONS]
    random.shuffle(combos)

    for keywords, location in combos:
        if requests_made >= _LI_MAX_REQUESTS:
            break
        requests_made += 1
        try:
            cards = _li_search(keywords, location)
        except requests.RequestException:
            continue

        for c in cards:
            title_el = c.select_one(".base-search-card__title")
            company_el = c.select_one(".base-search-card__subtitle")
            loc_el = c.select_one(".job-search-card__location")
            link_el = c.select_one("a.base-card__full-link") or c.select_one("a")
            time_el = c.select_one("time")

            href = _li_clean_url(link_el.get("href") if link_el else "")
            loc_text = loc_el.get_text(strip=True) if loc_el else location
            if not href or href in seen_urls or not _LI_INDIA.search(loc_text) or re.search(r", (OH|US)\b|United States", loc_text):
                continue
            seen_urls.add(href)

            urn = c.get("data-entity-urn", "")
            job_id = urn.split(":")[-1] if urn else ""

            desc = ""
            if job_id and detail_fetches < _LI_MAX_DETAIL_FETCHES:
                desc = _li_detail_description(job_id)
                detail_fetches += 1
                time.sleep(random.uniform(3, 6))

            posted_at = normalize_date(time_el.get("datetime")) if time_el else None
            leads.append(_lead(
                company_el.get_text(strip=True) if company_el else "",
                title_el.get_text(strip=True) if title_el else "",
                desc, href, "", loc_el.get_text(strip=True) if loc_el else location,
                posted_at, "linkedin",
            ))

        time.sleep(random.uniform(3, 6))

    return leads


# ----------------------------------------------------------------- 3. Hasjob --

_ENG_TITLE_RE = re.compile(
    r"engineer|developer|\bsde\b|software|backend|frontend|full[- ]?stack|\bml\b|machine learning|"
    r"\bai\b|data scien|devops|\bsre\b|programmer|technical", re.I,
)
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def fetch_hasjob():
    r = _get("https://hasjob.co/feed")
    root = ET.fromstring(r.content)

    leads = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        title = (entry.findtext(f"{_ATOM_NS}title") or "").strip()
        if not _ENG_TITLE_RE.search(title):
            continue
        link_el = entry.find(f"{_ATOM_NS}link")
        url = link_el.get("href") if link_el is not None else ""
        location = entry.findtext(f"{_ATOM_NS}location") or ""
        content = entry.findtext(f"{_ATOM_NS}content") or ""
        published = entry.findtext(f"{_ATOM_NS}published") or entry.findtext(f"{_ATOM_NS}updated")
        posted_at = normalize_date(published)

        # company name: hasjob entries have no explicit <author> for the org; the
        # first bold link in the content body is the posting company's name/site.
        company = ""
        website = ""
        name_match = re.search(r'<strong>\s*<a href="([^"]+)">([^<]+)</a>', content)
        if name_match:
            website = name_match.group(1)
            company = strip_html(name_match.group(2))
        if not company:
            # fall back to the URL's org slug, e.g. hasjob.co/<org>/<id>
            path_parts = [p for p in urlparse(url).path.split("/") if p]
            if path_parts:
                company = path_parts[0]

        leads.append(_lead(company, title, content, url, website, location, posted_at, "hasjob"))
    return leads


# ------------------------------------------------------------- 4. Reddit hiring --

_REDDIT_UA = "volley/0.1 (job search script)"
_REDDIT_FEEDS = (
    ("developersIndia", "hiring"),
    ("StartUpIndia", "hiring OR intern"),
    ("indianstartups", "hiring OR intern"),
)
_REDDIT_HIRING_RE = re.compile(r"\[hiring\]|\bhiring\b|looking for.*intern|we('| a)re hiring", re.I)
_REDDIT_FOR_HIRE_RE = re.compile(
    r"\[for\s*hire\]|looking for (a |an )?(job|role|opportunit|referral)|seeking (a |an )?"
    r"(job|role|opportunit)|open to work|fresher.*(looking|seeking)|2025 grad|2026 grad|"
    r"as a .*(dev|engineer).*(looking|what)", re.I,
)

def _reddit_session():
    s = requests.Session()
    s.headers.update({"User-Agent": _REDDIT_UA})
    return s


def _reddit_get(session, url):
    last_exc = RuntimeError(f"gave up fetching {url}")
    for attempt in range(2):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code == 429:
                last_exc = requests.HTTPError("429 Too Many Requests", response=r)
                time.sleep(20)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_exc = e
    raise last_exc


def fetch_reddit_hiring():
    session = _reddit_session()
    leads = []
    seen = set()

    for sub, query in _REDDIT_FEEDS:
        url = (
            f"https://www.reddit.com/r/{sub}/search.rss"
            f"?q={quote(query)}&restrict_sr=1&sort=new&t=month"
        )
        try:
            r = _reddit_get(session, url)
            root = ET.fromstring(r.content)
        except (requests.RequestException, ET.ParseError):
            continue

        for entry in root.findall(f"{_ATOM_NS}entry"):
            title = entry.findtext(f"{_ATOM_NS}title") or ""
            if _REDDIT_FOR_HIRE_RE.search(title) or not _REDDIT_HIRING_RE.search(title) \
                    or not _ENG_TITLE_RE.search(title) or re.search(r"commission|sales|marketing|unpaid", title, re.I):
                continue

            link_el = entry.find(f"{_ATOM_NS}link")
            post_url = link_el.get("href") if link_el is not None else ""
            if not post_url or post_url in seen:
                continue
            seen.add(post_url)

            author = entry.findtext(f"{_ATOM_NS}author/{_ATOM_NS}name") or ""
            content = entry.findtext(f"{_ATOM_NS}content") or ""
            published = entry.findtext(f"{_ATOM_NS}published") or entry.findtext(f"{_ATOM_NS}updated")
            posted_at = normalize_date(published)

            text = strip_html(content)
            website = ""
            url_match = re.search(r'href="(https?://(?!www\.reddit\.com|external-preview\.redd\.it|preview\.redd\.it|i\.redd\.it)[^"]+)"', content)
            if url_match:
                website = url_match.group(1)

            company_match = re.search(r"\bat\s+([A-Z][\w.&-]*(?:\s+[A-Z][\w.&-]*){0,2})\b", title)
            company = company_match.group(1) if company_match else (author or "")

            leads.append(_lead(company, title, content, post_url, website, "India", posted_at, "reddit"))

        time.sleep(random.uniform(8, 12))

    return leads


# ------------------------------------------------------------ 5. GitHub hiring --
# GitHub search for issues/discussions naming India + hiring/intern returns no
# real signal (checked live: 0 hits for a 30-day title search), and the curated
# "Indian startups hiring" repos found via repo search are either generic
# resource lists (job-board links, not individual companies) or web-app
# projects with no parseable README table -- not real per-company postings.
# This fetcher is implemented but documented as a no-op; see report.


# ------------------------------------------------------------- portfolio --
# Startups backed by India-focused investors (Antler India, Peak XV Surge, Stellaris, LetsVenture, Titan
# Capital; see portfolio.py). Small, funded, often no posting: like funding_news they're cold-emailed with
# "do you take interns". Portfolios change slowly and ~85 detail pages are read, so this runs on Mondays only.

def fetch_portfolio():
    if datetime.now(timezone.utc).weekday() != 0 and not os.environ.get("VOLLEY_PORTFOLIO_NOW"):
        return []
    from .portfolio import fetch_portfolios
    leads = []
    for row in fetch_portfolios():
        site = row.get("website") or ""
        if not site or site in _KNOWN_URLS:
            continue
        leads.append(_lead(row["company"], "Software Engineering (no public opening)",
                           f"Backed by {row['investor']}. {row.get('description') or ''}", site, site,
                           row.get("location") or "India", None, "portfolio"))
    return leads


# ---------------------------------------------------------------- manual --
# Leads collected by hand or in a logged-in browser (LinkedIn / X hiring posts, which no keyless API serves):
# data/manual_leads.json is a list of {company, title, url, website, location, description, emails, posted_at}.
# Each entry is picked up once (its url is then a known lead).

MANUAL_LEADS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "manual_leads.json")


def fetch_manual():
    if not os.path.exists(MANUAL_LEADS):
        return []
    with open(MANUAL_LEADS) as f:
        items = json.load(f)
    leads = []
    for it in items:
        if not it.get("company") or it.get("url") in _KNOWN_URLS:
            continue
        leads.append(_lead(it["company"], it.get("title") or "Software Engineering Internship", it.get("description") or "",
                           it.get("url") or "", it.get("website") or "", it.get("location") or "India",
                           normalize_date(it.get("posted_at")), "manual",
                           extra_email_text=" ".join(it.get("emails") or [])))
    return leads


# --------------------------------------------------------------- runner --

_SOURCES = {
    "hn_hiring": fetch_hn_hiring,
    "yc_hiring": fetch_yc_hiring,
    "remoteok": fetch_remoteok,
    "remotive": fetch_remotive,
    "arbeitnow": fetch_arbeitnow,
    "jobicy": fetch_jobicy,
    "himalayas": fetch_himalayas,
    "unstop": fetch_unstop,
    "internshala": fetch_internshala,
    "github_orgs": fetch_github_orgs,
    "funding_news": fetch_funding_news,
    "simplify": fetch_simplify,
    "ats_boards": fetch_ats_boards,
    "yc_india": fetch_yc_india,
    "linkedin": fetch_linkedin,
    "hasjob": fetch_hasjob,
    "reddit": fetch_reddit_hiring,
    "manual": fetch_manual,
    "portfolio": fetch_portfolio,
}


SOURCE_DEADLINE = 8 * 60


def fetch_all(enabled: dict, log=print, priority=None, known_urls=()) -> list:
    _KNOWN_URLS.clear()
    _KNOWN_URLS.update(known_urls)
    if priority:
        _INTERNSHALA_FLOOR.update(year=priority["min_lpa"] * 100000, month=priority["min_stipend"])
    # Sources fetch in parallel (one after another took ~19 min, Internshala alone 10) on daemon threads, so a
    # source still running at the deadline is dropped for the day instead of holding the whole run.
    results, threads = {}, {}

    def one(key, fn):
        try:
            results[key] = fn()
        except Exception as e:
            results[key] = e

    for key, fn in _SOURCES.items():
        if enabled.get(key):
            threads[key] = threading.Thread(target=one, args=(key, fn), daemon=True)
            threads[key].start()
    deadline = time.monotonic() + SOURCE_DEADLINE
    all_leads = []
    for key, t in threads.items():
        t.join(max(0, deadline - time.monotonic()))
        got = results.get(key)
        if t.is_alive():
            log(f"source {key} still running after {SOURCE_DEADLINE // 60} min, skipped today")
        elif isinstance(got, Exception):
            log(f"source {key} failed: {got}")
        else:
            log(f"{key}: {len(got)} leads")
            all_leads.extend(got)
    return all_leads


if __name__ == "__main__":
    enabled = {k: True for k in _SOURCES}
    leads = fetch_all(enabled)

    print(f"\nTotal: {len(leads)} leads\n")

    by_source = {}
    for lead in leads:
        by_source.setdefault(lead["source"], []).append(lead)

    for key in _SOURCES:
        subset = by_source.get(key, [])
        print(f"{key}: {len(subset)}")
        if subset:
            sample = dict(subset[0])
            sample["description"] = sample["description"][:200]
            print(f"  sample: {sample}\n")
