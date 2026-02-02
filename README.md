# Lebo Board Watch

AI-powered weekly newsletter that monitors Mt. Lebanon, PA local government meetings — school board, municipal commission, and more — so busy residents can stay informed without sitting through hours of public meetings.

## How It Works

The pipeline runs in four stages:

```
Ingest → Extract → Consolidate → Publish
```

1. **Ingest** (`scripts/ingest_data.py`) — Scrapes YouTube transcripts, BoardDocs agendas, official meeting minutes (PDF), and budget documents from municipal websites.
2. **Extract** (`scripts/analyze_meeting.py` Phase 1) — Sends each meeting transcript to an LLM (Claude or GPT-4) to extract votes, spending items, key debates, quotes, and citizen comment sentiment.
3. **Consolidate** (`scripts/analyze_meeting.py` Phase 2) — Combines all per-meeting extracts with historical data from Supabase into one cohesive weekly newsletter.
4. **Publish** (`scripts/publish_to_ghost.py`) — Posts the newsletter as a draft to Ghost CMS for review before publishing.

A GitHub Actions workflow runs the full pipeline every Friday at 9 AM EST.

## Project Structure

```
community-pulse/
├── scripts/
│   ├── ingest_data.py      # Data scraping (YouTube, BoardDocs, PDFs)
│   ├── analyze_meeting.py  # LLM extraction + newsletter generation
│   ├── publish_to_ghost.py # Ghost CMS draft publisher
│   ├── recon_township.py   # Township data source discovery tool
│   └── db.py               # Supabase persistence layer
├── project_context.md      # System prompt with Mt. Lebanon context
├── requirements.txt
├── .env.example
├── assets/
│   └── logo.png            # Newsletter feature image
├── data/
│   ├── transcripts/        # YouTube meeting transcripts
│   ├── agendas/            # BoardDocs + PDF agendas
│   ├── minutes/            # Official meeting minutes
│   ├── budget/             # Budget documents
│   ├── extracts/           # Phase 1 LLM output cache
│   ├── votes/              # Structured vote records (JSON)
│   ├── drafts/             # Generated newsletter markdown
│   └── configs/            # Township recon output
├── migrations/
│   └── 001_initial_schema.sql  # Supabase schema
└── .github/
    └── workflows/
        └── lebo_watch.yml  # Weekly automation
```

## Setup

### Prerequisites

- Python 3.12+
- An Anthropic or OpenAI API key
- (Optional) Supabase project for historical tracking
- (Optional) Ghost CMS site for publishing
- (Optional) Playwright for BoardDocs scraping

### Installation

```bash
git clone https://github.com/connorhurley/community-pulse.git
cd community-pulse
pip install -r requirements.txt

# For BoardDocs scraping
playwright install chromium
```

### Environment Variables

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes* | Claude API key |
| `OPENAI_API_KEY` | Yes* | OpenAI API key (*one of the two is required) |
| `GHOST_API_URL` | No | Ghost site URL |
| `GHOST_ADMIN_KEY` | No | Ghost Admin API key (format: `{id}:{secret}`) |
| `SUPABASE_URL` | No | Supabase project URL |
| `SUPABASE_KEY` | No | Supabase service role key |

Supabase and Ghost are optional — the pipeline works in file-only mode without them.

### Database Setup (Optional)

If using Supabase, run the migration to create tables for meetings, votes, spending, officials, and newsletters:

```sql
-- Run migrations/001_initial_schema.sql in your Supabase SQL editor
```

## Usage

### Full Pipeline

```bash
# 1. Scrape latest meeting data
python scripts/ingest_data.py

# 2. Analyze meetings and generate newsletter
python scripts/analyze_meeting.py

# 3. Publish draft to Ghost
python scripts/publish_to_ghost.py
```

### Ingestion Options

```bash
python scripts/ingest_data.py --lookback-days 14    # Scrape last 14 days (default: 7)
python scripts/ingest_data.py --youtube-only        # Only scrape YouTube transcripts
python scripts/ingest_data.py --boarddocs-only      # Only scrape BoardDocs agendas
```

### Analysis Options

```bash
python scripts/analyze_meeting.py --provider anthropic    # Use Claude (default)
python scripts/analyze_meeting.py --provider openai       # Use GPT-4
python scripts/analyze_meeting.py --model gpt-4o          # Override model
python scripts/analyze_meeting.py --file path/to/file.txt # Analyze a single file
python scripts/analyze_meeting.py --retry-failed          # Skip cached extractions, only process new/failed
python scripts/analyze_meeting.py --digest-only           # Regenerate newsletter from cache (no Phase 1 API calls)
python scripts/analyze_meeting.py --clear-cache           # Wipe extraction cache and re-extract everything
```

### Township Reconnaissance

Discover data sources for any US municipality:

```bash
python scripts/recon_township.py --town "Mt. Lebanon, PA"
python scripts/recon_township.py --town "Upper St. Clair, PA"
```

Outputs a JSON config to `data/configs/` with the municipality's website, YouTube channel, BoardDocs/eCode360 URLs, and budget page.

## Newsletter Sections

Each generated newsletter includes:

| Section | Content |
|---------|---------|
| **The Headlines** | Top 3 highest-impact stories (tax/fee changes, zoning fights, school issues) |
| **The Deep Dive** | Investigative summaries: the numbers, the hidden detail, and the next-step prediction |
| **Quote of the Week** | Most interesting quote from any meeting |
| **The Checkbook** | Top 3 largest spending items |
| **Field & Facility Watch** | Sports and parks facility updates |
| **The Smoke Detector** | Financial red flags, legal threats, quiet denials, zoning fights, split votes |
| **The Disconnect Index** | Public comment sentiment vs. board votes |
| **Save the Date** | Actionable future dates where residents need to show up or act |

## Data Sources

| Source | Method | Output |
|--------|--------|--------|
| YouTube (`@mtlmeetings`) | `yt-dlp` + transcript API | `data/transcripts/` |
| BoardDocs (School Board) | Playwright headless browser | `data/agendas/` |
| mtlebanon.org (Agendas/Minutes) | BeautifulSoup + pdfminer | `data/agendas/`, `data/minutes/` |
| mtlebanon.org (Budget) | BeautifulSoup + pdfminer | `data/budget/` |

## Architecture Notes

- **Extraction caching**: Phase 1 results are cached in `data/extracts/`. Use `--retry-failed` to skip already-processed meetings, or `--digest-only` to skip Phase 1 entirely and regenerate the newsletter from cache.
- **Historical context**: When Supabase is enabled, Phase 2 pulls repeat vendor spending, project totals, and official dissent patterns from the last year to give the LLM richer context.
- **Rate limiting**: 90-second delays between LLM calls to stay within API rate limits.
- **Graceful degradation**: Supabase and Ghost are optional. Without them, the pipeline still generates local markdown drafts.
