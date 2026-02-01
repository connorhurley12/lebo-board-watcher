#!/usr/bin/env python3
"""
Board Watch — Data Ingestion
Scrapes YouTube meeting videos and BoardDocs agendas.
"""

import argparse
import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
import yt_dlp

load_dotenv()
from youtube_transcript_api import YouTubeTranscriptApi

# ---------------------------------------------------------------------------
# Configuration — change these for a different town
# ---------------------------------------------------------------------------

YOUTUBE_SOURCES = [
    {
        "name": "Municipality",
        "type": "channel",
        "url": "https://www.youtube.com/@mtlmeetings",
    },
    {
        "name": "SchoolBoard",
        "type": "playlist",
        "url": "https://www.youtube.com/playlist?list=PL2Lgvh7YyccoRq1ET1MVKEEXl6WgCm0y0",
    },
    {
        "name": "SchoolBoardPresentations",
        "type": "playlist",
        "url": "https://www.youtube.com/playlist?list=PL2Lgvh7YyccqB9XQ_sTpD8dThPnG_GogB",
    },
]

BOARDDOCS_URL = "https://go.boarddocs.com/pa/mtlebanon/Board.nsf/Public"
MTLEB_AGENDAS_URL = "https://mtlebanon.org/about/agendas-minutes/"
MTLEB_FINANCE_URL = "https://mtlebanon.org/departments/finance/"

DATA_DIR = Path("data")
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
AGENDAS_DIR = DATA_DIR / "agendas"
MINUTES_DIR = DATA_DIR / "minutes"
BUDGET_DIR = DATA_DIR / "budget"

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

class _QuietLogger:
    """Custom logger for yt_dlp that silences 'not available' noise."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.debug("yt_dlp: %s", msg)

    def error(self, msg):
        # Only surface errors that aren't "This video is not available"
        if "not available" not in str(msg).lower():
            log.warning("yt_dlp: %s", msg)


def fetch_video_list(source: dict, lookback_days: int) -> list[dict]:
    """Return metadata for videos published within *lookback_days*."""
    cutoff = datetime.now() - timedelta(days=lookback_days)
    cutoff_str = cutoff.strftime("%Y%m%d")
    url = source["url"]

    # Channels need /videos appended for a reliable listing
    if source["type"] == "channel" and not url.endswith("/videos"):
        url = url.rstrip("/") + "/videos"

    # Use full (non-flat) extraction so yt_dlp resolves upload_date for us.
    # The dateafter filter tells yt_dlp to stop once it hits older videos,
    # and break_on_reject avoids walking the entire back-catalog.
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,          # skip unavailable videos silently
        "noprogress": True,
        "logger": _QuietLogger(),      # suppress yt_dlp's own ERROR prints
        "playlistend": 30,             # safety cap
        "dateafter": cutoff_str,       # only videos on or after cutoff
        "break_on_reject": True,       # stop playlist once a video is too old
        "remote_components": ["ejs:github"],  # required for YouTube JS challenge solving
    }

    videos = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as exc:
            log.error("yt_dlp extraction failed for %s: %s", url, exc)
            return []

        if info is None:
            return []

        # Single video (unlikely) vs playlist
        entries = info.get("entries") or ([info] if info.get("id") else [])

        for entry in entries:
            if entry is None:
                continue

            video_id = entry.get("id")
            upload_date_str = entry.get("upload_date")

            if not video_id or not upload_date_str:
                continue

            upload_date = datetime.strptime(upload_date_str, "%Y%m%d")
            if upload_date < cutoff:
                continue

            videos.append({
                "id": video_id,
                "title": entry.get("title", "Untitled"),
                "date": upload_date.strftime("%Y-%m-%d"),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "source": source["name"],
            })

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

    ensure_dirs()

    run_youtube = not args.boarddocs_only
    run_boarddocs = not args.youtube_only

    if run_youtube:
        log.info("=== YouTube Ingestion ===")
        scrape_youtube(YOUTUBE_SOURCES, lookback_days=args.lookback_days)

    if run_boarddocs:
        log.info("=== BoardDocs Ingestion ===")
        scrape_boarddocs(BOARDDOCS_URL)

        log.info("=== Mt. Lebanon Website Ingestion ===")
        scrape_mtleb_agendas(MTLEB_AGENDAS_URL, lookback_days=args.lookback_days)

        log.info("=== Finance / Budget Ingestion ===")
        scrape_finance_budget(MTLEB_FINANCE_URL)

    log.info("Ingestion complete.")


if __name__ == "__main__":
    main()
