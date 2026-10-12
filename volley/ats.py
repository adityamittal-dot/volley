"""Extra ATS board readers for volley's sources.py: detection of ATS links embedded in
arbitrary text/HTML, plus fetchers for a few India-popular HR platforms (Keka, Darwinbox,
Zoho Recruit, Freshteam) alongside the existing Lever/Greenhouse/Ashby/Workable readers.

Only stdlib (re, json, html, time, urllib) + requests + bs4 are used, matching volley's
existing sources.py conventions. Every fetcher returns the same tuple shape used there:

    (title, location, url, posted_at_iso_or_None, description)

Research notes (verified live 2026-10-13):

- keka: every tenant's public, no-login job feed is
  ``https://<slug>.keka.com/careers/api/jobs/default/active`` (JSON array). Job detail
  pages are ``https://<slug>.keka.com/careers/jobdetails/<id>``. Verified against
  inc42.keka.com (28 jobs) and clrfacility.keka.com (18 jobs).

- zoho recruit: the public careers page ``https://<slug>.zohorecruit.<tld>/jobs/Careers``
  is server-rendered and embeds the full job list as a JSON blob inside a
  ``<input type="hidden" value="[...]">`` tag (HTML-entity escaped). No separate API call
  needed. Verified against thehireboost.zohorecruit.com (33 jobs) and
  rhs.zohorecruit.com (311 jobs).

- freshteam: the public ``https://<slug>.freshteam.com/jobs`` listing page is also fully
  server-rendered (``a.heading`` cards with ``data-portal-location`` + ``.job-title``).
  Per-job description requires a second GET of the job detail page
  (``.job-details-content``). Verified against haptik.freshteam.com (4 jobs) and
  spotdraft.freshteam.com (3 jobs).

- darwinbox: NOT implemented. Every tenant's public candidate site
  (``<slug>.darwinbox.in/ms/candidate/careers``) is an Angular SPA whose data API
  (``/ms/candidateapi/...``) sits behind a Cloudflare bot-challenge ("Attention
  Required!") that blocks plain HTTP clients platform-wide (tested against cars24,
  bigbasket and urbancompany tenants, all blocked identically). There is no
  no-login endpoint reachable with requests/bs4, so `board_jobs("darwinbox", ...)`
  always returns `[]`. Detection of darwinbox links is still implemented, in case a
  future caller wants to flag/queue them for a browser-based fetch.
"""

import html
import json
import re
import time

import requests
from bs4 import BeautifulSoup

TIMEOUT = 20

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
})


def _get(url, **kwargs):
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


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(s):
    if not s:
        return ""
    s = html.unescape(s)
    s = _TAG_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


# -------------------------------------------------------------- detection --
# Generic slugs that show up in embed/widget/doc URLs rather than a real tenant.
_GENERIC_SLUGS = {"embed", "embedjobs", "api", "v1", "v2", "jobs", "js", "widget",
                   "www", "app", "static", "cdn", "default", "careers", "ms",
                   "candidateportal", "candidate", "login", "signup"}

# Each pattern's first captured group is the tenant slug.
ATS_LINK = {
    "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9][A-Za-z0-9-]*)", re.I),
    "greenhouse": re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9][A-Za-z0-9-]*)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9][A-Za-z0-9-]*)", re.I),
    "workable": re.compile(r"apply\.workable\.com/([A-Za-z0-9][A-Za-z0-9-]*)", re.I),
    "keka": re.compile(r"([A-Za-z0-9][A-Za-z0-9-]*)\.keka\.com/careers", re.I),
    "darwinbox": re.compile(r"([A-Za-z0-9][A-Za-z0-9-]*)\.darwinbox\.in/ms/candidate/careers", re.I),
    "zoho": re.compile(r"([A-Za-z0-9][A-Za-z0-9-]*)\.zohorecruit\.(?:com|in)/jobs/Careers", re.I),
    "freshteam": re.compile(r"([A-Za-z0-9][A-Za-z0-9-]*)\.freshteam\.com/jobs", re.I),
}


def detect_boards(urls_and_html):
    """Scan arbitrary text/HTML (a page's raw source, a blob of hrefs, etc.) for links
    to any recognised ATS board. Returns a set of (kind, slug) pairs, skipping generic
    non-tenant slugs like "embed"/"api"/"jobs".
    """
    found = set()
    if not urls_and_html:
        return found
    for kind, pattern in ATS_LINK.items():
        for m in pattern.finditer(urls_and_html):
            slug = m.group(1).lower()
            if slug in _GENERIC_SLUGS:
                continue
            found.add((kind, slug))
    return found


# ----------------------------------------------------------------- dates --

def _date_only_to_iso(d):
    """'2026-09-29' -> '2026-09-29T00:00:00Z'. None/empty -> None."""
    if not d:
        return None
    d = d.strip()
    if not d:
        return None
    if "T" in d:
        return d if d.endswith("Z") else d.replace("+00:00", "Z")
    return f"{d}T00:00:00Z"


def _passthrough_iso(d):
    """Keka's publishedOn is already ISO-8601 with milliseconds + Z, e.g.
    '2026-10-12T06:51:08.077Z'. Normalize to whole-second precision; pass through
    unparseable/empty values as None.
    """
    if not d:
        return None
    d = d.strip()
    if not d:
        return None
    # trim sub-second precision: "...08.077Z" -> "...08Z"
    return re.sub(r"\.\d+Z$", "Z", d)


# ------------------------------------------------------------------ keka --

def _keka_jobs(slug):
    url = f"https://{slug}.keka.com/careers/api/jobs/default/active"
    data = _get(url).json()
    out = []
    for j in data:
        locs = j.get("jobLocations") or []
        location = ", ".join(l.get("name", "") for l in locs if l.get("name")) or ", ".join(
            filter(None, [j.get("city"), j.get("country")])
        )
        job_url = f"https://{slug}.keka.com/careers/jobdetails/{j['id']}"
        posted_at = _passthrough_iso(j.get("publishedOn"))
        description = strip_html(j.get("description") or j.get("excerpt") or "")
        out.append((j.get("title", ""), location, job_url, posted_at, description))
    return out


# --------------------------------------------------------------- darwinbox --
# See module docstring: every tenant's candidate API sits behind a Cloudflare
# bot-challenge that blocks plain requests/bs4 clients. No working endpoint found.

def _darwinbox_jobs(slug):
    return []


# ------------------------------------------------------------------ zoho --

_ZOHO_JOBS_ARRAY_RE = re.compile(r'value="(\[.*?\])"')
_ZOHO_TLDS = ("com", "in")


def _zoho_extract_jobs_array(page_html):
    for m in _ZOHO_JOBS_ARRAY_RE.finditer(page_html):
        raw = html.unescape(m.group(1))
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, list) and data and isinstance(data[0], dict) and "Posting_Title" in data[0]:
            return data
    return []


def _zoho_jobs(slug):
    page_html = None
    base = None
    for tld in _ZOHO_TLDS:
        candidate_base = f"https://{slug}.zohorecruit.{tld}"
        try:
            r = _get(f"{candidate_base}/jobs/Careers")
        except requests.RequestException:
            continue
        if len(r.text) > 5000:   # the "no such tenant" fallback page is a couple KB
            page_html, base = r.text, candidate_base
            break
    if not page_html:
        return []

    out = []
    for j in _zoho_extract_jobs_array(page_html):
        location = ", ".join(filter(None, [j.get("City"), j.get("State"), j.get("Country")]))
        if not location and j.get("Remote_Job"):
            location = "Remote"
        job_url = f"{base}/jobs/Careers/{j['id']}"
        posted_at = _date_only_to_iso(j.get("Date_Opened"))
        description = strip_html(j.get("Job_Description") or "")
        out.append((j.get("Posting_Title", ""), location, job_url, posted_at, description))
    return out


# -------------------------------------------------------------- freshteam --

def _freshteam_jobs(slug):
    base = f"https://{slug}.freshteam.com"
    r = _get(f"{base}/jobs")
    soup = BeautifulSoup(r.text, "html.parser")
    cards = soup.select("a.heading[href]")

    out = []
    for a in cards:
        href = a.get("href", "")
        if not href:
            continue
        job_url = href if href.startswith("http") else base + href
        title_el = a.select_one(".job-title")
        title = title_el.get_text(strip=True) if title_el else a.get("data-portal-title", "")
        location = a.get("data-portal-location", "")
        if not location:
            loc_el = a.select_one(".location-info")
            location = loc_el.get_text(" ", strip=True) if loc_el else ""

        description = ""
        try:
            d = _get(job_url)
            dsoup = BeautifulSoup(d.text, "html.parser")
            content = dsoup.select_one(".job-details-content")
            description = strip_html(str(content)) if content else ""
        except requests.RequestException:
            pass
        time.sleep(0.2)

        out.append((title, location, job_url, None, description))
    return out


# --------------------------------------------------------------- dispatch --

_BOARD_FETCHERS = {
    "keka": _keka_jobs,
    "darwinbox": _darwinbox_jobs,
    "zoho": _zoho_jobs,
    "freshteam": _freshteam_jobs,
}


def board_jobs(kind, slug):
    """(title, location, url, posted_at_iso_or_None, description) for every posting on
    one board, for the new kinds this module adds: keka, darwinbox, zoho, freshteam.
    darwinbox always returns [] -- see module docstring.
    """
    fn = _BOARD_FETCHERS.get(kind)
    if not fn:
        return []
    return fn(slug)


# -------------------------------------------------------------------- __main__ --

if __name__ == "__main__":
    sample_html = """
    <a href="https://jobs.lever.co/cred/abc123">Cred</a>
    <a href="https://jobs.lever.co/embed/widget">skip me</a>
    <a href="https://boards.greenhouse.io/groww/jobs/123">Groww</a>
    <a href="https://job-boards.greenhouse.io/highradius">HighRadius</a>
    <a href="https://jobs.ashbyhq.com/atlan">Atlan</a>
    <a href="https://apply.workable.com/leena-ai/j/XYZ">Leena AI</a>
    <a href="https://inc42.keka.com/careers">Inc42</a>
    <a href="https://clrfacility.keka.com/careers/jobdetails/163198">CLR</a>
    <a href="https://cars24.darwinbox.in/ms/candidate/careers">Cars24</a>
    <a href="https://thehireboost.zohorecruit.com/jobs/Careers">HireBoost</a>
    <a href="https://rhs.zohorecruit.com/jobs/Careers/466622000032251525">RHS</a>
    <a href="https://haptik.freshteam.com/jobs">Haptik</a>
    <a href="https://api.freshteam.com/jobs">skip me too (generic slug? no -- "api" is generic)</a>
    """

    boards = detect_boards(sample_html)
    print(f"detect_boards found {len(boards)} boards:")
    for kind, slug in sorted(boards):
        print(f"  {kind}: {slug}")

    print()
    test_slugs = {
        "keka": ["inc42", "clrfacility"],
        "zoho": ["thehireboost", "rhs"],
        "freshteam": ["haptik", "spotdraft"],
        "darwinbox": ["cars24", "bigbasket"],
    }
    for kind, slugs in test_slugs.items():
        for slug in slugs:
            try:
                jobs = board_jobs(kind, slug)
                print(f"{kind}/{slug}: {len(jobs)} jobs")
                if jobs:
                    title, location, url, posted_at, desc = jobs[0]
                    print(f"  sample: title={title!r} location={location!r} posted_at={posted_at!r}")
                    print(f"          url={url!r}")
                    print(f"          description[:120]={desc[:120]!r}")
            except Exception as e:
                print(f"{kind}/{slug}: ERROR {e}")
