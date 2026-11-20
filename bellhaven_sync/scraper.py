"""Scraper: every Bellhaven community on the operator's website.

How it finds them:
  1. Walk the community directory by following its "Next" link until there is none. We never loop on
     page numbers, because the site quietly returns the last page again for out-of-range page numbers.
  2. Also collect community links from the home page. The home page features a new community
     ("New this year") that the directory does not list, and it says how many communities there are,
     which we use as a completeness check.
  3. Visit each community's own page. Only the detail page has the full street address, so it is the
     source of truth for every field.
A location missing a required field fails the run loudly instead of producing half-empty proposals.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config
from .normalize import care_type

log = logging.getLogger(__name__)

COMMUNITY_LINK = re.compile(r"^/communities/[^/?#]+$")
CITY_STATE_ZIP = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$")
COUNT_CLAIM = re.compile(r"serve\s+(\d+)\s+communities", re.I)


class ScrapeError(RuntimeError):
    pass


@dataclass
class Location:
    slug: str
    url: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: list[str]
    administrator: str = ""
    phone: str = ""
    found_on: list[str] = field(default_factory=list)  # "directory" and/or "home page"

    @property
    def care_types(self) -> list[str]:
        """The offerings translated to the CRM's vocabulary, in website order."""
        out = []
        for offering in self.care_offerings:
            mapped = care_type(offering, config.CARE_TYPE_MAP)
            if mapped and mapped not in out:
                out.append(mapped)
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["care_types"] = self.care_types
        return d


@dataclass
class ScrapeResult:
    locations: list[Location]
    pages_visited: list[str]
    claimed_count: int | None
    warnings: list[str]


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = config.USER_AGENT
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def _get(s: requests.Session, url: str) -> BeautifulSoup:
    r = s.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def community_links(soup: BeautifulSoup) -> list[str]:
    out = []
    for a in soup.select("a[href]"):
        href = a["href"].split("#")[0]
        if COMMUNITY_LINK.match(href) and href not in out:
            out.append(href)
    return out


def next_page(soup: BeautifulSoup) -> str | None:
    for a in soup.select(".pager a[href]"):
        if "next" in a.get_text(" ", strip=True).lower():
            return a["href"]
    return None


def parse_detail(html_soup: BeautifulSoup, slug: str, url: str) -> Location:
    h1 = html_soup.find("h1")
    fields: dict[str, object] = {}
    detail = html_soup.select_one("dl.detail") or html_soup
    for dt in detail.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd is None:
            continue
        label = dt.get_text(" ", strip=True).lower()
        if label == "address":
            fields["address_lines"] = [t.strip() for t in dd.stripped_strings]
        elif label.startswith("care"):
            badges = [b.get_text(" ", strip=True) for b in dd.select(".badge")]
            fields["care"] = badges or [t.strip() for t in dd.stripped_strings]
        else:
            fields[label] = dd.get_text(" ", strip=True)

    lines = fields.get("address_lines") or []
    if len(lines) < 2:
        raise ScrapeError(f"{url}: address not found or incomplete: {lines!r}")
    street, last = lines[0], lines[-1]
    m = CITY_STATE_ZIP.match(last)
    if not h1 or not m:
        raise ScrapeError(f"{url}: could not read name or 'City, ST 12345' from {last!r}")
    return Location(
        slug=slug,
        url=url,
        name=h1.get_text(" ", strip=True),
        street=street,
        city=m["city"].strip(),
        state=m["state"],
        zip=m["zip"][:5],
        care_offerings=list(fields.get("care") or []),
        administrator=str(fields.get("administrator", "")),
        phone=str(fields.get("phone", "")),
    )


def scrape(base_url: str | None = None, s: requests.Session | None = None) -> ScrapeResult:
    base = (base_url or config.SITE_URL).rstrip("/") + "/"
    s = s or session()
    found: dict[str, set[str]] = {}
    visited: list[str] = []
    warnings: list[str] = []

    # 1. The directory, following "Next" links.
    url: str | None = urljoin(base, "communities")
    while url and url not in visited:
        visited.append(url)
        soup = _get(s, url)
        for href in community_links(soup):
            found.setdefault(href, set()).add("directory")
        nxt = next_page(soup)
        url = urljoin(url, nxt) if nxt else None

    # 2. The home page, for featured communities the directory leaves out, and the stated total.
    home_url = urljoin(base, "")
    home = _get(s, home_url)
    visited.append(home_url)
    for href in community_links(home):
        found.setdefault(href, set()).add("home page")
    claim = COUNT_CLAIM.search(home.get_text(" ", strip=True))
    claimed = int(claim.group(1)) if claim else None

    # 3. Each community's own page.
    locations = []
    for href in sorted(found):
        detail_url = urljoin(base, href.lstrip("/"))
        loc = parse_detail(_get(s, detail_url), slug=href.rsplit("/", 1)[-1], url=detail_url)
        loc.found_on = sorted(found[href])
        locations.append(loc)
        if found[href] == {"home page"}:
            warnings.append(f"{loc.name} is linked from the home page but missing from the directory")

    if claimed is not None and claimed != len(locations):
        warnings.append(f"Home page says {claimed} communities; scraped {len(locations)}")
    for w in warnings:
        log.warning(w)
    return ScrapeResult(locations=locations, pages_visited=visited, claimed_count=claimed, warnings=warnings)
