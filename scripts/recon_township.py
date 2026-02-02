#!/usr/bin/env python3
"""
Community Pulse — Township Reconnaissance
Discovers municipal data sources for any US township.

Usage:
    python recon_township.py --town "Mt. Lebanon, PA"
"""

import argparse
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIGS_DIR = Path("data") / "configs"
SEARCH_DELAY = 2.0  # seconds between DDG queries
HTTP_TIMEOUT = 15  # seconds

SOCIAL_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com",
    "linkedin.com", "nextdoor.com", "tiktok.com", "pinterest.com",
    "reddit.com",
}

SEARCH_ENGINE_DOMAINS = {
    "google.com", "bing.com", "duckduckgo.com", "yahoo.com",
    "wikipedia.org",
}

VENDOR_SIGNATURES = {
    "boarddocs": ["go.boarddocs.com", "boarddocs.com"],
    "ecode360": ["ecode360.com"],
    "civicplus": ["civicplus.com", "/AgendaCenter/"],
    "municode": ["library.municode.com", "municode.com"],
    "granicus": ["granicus.com", "/legistar"],
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_town(town_str: str) -> tuple[str, str]:
    """Parse 'Town Name, ST' into (town_name, state_abbrev).

    Raises ValueError if the format is invalid.
    """
    if "," not in town_str:
        raise ValueError(
            f"Invalid town format: '{town_str}'. Expected 'Town Name, ST' (e.g., 'Mt. Lebanon, PA')."
        )
    parts = [p.strip() for p in town_str.rsplit(",", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"Invalid town format: '{town_str}'. Expected 'Town Name, ST'."
        )
    return parts[0], parts[1].upper()


def make_slug(town_name: str, state: str) -> str:
    """Create a filesystem-safe slug from town name and state."""
    raw = f"{town_name} {state}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug


def _get_domain(url: str) -> str:
    """Extract the registered domain from a URL (e.g., 'mtlebanon.org')."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    # Strip 'www.' prefix
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


def _is_social_or_search(domain: str) -> bool:
    """Return True if the domain is a social media or search engine site."""
    for blocked in SOCIAL_DOMAINS | SEARCH_ENGINE_DOMAINS:
        if domain == blocked or domain.endswith("." + blocked):
            return True
    return False


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def search_ddg(query: str, max_results: int = 10) -> list[dict]:
    """Run a DuckDuckGo search. Returns list of result dicts with title/href/body."""
    try:
        from ddgs import DDGS
        results = list(DDGS().text(query, max_results=max_results))
        log.info("DDG search '%s' returned %d results", query, len(results))
        return results
    except Exception as exc:
        log.warning("DDG search failed for '%s': %s", query, exc)
        return []


def run_searches(town: str) -> dict[str, list[dict]]:
    """Execute all recon searches for the given town string.

    Returns a dict mapping category label to search results.
    """
    queries = {
        "official_site": f"{town} official website",
        "agendas": f"{town} board meetings agenda",
        "school_board": f"{town} school board boarddocs",
        "municipal_code": f"{town} ecode360",
        "youtube": f"{town} youtube channel meetings",
    }

    results: dict[str, list[dict]] = {}
    for i, (label, query) in enumerate(queries.items()):
        if i > 0:
            time.sleep(SEARCH_DELAY)
        results[label] = search_ddg(query)

    return results


# ---------------------------------------------------------------------------
# Domain identification
# ---------------------------------------------------------------------------


def identify_primary_domain(results: list[dict], town_name: str = "") -> str | None:
    """Determine the municipality's primary website domain.

    Prefers domains containing the town name, then by TLD (.gov > .org > .us).
    Filters out social media and search engine domains.
    """
    candidates: list[str] = []
    for r in results:
        url = r.get("href", "")
        domain = _get_domain(url)
        if domain and not _is_social_or_search(domain) and domain not in candidates:
            candidates.append(domain)

    if not candidates:
        return None

    # Build keywords from town name for relevance matching
    town_keywords = set()
    if town_name:
        # "Mt. Lebanon" -> {"mt", "lebanon"} ; "Upper St. Clair" -> {"upper", "st", "clair"}
        for word in re.split(r"[\s.]+", town_name.lower()):
            cleaned = re.sub(r"[^a-z]", "", word)
            if cleaned and len(cleaned) > 2:
                town_keywords.add(cleaned)

    def _domain_score(d: str) -> tuple[int, int]:
        """Lower score = better. (relevance_score, tld_score)."""
        d_lower = d.lower().replace("-", "").replace(".", "")
        # Check if domain contains town keywords
        keyword_hits = sum(1 for kw in town_keywords if kw in d_lower)
        relevance = -keyword_hits  # More hits = lower (better) score

        # TLD preference
        if d.endswith(".gov"):
            tld = 0
        elif d.endswith(".org"):
            tld = 1
        elif d.endswith(".us"):
            tld = 2
        else:
            tld = 3

        return (relevance, tld)

    candidates.sort(key=_domain_score)
    primary = candidates[0]
    log.info("Primary domain identified: %s", primary)
    return primary


# ---------------------------------------------------------------------------
# Technology fingerprinting
# ---------------------------------------------------------------------------


def fingerprint_url(url: str, session: requests.Session) -> tuple[str | None, str]:
    """Check a URL for known vendor technology signatures.

    First checks the URL string itself. If no match, fetches the page and
    checks the response URL (after redirects) and page content.

    Returns (vendor_name, resolved_url). vendor_name is None if unrecognized.
    """
    # Pass 1: Check URL string
    for vendor, patterns in VENDOR_SIGNATURES.items():
        for pattern in patterns:
            if pattern in url:
                log.info("Fingerprinted %s as %s (URL match)", url[:80], vendor)
                return vendor, url

    # Pass 2: Fetch and check response URL + structured content (links, iframes)
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        final_url = resp.url

        # Check redirected URL
        for vendor, patterns in VENDOR_SIGNATURES.items():
            for pattern in patterns:
                if pattern in final_url:
                    log.info("Fingerprinted %s as %s (redirect match)", url[:80], vendor)
                    return vendor, final_url

        # Check links and iframes for vendor signatures (not raw page text,
        # which causes false positives on unrelated pages)
        soup = BeautifulSoup(resp.text, "html.parser")
        link_urls: list[str] = []
        for tag in soup.find_all(["a", "iframe", "script"], href=True):
            link_urls.append(tag.get("href", ""))
        for tag in soup.find_all(["iframe", "script"], src=True):
            link_urls.append(tag.get("src", ""))

        for link in link_urls:
            for vendor, patterns in VENDOR_SIGNATURES.items():
                for pattern in patterns:
                    if pattern in link:
                        log.info("Fingerprinted %s as %s (link match)", url[:80], vendor)
                        return vendor, final_url

    except requests.RequestException as exc:
        log.warning("Failed to fetch %s for fingerprinting: %s", url[:80], exc)

    return None, url


def find_vendor_sources(
    search_results: dict[str, list[dict]],
    session: requests.Session,
) -> dict:
    """Identify technology vendors from search results.

    Returns a dict with provider/URL pairs for school board, municipal code,
    and agenda systems.
    """
    sources: dict = {
        "school_board_provider": None,
        "school_board_url": None,
        "municipal_code_provider": None,
        "municipal_code_url": None,
        "agenda_provider": None,
        "agenda_url": None,
    }

    # Map search categories to config keys
    category_map = {
        "school_board": ("school_board_provider", "school_board_url"),
        "municipal_code": ("municipal_code_provider", "municipal_code_url"),
        "agendas": ("agenda_provider", "agenda_url"),
    }

    for category, (provider_key, url_key) in category_map.items():
        results = search_results.get(category, [])

        # First pass: check for direct URL pattern matches (cheap, no HTTP)
        for r in results[:10]:
            url = r.get("href", "")
            if not url or _is_social_or_search(_get_domain(url)):
                continue
            for vendor, patterns in VENDOR_SIGNATURES.items():
                for pattern in patterns:
                    if pattern in url:
                        sources[provider_key] = vendor
                        sources[url_key] = url
                        log.info(
                            "%s source identified as: %s (%s)",
                            category, vendor, url[:80],
                        )
                        break
                if sources[provider_key]:
                    break
            if sources[provider_key]:
                break

        # Second pass: fetch and fingerprint if no direct match found
        if not sources[provider_key]:
            for r in results[:3]:  # Only fetch top 3 to limit requests
                url = r.get("href", "")
                if not url or _is_social_or_search(_get_domain(url)):
                    continue
                vendor, resolved_url = fingerprint_url(url, session)
                if vendor:
                    sources[provider_key] = vendor
                    sources[url_key] = resolved_url
                    log.info(
                        "%s source identified as: %s (%s)",
                        category, vendor, resolved_url[:80],
                    )
                    break

    return sources


# ---------------------------------------------------------------------------
# YouTube discovery
# ---------------------------------------------------------------------------


def find_youtube_channel(results: list[dict]) -> str | None:
    """Extract the municipality's YouTube channel URL from search results."""
    for r in results:
        url = r.get("href", "")
        parsed = urlparse(url)
        if parsed.hostname and "youtube.com" in parsed.hostname:
            path = parsed.path
            # Match /@handle or /channel/ID or /c/name
            if path.startswith("/@") or path.startswith("/channel/") or path.startswith("/c/"):
                log.info("YouTube channel found: %s", url)
                return url

    # Fallback: any youtube.com URL from results
    for r in results:
        url = r.get("href", "")
        if "youtube.com" in url:
            log.info("YouTube URL found (non-channel): %s", url)
            return url

    return None


# ---------------------------------------------------------------------------
# Budget / Finance finder
# ---------------------------------------------------------------------------


def find_budget_url(primary_domain: str, session: requests.Session) -> str | None:
    """Find the budget/finance page on the municipality's primary website.

    Scans homepage links for "budget" or "finance" text, then tries common
    fallback paths.
    """
    base_url = f"https://{primary_domain}"

    # Scan homepage for budget/finance links
    try:
        resp = session.get(base_url, timeout=HTTP_TIMEOUT)
        soup = BeautifulSoup(resp.text, "html.parser")

        for a_tag in soup.find_all("a", href=True):
            text = (a_tag.get_text() or "").strip().lower()
            href = a_tag["href"]
            if any(kw in text for kw in ("budget", "finance")):
                # Resolve relative URLs
                if href.startswith("/"):
                    href = base_url + href
                elif not href.startswith("http"):
                    href = base_url + "/" + href
                log.info("Budget/finance link found: %s", href)
                return href
    except requests.RequestException as exc:
        log.warning("Failed to scan %s for budget links: %s", base_url, exc)

    # Fallback: try common paths
    fallback_paths = ["/finance/", "/departments/finance/", "/budget/"]
    for path in fallback_paths:
        url = base_url + path
        try:
            resp = session.head(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
            if resp.status_code == 200:
                log.info("Budget page found via fallback: %s", url)
                return url
        except requests.RequestException:
            continue

    return None


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------


def build_config(
    town_name: str,
    state: str,
    primary_domain: str | None,
    vendor_sources: dict,
    youtube_url: str | None,
    budget_url: str | None,
) -> dict:
    """Assemble the standardized township config dict."""
    return {
        "town_name": town_name,
        "state": state,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sources": {
            "municipality_website": f"https://{primary_domain}" if primary_domain else None,
            "municipality_youtube": youtube_url,
            "school_board_provider": vendor_sources.get("school_board_provider"),
            "school_board_url": vendor_sources.get("school_board_url"),
            "municipal_code_provider": vendor_sources.get("municipal_code_provider"),
            "municipal_code_url": vendor_sources.get("municipal_code_url"),
            "agenda_provider": vendor_sources.get("agenda_provider"),
            "agenda_url": vendor_sources.get("agenda_url"),
            "budget_url": budget_url,
        },
    }


def save_config(config: dict, slug: str, output_path: str | None = None) -> Path:
    """Write the config to data/configs/{slug}.json and return the path."""
    if output_path:
        filepath = Path(output_path)
        filepath.parent.mkdir(parents=True, exist_ok=True)
    else:
        CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
        filepath = CONFIGS_DIR / f"{slug}.json"

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    log.info("Config saved to: %s", filepath)
    return filepath


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Community Pulse — Township Reconnaissance. "
                    "Discover municipal data sources for any US township."
    )
    parser.add_argument(
        "--town",
        required=True,
        help='Town name and state, e.g. "Mt. Lebanon, PA"',
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Override output file path (default: data/configs/{slug}.json)",
    )
    args = parser.parse_args()

    # Parse town
    try:
        town_name, state = parse_town(args.town)
    except ValueError as exc:
        log.error(str(exc))
        raise SystemExit(1)

    slug = make_slug(town_name, state)
    log.info("Recon target: %s, %s (slug: %s)", town_name, state, slug)

    # Create session with browser-like User-Agent
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    # Step 1: Run searches
    log.info("Running web searches...")
    search_results = run_searches(args.town)

    # Step 2: Identify primary domain
    primary_domain = identify_primary_domain(
        search_results.get("official_site", []), town_name=town_name
    )
    if not primary_domain:
        log.warning("Could not identify primary municipal domain")

    # Step 3: Fingerprint vendor sources
    log.info("Fingerprinting technology vendors...")
    vendor_sources = find_vendor_sources(search_results, session)

    # Step 4: Find YouTube channel
    youtube_url = find_youtube_channel(search_results.get("youtube", []))

    # Step 5: Find budget/finance page
    budget_url = None
    if primary_domain:
        log.info("Searching for budget/finance page...")
        budget_url = find_budget_url(primary_domain, session)

    # Step 6: Build and save config
    config = build_config(
        town_name=town_name,
        state=state,
        primary_domain=primary_domain,
        vendor_sources=vendor_sources,
        youtube_url=youtube_url,
        budget_url=budget_url,
    )

    filepath = save_config(config, slug, output_path=args.output)

    # Print results
    print("\n" + "=" * 60)
    print(f"  Township Recon: {town_name}, {state}")
    print("=" * 60)

    sources = config["sources"]
    print(f"\n  Primary Website:     {sources['municipality_website'] or 'Not found'}")
    print(f"  YouTube Channel:     {sources['municipality_youtube'] or 'Not found'}")
    print(f"  School Board:        {sources['school_board_provider'] or 'Not found'}"
          + (f" — {sources['school_board_url']}" if sources['school_board_url'] else ""))
    print(f"  Municipal Code:      {sources['municipal_code_provider'] or 'Not found'}"
          + (f" — {sources['municipal_code_url']}" if sources['municipal_code_url'] else ""))
    print(f"  Agenda System:       {sources['agenda_provider'] or 'Not found'}"
          + (f" — {sources['agenda_url']}" if sources['agenda_url'] else ""))
    print(f"  Budget/Finance Page: {sources['budget_url'] or 'Not found'}")

    print(f"\n  Config saved to: {filepath}")
    print("=" * 60 + "\n")

    # Also dump full JSON for piping
    print(json.dumps(config, indent=2))


if __name__ == "__main__":
    main()
