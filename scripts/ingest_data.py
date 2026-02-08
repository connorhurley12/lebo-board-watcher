#!/usr/bin/env python3
"""
Board Watch — Data Ingestion
Scrapes YouTube meeting videos and BoardDocs agendas.
"""

import argparse
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
import requests as _requests

load_dotenv()
from youtube_transcript_api import YouTubeTranscriptApi

# ---------------------------------------------------------------------------
# Configuration — loaded from data/configs/<town>.json
# ---------------------------------------------------------------------------

CONFIGS_DIR = Path("data/configs")
DATA_DIR = Path("data")
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
AGENDAS_DIR = DATA_DIR / "agendas"
MINUTES_DIR = DATA_DIR / "minutes"
BUDGET_DIR = DATA_DIR / "budget"


def load_config(config_path: Path) -> dict:
    """Load a town config JSON file and return parsed sources."""
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_filename(name: str, max_length: int = 120) -> str:
    """Remove characters that are unsafe in filenames."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.strip().replace(" ", "_")
    return name[:max_length]


def ensure_dirs():
    """Create output directories if they don't exist."""
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    AGENDAS_DIR.mkdir(parents=True, exist_ok=True)
    MINUTES_DIR.mkdir(parents=True, exist_ok=True)
    BUDGET_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# YouTube Scraper
# ---------------------------------------------------------------------------

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")


def _yt_api_get(endpoint: str, params: dict) -> dict | None:
    """Make a YouTube Data API v3 GET request."""
    params["key"] = YOUTUBE_API_KEY
    resp = _requests.get(f"{YOUTUBE_API_BASE}/{endpoint}", params=params, timeout=15)
    if resp.status_code != 200:
        log.error("YouTube API error (%s): %s", resp.status_code, resp.text[:300])
        return None
    return resp.json()


def _resolve_channel_uploads_playlist(channel_url: str) -> str | None:
    """Given a channel URL, return its 'uploads' playlist ID."""
    # Handle @handle-style URLs
    handle = channel_url.rstrip("/").split("/")[-1]
    if handle.startswith("@"):
        data = _yt_api_get("channels", {"forHandle": handle, "part": "contentDetails"})
    else:
        data = _yt_api_get("channels", {"id": handle, "part": "contentDetails"})

    if not data or not data.get("items"):
        log.error("Could not resolve channel: %s", channel_url)
        return None
    return data["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]


def _extract_playlist_id(url: str) -> str | None:
    """Extract playlist ID from a YouTube playlist URL."""
    match = re.search(r"list=([A-Za-z0-9_-]+)", url)
    return match.group(1) if match else None


def fetch_video_list(source: dict, lookback_days: int) -> list[dict]:
    """Return metadata for videos published within *lookback_days*."""
    if not YOUTUBE_API_KEY:
        log.error("YOUTUBE_API_KEY not set — cannot list YouTube videos")
        return []

    cutoff = datetime.now() - timedelta(days=lookback_days)

    # Resolve to a playlist ID
    if source["type"] == "channel":
        playlist_id = _resolve_channel_uploads_playlist(source["url"])
    else:
        playlist_id = _extract_playlist_id(source["url"])

    if not playlist_id:
        log.error("Could not determine playlist ID for %s", source["url"])
        return []

    # Fetch playlist items (most recent first)
    videos = []
    page_token = None

    while True:
        params = {
            "playlistId": playlist_id,
            "part": "snippet",
            "maxResults": 30,
        }
        if page_token:
            params["pageToken"] = page_token

        data = _yt_api_get("playlistItems", params)
        if not data:
            break

        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            published = snippet.get("publishedAt", "")
            video_id = snippet.get("resourceId", {}).get("videoId")

            if not video_id or not published:
                continue

            upload_date = datetime.fromisoformat(published.replace("Z", "+00:00")).replace(tzinfo=None)
            if upload_date < cutoff:
                # Playlist is newest-first, so we can stop early
                page_token = None
                break

            videos.append({
                "id": video_id,
                "title": snippet.get("title", "Untitled"),
                "date": upload_date.strftime("%Y-%m-%d"),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "source": source["name"],
            })
        else:
            page_token = data.get("nextPageToken")

        if not page_token:
            break

    return videos


def fetch_transcript(video_id: str) -> str | None:
    """Download English captions for a video. Returns None on failure."""
    try:
        api = YouTubeTranscriptApi()
        transcript = api.fetch(video_id, languages=["en"])
        return " ".join(snippet.text for snippet in transcript.snippets)
    except Exception as exc:
        log.warning("No transcript for %s: %s", video_id, exc)
        return None


def save_transcript(video: dict, text: str) -> Path:
    """Write transcript to data/transcripts/."""
    safe_title = sanitize_filename(video["title"])
    filename = f"{video['date']}_{video['source']}_{safe_title}.txt"
    filepath = TRANSCRIPTS_DIR / filename

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"Title: {video['title']}\n")
        f.write(f"Date: {video['date']}\n")
        f.write(f"URL: {video['url']}\n")
        f.write(f"Source: {video['source']}\n\n")
        f.write("=" * 80 + "\n\n")
        f.write(text)

    log.info("Saved transcript: %s", filename)
    return filepath


def scrape_youtube(sources: list[dict], lookback_days: int = 7):
    """Fetch recent videos and their transcripts from all YouTube sources."""
    for source in sources:
        log.info("Processing YouTube source: %s (%s)", source["name"], source["type"])
        videos = fetch_video_list(source, lookback_days)
        log.info("Found %d recent videos", len(videos))

        for video in videos:
            # Skip if we already have a transcript for this video
            safe_title = sanitize_filename(video["title"])
            expected_filename = f"{video['date']}_{video['source']}_{safe_title}.txt"
            if (TRANSCRIPTS_DIR / expected_filename).exists():
                log.debug("Transcript already saved: %s", expected_filename)
                continue

            transcript = fetch_transcript(video["id"])
            if transcript:
                save_transcript(video, transcript)
            else:
                log.warning("Skipping (no transcript): %s", video["title"])


# ---------------------------------------------------------------------------
# BoardDocs Scraper
# ---------------------------------------------------------------------------

async def scrape_boarddocs_async(url: str):
    """Use Playwright to grab the most recent meeting agenda from BoardDocs."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url, timeout=30_000, wait_until="networkidle")

            # BoardDocs is a single-page JS app. Click the MEETINGS tab first.
            meetings_tab = await page.query_selector("text=MEETINGS")
            if meetings_tab:
                await meetings_tab.click()
                await page.wait_for_timeout(3_000)
            else:
                log.warning("Could not find MEETINGS tab — trying page as-is")

            # Meeting entries are <a class="meeting"> inside .wrap-year
            first_meeting = await page.query_selector("a.meeting")
            if not first_meeting:
                log.error("Could not locate a meeting entry on BoardDocs")
                return

            meeting_text = (await first_meeting.inner_text()).strip()
            log.info("Opening meeting: %s", meeting_text.replace("\n", " — "))
            await first_meeting.click()
            await page.wait_for_timeout(5_000)

            # Extract meeting metadata
            meeting_name = ""
            meeting_date_text = ""
            try:
                meeting_name = await page.inner_text(".meeting-name")
                meeting_date_text = await page.inner_text(".meeting-date")
            except Exception:
                meeting_name = meeting_text

            # Extract agenda items (class "item agendaorder")
            items = await page.query_selector_all(".item.agendaorder")
            agenda_lines = []
            for item in items:
                item_text = (await item.inner_text()).strip()
                if item_text:
                    agenda_lines.append(item_text)

            if not agenda_lines:
                log.warning("No agenda items found — grabbing full page text")
                agenda_text = await page.inner_text("body")
            else:
                agenda_text = "\n\n".join(agenda_lines)

            # Parse date from the .meeting-date element (e.g. "Monday, January 26, 2026")
            date_match = re.search(
                r"(\w+),?\s+(\w+ \d{1,2},? \d{4})", meeting_date_text
            )
            if date_match:
                try:
                    date_obj = datetime.strptime(
                        date_match.group(2).replace(",", ""), "%B %d %Y"
                    )
                    date_str = date_obj.strftime("%Y-%m-%d")
                except ValueError:
                    date_str = datetime.now().strftime("%Y-%m-%d")
            else:
                date_str = datetime.now().strftime("%Y-%m-%d")

            safe_name = sanitize_filename(meeting_name)
            filename = f"{date_str}_boarddocs_{safe_name}.txt"
            filepath = AGENDAS_DIR / filename

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"Source: BoardDocs\n")
                f.write(f"Meeting: {meeting_name}\n")
                f.write(f"Date: {meeting_date_text}\n")
                f.write(f"URL: {page.url}\n\n")
                f.write("=" * 80 + "\n\n")
                f.write(agenda_text)

            log.info("Saved agenda: %s (%d items)", filename, len(agenda_lines))

        except Exception as exc:
            log.error("BoardDocs scrape failed: %s", exc)
        finally:
            await browser.close()


def scrape_boarddocs(url: str):
    """Synchronous wrapper around the async BoardDocs scraper."""
    asyncio.run(scrape_boarddocs_async(url))


# ---------------------------------------------------------------------------
# Mt. Lebanon Website Scraper (agendas & minutes PDFs)
# ---------------------------------------------------------------------------

def scrape_mtleb_agendas(url: str, lookback_days: int = 30):
    """
    Scrape the Mt. Lebanon agendas-minutes page for PDF links.

    The page is static HTML with PDF links organized by board/commission.
    Downloads agenda and minutes PDFs and saves their text content.
    Only downloads documents dated within *lookback_days* of today.
    """
    import requests as req
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    try:
        resp = req.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.error("Failed to fetch %s: %s", url, exc)
        return

    soup = BeautifulSoup(resp.text, "html.parser")

    # Find all PDF links on the page
    pdf_links = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if href.lower().endswith(".pdf"):
            # Resolve relative URLs
            if href.startswith("/"):
                href = "https://mtlebanon.org" + href
            elif not href.startswith("http"):
                href = "https://mtlebanon.org/" + href

            link_text = a_tag.get_text(strip=True)
            pdf_links.append({"url": href, "text": link_text})

    if not pdf_links:
        log.warning("No PDF links found on %s", url)
        return

    log.info("Found %d PDF links on mtlebanon.org", len(pdf_links))
    cutoff = datetime.now() - timedelta(days=lookback_days)

    for pdf in pdf_links:
        pdf_url = pdf["url"]
        pdf_stem = Path(pdf_url).stem  # e.g. "CM-01272026A" or "LIBRARY-01202026A"

        # Parse the structured filename: BOARD-MMDDYYYYX where X = A(genda) or M(inutes)
        fname_match = re.search(r"([A-Za-z_-]+?)-(\d{6,8})([AaMm]?)$", pdf_stem)
        if fname_match:
            board_name = fname_match.group(1)
            date_digits = fname_match.group(2)
            type_letter = fname_match.group(3).upper()
        else:
            board_name = pdf_stem
            date_digits = ""
            type_letter = ""

        # Determine agenda vs minutes
        if type_letter == "M":
            doc_type = "minutes"
            out_dir = MINUTES_DIR
        else:
            doc_type = "agenda"
            out_dir = AGENDAS_DIR

        # Parse date from digits (MMDDYYYY or MMDDYY)
        date_obj = None
        if len(date_digits) == 8:
            try:
                date_obj = datetime.strptime(date_digits, "%m%d%Y")
                date_str = date_obj.strftime("%Y-%m-%d")
            except ValueError:
                date_str = datetime.now().strftime("%Y-%m-%d")
        elif len(date_digits) == 6:
            try:
                date_obj = datetime.strptime(date_digits, "%m%d%y")
                date_str = date_obj.strftime("%Y-%m-%d")
            except ValueError:
                date_str = datetime.now().strftime("%Y-%m-%d")
        else:
            date_str = datetime.now().strftime("%Y-%m-%d")

        # Skip documents older than the lookback window
        if date_obj and date_obj < cutoff:
            log.debug("Skipping old document: %s (%s)", pdf_stem, date_str)
            continue

        # Download the PDF and extract text
        try:
            pdf_resp = req.get(pdf_url, headers=headers, timeout=30)
            pdf_resp.raise_for_status()
        except Exception as exc:
            log.warning("Could not download %s: %s", pdf_url, exc)
            continue

        pdf_text = _extract_pdf_text(pdf_resp.content)
        if not pdf_text:
            pdf_text = f"[PDF could not be parsed — view at {pdf_url}]"

        safe_board = sanitize_filename(board_name)
        filename = f"{date_str}_mtleb_{doc_type}_{safe_board}.txt"
        filepath = out_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"Source: mtlebanon.org\n")
            f.write(f"Board: {board_name}\n")
            f.write(f"Type: {doc_type}\n")
            f.write(f"Date: {date_str}\n")
            f.write(f"PDF URL: {pdf_url}\n\n")
            f.write("=" * 80 + "\n\n")
            f.write(pdf_text)

        log.info("Saved %s: %s", doc_type, filename)


# ---------------------------------------------------------------------------
# Finance / Budget Scraper
# ---------------------------------------------------------------------------

def scrape_finance_budget(finance_url: str):
    """
    Scrape the Mt. Lebanon finance page for budget subpages and PDFs.

    Discovers budget year pages (e.g., /finance/2026-budget/) and downloads
    both the page text (budget summaries, tables) and any linked PDFs.
    """
    import requests as req
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    try:
        resp = req.get(finance_url, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.error("Failed to fetch %s: %s", finance_url, exc)
        return

    soup = BeautifulSoup(resp.text, "html.parser")

    # Find budget year pages (e.g., /finance/2026-budget/, /finance/2026-managers-recommended-budget/)
    budget_links = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if re.search(r"/finance/\d{4}-.*budget", href, re.IGNORECASE):
            if href.startswith("/"):
                href = "https://mtlebanon.org" + href
            elif not href.startswith("http"):
                href = "https://mtlebanon.org/" + href
            link_text = a_tag.get_text(strip=True)
            budget_links.append({"url": href, "text": link_text})

    # Deduplicate by URL
    seen_urls = set()
    unique_links = []
    for link in budget_links:
        if link["url"] not in seen_urls:
            seen_urls.add(link["url"])
            unique_links.append(link)
    budget_links = unique_links

    if not budget_links:
        log.warning("No budget pages found on %s", finance_url)
        return

    log.info("Found %d budget page(s) on finance page", len(budget_links))

    for link in budget_links:
        _scrape_budget_page(link["url"], link["text"], headers)


def _scrape_budget_page(page_url: str, page_title: str, headers: dict):
    """Scrape a single budget page for its content and PDFs."""
    import requests as req
    from bs4 import BeautifulSoup

    try:
        resp = req.get(page_url, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Could not fetch budget page %s: %s", page_url, exc)
        return

    soup = BeautifulSoup(resp.text, "html.parser")

    # Extract the main content area text (budget tables, summaries)
    content_area = soup.find("main") or soup.find("article") or soup.find("div", class_="entry-content")
    if content_area:
        page_text = content_area.get_text(separator="\n", strip=True)
    else:
        page_text = soup.get_text(separator="\n", strip=True)

    # Extract year from URL (e.g., /2026-budget/ → 2026)
    year_match = re.search(r"/(\d{4})-", page_url)
    year_str = year_match.group(1) if year_match else datetime.now().strftime("%Y")

    # Save the page text as a summary
    safe_title = sanitize_filename(page_title or f"budget_{year_str}")
    summary_filename = f"{year_str}_budget_summary_{safe_title}.txt"
    summary_path = BUDGET_DIR / summary_filename

    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"Source: mtlebanon.org\n")
        f.write(f"Type: budget_summary\n")
        f.write(f"Year: {year_str}\n")
        f.write(f"Title: {page_title}\n")
        f.write(f"URL: {page_url}\n\n")
        f.write("=" * 80 + "\n\n")
        f.write(page_text)

    log.info("Saved budget summary: %s", summary_filename)

    # Find and download PDFs linked from this budget page
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if not href.lower().endswith(".pdf"):
            continue

        if href.startswith("/"):
            href = "https://mtlebanon.org" + href
        elif not href.startswith("http"):
            href = "https://mtlebanon.org/" + href

        pdf_name = Path(href).stem
        safe_pdf = sanitize_filename(pdf_name)
        pdf_filename = f"{year_str}_budget_pdf_{safe_pdf}.txt"
        pdf_path = BUDGET_DIR / pdf_filename

        # Skip if we already have this file
        if pdf_path.exists():
            log.debug("Budget PDF already saved: %s", pdf_filename)
            continue

        try:
            pdf_resp = req.get(href, headers=headers, timeout=30)
            pdf_resp.raise_for_status()
        except Exception as exc:
            log.warning("Could not download budget PDF %s: %s", href, exc)
            continue

        pdf_text = _extract_pdf_text(pdf_resp.content)
        if not pdf_text:
            pdf_text = f"[PDF could not be parsed — view at {href}]"

        link_text = a_tag.get_text(strip=True)
        with open(pdf_path, "w", encoding="utf-8") as f:
            f.write(f"Source: mtlebanon.org\n")
            f.write(f"Type: budget_document\n")
            f.write(f"Year: {year_str}\n")
            f.write(f"Title: {link_text or pdf_name}\n")
            f.write(f"PDF URL: {href}\n\n")
            f.write("=" * 80 + "\n\n")
            f.write(pdf_text)

        log.info("Saved budget PDF: %s", pdf_filename)


def _extract_pdf_text(pdf_bytes: bytes) -> str | None:
    """Try to extract text from PDF bytes. Returns None if extraction fails."""
    try:
        from io import BytesIO
        from pdfminer.high_level import extract_text

        return extract_text(BytesIO(pdf_bytes)).strip() or None
    except ImportError:
        log.warning(
            "pdfminer.six not installed — PDF text extraction unavailable. "
            "Install with: pip install pdfminer.six"
        )
        return None
    except Exception as exc:
        log.warning("PDF text extraction failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Board Watch — Data Ingestion")
    parser.add_argument(
        "--config",
        type=str,
        default="data/configs/mt-lebanon-pa.json",
        help="Path to town config JSON (default: data/configs/mt-lebanon-pa.json)",
    )
    parser.add_argument(
        "--youtube-only", action="store_true", help="Only scrape YouTube"
    )
    parser.add_argument(
        "--boarddocs-only", action="store_true", help="Only scrape BoardDocs + mtlebanon.org"
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=7,
        help="Only fetch videos from the last N days (default: 7)",
    )
    args = parser.parse_args()

    # Load town config
    config_path = Path(args.config)
    if not config_path.exists():
        log.error("Config file not found: %s", config_path)
        return
    config = load_config(config_path)
    sources = config["sources"]
    log.info("Loaded config for %s, %s", config["town_name"], config["state"])

    ensure_dirs()

    run_youtube = not args.boarddocs_only
    run_boarddocs = not args.youtube_only

    # YouTube sources from config
    youtube_sources = sources.get("municipality_youtube", [])
    if isinstance(youtube_sources, str):
        # Backward compat: single URL string → wrap as channel entry
        youtube_sources = [{"name": "Municipality", "type": "channel", "url": youtube_sources}]

    if run_youtube and youtube_sources:
        log.info("=== YouTube Ingestion ===")
        scrape_youtube(youtube_sources, lookback_days=args.lookback_days)

    if run_boarddocs:
        boarddocs_url = sources.get("school_board_url")
        agenda_url = sources.get("agenda_url")
        budget_url = sources.get("budget_url")

        if boarddocs_url:
            log.info("=== BoardDocs Ingestion ===")
            scrape_boarddocs(boarddocs_url)

        if agenda_url:
            log.info("=== Website Agenda/Minutes Ingestion ===")
            scrape_mtleb_agendas(agenda_url, lookback_days=args.lookback_days)

        if budget_url:
            log.info("=== Finance / Budget Ingestion ===")
            scrape_finance_budget(budget_url)

    log.info("Ingestion complete.")


if __name__ == "__main__":
    main()
