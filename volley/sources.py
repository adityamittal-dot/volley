"""Fetch fresh job/lead postings from free, keyless sources for cold outreach.

Each source has its own fetch_<key>() function returning a list of lead
dicts with a fixed set of keys (see _lead()). fetch_all() runs the enabled
ones, isolating failures per source so one broken API doesn't kill the rest.
"""

import html
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests

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


def _lead(company, title, raw_description, url, website, location, posted_at, source, extra_email_text=""):
    """Build a lead dict. raw_description is scanned for emails BEFORE
    html-stripping (so mailto: links inside <a href> survive), then
    stripped/truncated for the description field.
    """
    emails = extract_emails((raw_description or "") + " " + (extra_email_text or ""))
    return {
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
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        v = str(value).strip()
        if not v:
            return None
        if v.isdigit():
            return datetime.fromtimestamp(int(v), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        if _UNSTOP_DATE_RE.match(v):
            dt = datetime.strptime(v, "%Y-%m-%d %H:%M:%S GMT%z")
            return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
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

def fetch_yc_hiring():
    r = _get("https://yc-oss.github.io/api/companies/hiring.json")
    companies = r.json()

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
        regions = c.get("regions") or []
        location = ", ".join(regions)

        leads.append(_lead(c.get("name", ""), title, description, url, website, location, None, "yc_hiring"))
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
    "ai engineer", "sde", "node",
)


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

                leads.append(_lead(
                    company, it.get("title", ""), it.get("details", ""),
                    post_url, "", location, posted_at, "unstop",
                ))
            time.sleep(0.3)

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
    "simplify": fetch_simplify,
}


def fetch_all(enabled: dict, log=print) -> list:
    all_leads = []
    for key, fn in _SOURCES.items():
        if not enabled.get(key):
            continue
        try:
            leads = fn()
        except Exception as e:
            log(f"source {key} failed: {e}")
            continue
        log(f"{key}: {len(leads)} leads")
        all_leads.extend(leads)
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
