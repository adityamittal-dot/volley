"""
portfolio.py - Scrape public VC / accelerator portfolio pages (plain HTTP,
no browser, no login) for Indian startups.

fetch_portfolios() -> list[dict] with keys:
    company, website, investor, description, location  (location "" if unknown)

Sources implemented (all reachable with plain requests + bs4, no JS execution
required to get the data we need):

  - antler        https://www.antler.co/portfolio            (Webflow, paginated,
                   global fund -> filtered to India-located companies only)
  - surge          https://surge.peakxv.com/companies         (Webflow; the full
                   name list has no links, but the "Surge cohort" cards link to
                   /companies/<slug> detail pages that carry the website)
  - stellaris      https://www.stellarisvp.com/portfolio       (Webflow; cards link
                   to /portfolio/<slug> detail pages that carry the website)
  - letsventure    https://letsventure.com/portfolio -> redirects to
                   https://lvxventures.com/portfolio           (Webflow; website is
                   in a parallel hidden "modal" collection list, same order as the
                   visible cards - zipped by index, no extra requests needed)
  - titancapital   https://www.titancapital.vc/portfolio       (Next.js app router;
                   data lives in `self.__next_f.push(...)` RSC chunks, not
                   __NEXT_DATA__. Only a small "featured" subset (~13 companies) is
                   server-rendered with real /portfolio/<slug> detail pages; the
                   "200+ companies" logo wall advertised in the page's own meta
                   description loads via client-side fetch and is NOT present in
                   the raw HTML, so it cannot be scraped without a browser. This
                   source is therefore necessarily partial.)

Sources tried and SKIPPED (need JS rendering / no reliable data in plain HTML):
  - 100x.vc/portfolio, blume.vc/portfolio, indianangelnetwork.com/portfolio,
    better.capital/portfolio, peakxv.com/companies/, venturehighway.vc/portfolio
      -> 404 / unreachable at those guessed paths.
  - elevationcapital.com/portfolio  -> 200, but it is a pure Next.js SPA with
    zero `self.__next_f.push` RSC payload for the portfolio grid (loads via a
    client-side fetch/GraphQL call after mount). No data in plain HTML.
  - ipv.vc/portfolio -> loads a static page whose *only* content is a
    `window.onload` JS redirect to /lander (a holding page). No usable data
    without executing JS.
  - accel.com/companies -> 200 (huge, 2.5MB) but no India geo-tag is present in
    the static HTML to let us keep only Indian companies out of Accel's global
    portfolio, and no SSR data markers (__NEXT_DATA__/__next_f) were found, so
    the list is almost certainly client-fetched.
  - letsventure.com/portfolio directly 0-byte responds; only works through the
    lvxventures.com redirect target (handled above).
"""

import json
import re
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
TIMEOUT = 20
SLEEP_BETWEEN_DETAIL_FETCHES = 0.3
MAX_DETAIL_PAGES = 60


def _get(url, **kwargs):
    kwargs.setdefault("headers", HEADERS)
    kwargs.setdefault("timeout", TIMEOUT)
    resp = requests.get(url, **kwargs)
    resp.raise_for_status()
    return resp


def _row(company, website, investor, description, location=""):
    return {
        "company": (company or "").strip(),
        "website": (website or "").strip(),
        "investor": investor,
        "description": (description or "").strip(),
        "location": (location or "").strip(),
    }


# --------------------------------------------------------------------------
# Antler (global fund, portfolio paginated via Webflow/Finsweet; we keep only
# companies whose location tag says India)
# --------------------------------------------------------------------------
def parse_antler():
    base = "https://www.antler.co/portfolio"
    page_param = None
    rows = []
    page = 1
    seen_names = set()
    while page <= 40:  # safety cap; real site runs out around page ~25
        url = base if page == 1 else f"{base}?{page_param}_page={page}"
        try:
            resp = _get(url)
        except requests.RequestException:
            break
        soup = BeautifulSoup(resp.text, "html.parser")

        if page == 1:
            m = re.search(r"\?([a-f0-9]+)_page=\d+", resp.text)
            if m:
                page_param = m.group(1)

        cards = soup.select(".portco_card")
        if not cards:
            break

        new_in_page = 0
        for card in cards:
            name_el = card.select_one('[fs-cmsfilter-field="name"]')
            if not name_el:
                continue
            name = name_el.get_text(strip=True)
            if name in seen_names:
                continue
            seen_names.add(name)
            new_in_page += 1

            desc_el = card.select_one('[fs-cmsfilter-field="description"]')
            tags = [t.get_text(strip=True) for t in card.select(".tag_small_text")]
            location = tags[0] if tags else ""
            link_el = card.select_one("a.clickable_link")
            website = link_el["href"] if link_el and link_el.has_attr("href") else ""

            if location.lower() != "india":
                continue

            rows.append(
                _row(
                    name,
                    website,
                    "Antler",
                    desc_el.get_text(strip=True) if desc_el else "",
                    location,
                )
            )

        if new_in_page == 0 or not page_param:
            break
        page += 1

    return rows


# --------------------------------------------------------------------------
# Surge (Peak XV Partners) - the full name list has no links; only the
# "Surge cohort" cards on the companies page link to /companies/<slug>
# detail pages, which carry the real external website + tagline.
# --------------------------------------------------------------------------
def parse_surge():
    listing_url = "https://surge.peakxv.com/companies"
    resp = _get(listing_url)
    soup = BeautifulSoup(resp.text, "html.parser")

    blocks = soup.select(".com-header_block_item")
    detail_paths = []
    seen = set()
    for b in blocks:
        a = b.select_one("a.com-header_block-logo-wrapper")
        if not a or not a.has_attr("href"):
            continue
        href = a["href"]
        if href in seen:
            continue
        seen.add(href)
        detail_paths.append(href)

    rows = []
    for i, path in enumerate(detail_paths[:MAX_DETAIL_PAGES]):
        detail_url = urllib.parse.urljoin(listing_url, path)
        try:
            dresp = _get(detail_url)
        except requests.RequestException:
            continue
        dsoup = BeautifulSoup(dresp.text, "html.parser")

        h1 = dsoup.find("h1")
        name = h1.get_text(strip=True) if h1 else path.rsplit("/", 1)[-1]

        desc = ""
        if h1 and h1.parent:
            sib = h1.find_next_sibling("div")
            if sib:
                desc = sib.get_text(strip=True)

        link = dsoup.select_one("a.client-info_link")
        website = link["href"] if link and link.has_attr("href") else ""

        rows.append(_row(name, website, "Surge (Peak XV Partners)", desc, ""))

        if i < len(detail_paths) - 1:
            time.sleep(SLEEP_BETWEEN_DETAIL_FETCHES)

    return rows


# --------------------------------------------------------------------------
# Stellaris Venture Partners - cards on the single portfolio page already
# carry name/description/sector; website requires a detail-page fetch.
# --------------------------------------------------------------------------
def parse_stellaris():
    listing_url = "https://www.stellarisvp.com/portfolio"
    resp = _get(listing_url)
    soup = BeautifulSoup(resp.text, "html.parser")

    cards = soup.select(".portfolio_company-wrap")
    entries = []
    for card in cards:
        name_el = card.select_one('[fs-cmsfilter-field="name"]')
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        desc_el = card.select_one(".portfolio_company-details > div.text-weight-light")
        if not desc_el:
            desc_el = card.select_one(".portfolio_company-details div")
        description = desc_el.get_text(strip=True) if desc_el else ""
        link_el = card.select_one('a[href^="/portfolio/"]')
        detail_path = link_el["href"] if link_el and link_el.has_attr("href") else None
        entries.append((name, description, detail_path))

    rows = []
    fetched = 0
    for name, description, detail_path in entries:
        website = ""
        if detail_path and fetched < MAX_DETAIL_PAGES:
            detail_url = urllib.parse.urljoin(listing_url, detail_path)
            try:
                dresp = _get(detail_url)
                dsoup = BeautifulSoup(dresp.text, "html.parser")
                link = dsoup.select_one("a.company_website-link")
                website = link["href"] if link and link.has_attr("href") else ""
            except requests.RequestException:
                pass
            fetched += 1
            time.sleep(SLEEP_BETWEEN_DETAIL_FETCHES)

        rows.append(_row(name, website, "Stellaris Venture Partners", description, ""))

    return rows


# --------------------------------------------------------------------------
# LetsVenture (lvx Ventures) - the visible cards and a parallel hidden
# "modal" collection list are rendered in the same order from the same CMS
# collection, so we zip them by index instead of following any links.
# --------------------------------------------------------------------------
def parse_letsventure():
    listing_url = "https://letsventure.com/portfolio"  # redirects to lvxventures.com
    resp = _get(listing_url)
    soup = BeautifulSoup(resp.text, "html.parser")

    items = soup.select(".portfolio-item")
    modals = soup.select(".modal-content")

    rows = []
    for item, modal in zip(items, modals):
        name_el = item.select_one("h4")
        name = name_el.get_text(strip=True) if name_el else ""

        link = modal.select_one("a.view-website")
        website = link["href"] if link and link.has_attr("href") else ""

        desc_el = modal.select_one(".modal-paragraph")
        description = desc_el.get_text(strip=True) if desc_el else ""

        if not name:
            continue
        rows.append(_row(name, website, "LetsVenture", description, ""))

    return rows


# --------------------------------------------------------------------------
# Titan Capital - Next.js app-router page. The company grid itself is
# client-fetched (not in the raw HTML), but a small "featured" subset is
# server-rendered with real /portfolio/<slug> detail-page links; those
# detail pages embed the external website in an RSC JSON blob and the
# company's tagline in the <meta name="description"> tag.
# --------------------------------------------------------------------------
_SOCIAL_DOMAINS = ("linkedin.com", "twitter.com", "x.com", "youtube.com", "facebook.com", "instagram.com")
_TITLE_RE = re.compile(r"<title>([^<]+?)\s*-\s*Titan Capital</title>")


def parse_titancapital():
    listing_url = "https://www.titancapital.vc/portfolio"
    resp = _get(listing_url)
    soup = BeautifulSoup(resp.text, "html.parser")

    seen = set()
    detail_paths = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/portfolio/") and href != "/portfolio" and href not in seen:
            seen.add(href)
            detail_paths.append(href)

    rows = []
    for i, path in enumerate(detail_paths[:MAX_DETAIL_PAGES]):
        detail_url = urllib.parse.urljoin(listing_url, path)
        try:
            dresp = _get(detail_url)
        except requests.RequestException:
            continue
        html = dresp.text
        dsoup = BeautifulSoup(html, "html.parser")

        title_m = _TITLE_RE.search(html)
        name = title_m.group(1).strip() if title_m else path.rsplit("/", 1)[-1]

        website = ""
        for link in dsoup.find_all("a", target="_blank", rel="noopener noreferrer", href=True):
            href = link["href"]
            if href.startswith("http") and not any(d in href for d in _SOCIAL_DOMAINS):
                website = href
                break

        meta_desc = dsoup.find("meta", attrs={"name": "description"})
        description = meta_desc["content"] if meta_desc and meta_desc.has_attr("content") else ""

        rows.append(_row(name, website, "Titan Capital", description, ""))

        if i < len(detail_paths) - 1:
            time.sleep(SLEEP_BETWEEN_DETAIL_FETCHES)

    return rows


SOURCES = {
    "antler": parse_antler,
    "surge": parse_surge,
    "stellaris": parse_stellaris,
    "letsventure": parse_letsventure,
    "titancapital": parse_titancapital,
}


def fetch_portfolios():
    """Run every registered source parser and return the combined list of rows.

    A failure in one source (network error, markup change, etc.) is caught
    and logged to stdout; it does not abort the other sources.
    """
    all_rows = []
    for source_name, parser in SOURCES.items():
        try:
            rows = parser()
        except Exception as exc:  # noqa: BLE001 - keep going on any source error
            print(f"[{source_name}] FAILED: {exc}")
            continue
        print(f"[{source_name}] {len(rows)} rows")
        all_rows.extend(rows)
    return all_rows


if __name__ == "__main__":
    per_source_rows = {}
    for source_name, parser in SOURCES.items():
        try:
            rows = parser()
        except Exception as exc:  # noqa: BLE001
            print(f"=== {source_name}: FAILED ({exc}) ===")
            continue
        per_source_rows[source_name] = rows
        print(f"=== {source_name}: {len(rows)} rows ===")
        for row in rows[:3]:
            print(" ", json.dumps(row, ensure_ascii=False))
        print()

    total = sum(len(v) for v in per_source_rows.values())
    print(f"TOTAL rows across all sources: {total}")
