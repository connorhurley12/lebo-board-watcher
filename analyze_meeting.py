#!/usr/bin/env python3
"""
Board Watch — Meeting Analysis Engine
Reads transcripts and agendas, sends them to an LLM for a parent-friendly summary.
"""

import argparse
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent
CONTEXT_FILE = PROJECT_ROOT / "project_context.md"
TRANSCRIPTS_DIR = PROJECT_ROOT / "data" / "transcripts"
AGENDAS_DIR = PROJECT_ROOT / "data" / "agendas"
MINUTES_DIR = PROJECT_ROOT / "data" / "minutes"
BUDGET_DIR = PROJECT_ROOT / "data" / "budget"
DRAFTS_DIR = PROJECT_ROOT / "data" / "drafts"

# ---------------------------------------------------------------------------
# The Prompt
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

# Phase 1: Extract key facts from a single meeting transcript.
EXTRACT_PROMPT = """\
You are a researcher preparing notes for a newsletter about Mt. Lebanon, PA local government.

Extract ALL noteworthy items from this meeting transcript. For each item, include:
- The meeting name/body (e.g., "Commission Meeting," "School Board Meeting")
- The topic and what happened (decisions, debates, votes, dollar amounts)
- Exact vote tallies if any (e.g., "5-0" or "4-1, Smith opposed")
- Any dollar amounts mentioned (from consent agenda, bill list, contracts)
- Any mentions of sports/parks facilities (turf, fields, ice rink, pool, coaching appointments)
- Any notable quotes (include speaker name and role)
- Any upcoming dates mentioned (hearings, deadlines, events)
- Any signs of controversy (split votes, defensive responses, heated public comment)

Use the term "Commissioners" for the Muni meeting and "Directors" for the School Board.

Be thorough and factual. Do not editorialize. Output as a structured list.
"""

# Phase 2: Combine per-meeting extracts into one consolidated newsletter.
NEWSLETTER_PROMPT = """\
You are the author of 'Lebo Board Watch,' a weekly newsletter for busy residents of Mt. Lebanon, PA.
Your goal is to save parents time by extracting the high-impact signal from the noise of local government.

Below are your research notes from ALL meetings that happened this week. Your job is to combine them into ONE cohesive newsletter that covers the most important items across all meetings.

**Tone Guidelines:**
1.  **No "Minutes":** Do not say "The board discussed..." or "Mr. Smith stated..." Instead, say "The Commission is considering..."
2.  **No Negative Reporting:** NEVER list what *didn't* happen. If they didn't talk about taxes, do not mention taxes. Only report on what was actually discussed.
3.  **"So What?" Factor:** For every topic, you must explain *why* a resident should care. (e.g., "This means parking on Washington Rd will be harder next month.")
4.  **Local Context:** Use the term "Commissioners" for the Muni meeting and "Directors" for the School Board.
5.  **Prioritize Impact:** Prioritize items where money is spent or local laws are changed over "Resolutions of Support" for state/federal issues. A symbolic letter to Harrisburg matters less than a new stop sign.
6.  **Quote Context:** When quoting someone, always include their role (e.g., "Student Liaison," "Commissioner," "resident"). This adds community feel.
7.  **Cross-Meeting:** When the same topic comes up in multiple meetings, consolidate it into one item rather than repeating it.

**Analysis Lenses:**
- **When discussing "Studies" or "Plans" (like Active Transportation or Hidden Hollow):** Don't just name the plan. Tell me the *physical* change I will see. Will there be new bike lanes? Will trees be cut down?
- **When discussing Resident Comments (like the Leaf Blower guy):** Treat this as a "Signal." Is this a lone wolf, or is the Board receptive? (e.g., "Did the Commissioners ask follow-up questions, or did they just say 'Thank you'?").
- **When discussing Zoning:** Always mention the specific street names involved (e.g., "Washington Rd," "Beverly Rd").

**IMPORTANT — No Duplicate Topics:**
Each item should appear in ONE section only. If a topic fits multiple sections, use these rules:
- A controversial spending item goes in **Smoke Detector**, NOT The Checkbook.
- A parks/facility topic that is also a Deep Dive-worthy debate goes in **Deep Dive**, NOT Field & Facility Watch.
- An upcoming date already discussed in Deep Dive should still get a one-line entry in **Save the Date** (dates are a quick-reference list, not analysis).

**Structure:**
Use the following Markdown structure exactly:

# 🚨 The Headlines
(Give me 3 punchy, click-baity bullets previewing the biggest topics from across ALL meetings this week. These are teasers only — do not repeat the full analysis here. E.g., "🍃 Gas Leaf Blower Ban Proposed?")

# 🏛️ The Deep Dive
(Pick the top 2-3 topics from ANY meeting this week that involve real debate, conflict, or significant decisions. For each one, write a short paragraph explaining the issue, the conflict/debate, and the outcome. Mention which meeting body it came from. This is for topics with substance — not routine approvals or one-line mentions.)

# 🗣️ Quote of the Week
(Find the most interesting, passionate, or funny quote from any meeting this week. Always include the speaker's role and which meeting it came from. If none exists, omit this section.)

# 💸 The Checkbook
(From across all meetings, list the top 3 largest *routine* dollar amounts — contracts, vendor payments, capital purchases. Format each as: "**$Amount:** [Item Description] (Who gets the money)". Do NOT include spending items that are controversial — those belong in the Smoke Detector. If no dollar amounts were mentioned, omit this section.)

# 🏟️ Field & Facility Watch
(Quick-hit updates on sports and parks facilities from any meeting. Scan for: Turf, Grass, Permits, Lights, Ice Rink, Pool, Courts, Wildcat Fields, Middle Field, Main Park, Bird Park, Robb Hollow, Hidden Hollow, or Coaching Appointments. Summarize each in 1 sentence. Do NOT include items already covered in The Deep Dive. If none of these topics came up, omit this section.)

# 🕵️‍♂️ The Smoke Detector
(Find the "Hidden Heat" from any meeting this week. Look for these patterns:

1. **Split Vote Alert:** Any vote that is NOT unanimous (e.g., 4-1 or 3-2). Identify EXACTLY who voted "No" and summarize their reason.

2. **Zoning & Development Watch:** Keywords like "setback variance," "multi-family," "ADU," "density," "shadow study," "traffic generation," or "character of the neighborhood." Flag any project that puts something "big" next to something "small." Always include street names.

3. **Culture War Scanner (Schools):** Mentions of "Library books," "DEI," "Opt-out policies," "Bathroom policies," or "flags." Report neutrally but prominently, even if brief.

4. **Spending Watch:** Large payments ($20k+) to Consultants, Strategists, or Design Firms for *reports* rather than *construction*. Highlight cost vs. deliverable.

5. **Public Friction:** In Public Comment, look for: "time is up" / "please wrap up," applause, or a Board member responding defensively. Summarize the conflict.

If none of these patterns appear, omit this section.)

# 📅 Save the Date
(A quick-reference list of future dates mentioned across ALL meetings: public hearings, no-school days, special voting meetings, community events. Format: "**Feb 18:** Parks Advisory Board Meeting (Vote on Tree Plan)." OK to include dates from topics covered above — this is a calendar, not analysis. If no upcoming dates were mentioned, omit this section.)
"""


# ---------------------------------------------------------------------------
# File Loading
# ---------------------------------------------------------------------------

def load_context() -> str:
    """Read the project_context.md system prompt."""
    if not CONTEXT_FILE.exists():
        log.warning("Context file not found at %s — proceeding without it", CONTEXT_FILE)
        return ""
    return CONTEXT_FILE.read_text(encoding="utf-8")


def load_text_files(directory: Path) -> list[dict]:
    """Load all .txt files from a directory."""
    files = []
    if not directory.exists():
        return files
    for fp in sorted(directory.glob("*.txt")):
        files.append({
            "filename": fp.name,
            "content": fp.read_text(encoding="utf-8"),
        })
    return files


# ---------------------------------------------------------------------------
# LLM Calls
# ---------------------------------------------------------------------------

def analyze_with_openai(system_prompt: str, user_prompt: str, model: str = "gpt-4o") -> str:
    """Call the OpenAI Chat Completions API."""
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=4000,
        temperature=0.7,
    )
    return response.choices[0].message.content


def analyze_with_anthropic(system_prompt: str, user_prompt: str, model: str = "claude-sonnet-4-5-20250929") -> str:
    """Call the Anthropic Messages API."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        temperature=0.7,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def build_extract_prompt(transcript: dict, agendas: list[dict], minutes: list[dict] | None = None) -> str:
    """Build a Phase 1 extraction prompt for a single transcript or minutes document."""
    parts: list[str] = []

    if agendas:
        parts.append("## Relevant Agendas\n")
        for a in agendas:
            parts.append(f"### {a['filename']}\n")
            content = a["content"]
            if len(content) > 15_000:
                content = content[:15_000] + "\n\n[Agenda truncated]"
            parts.append(content)
            parts.append("\n\n")

    if minutes:
        parts.append("## Relevant Minutes\n")
        for m in minutes:
            parts.append(f"### {m['filename']}\n")
            content = m["content"]
            if len(content) > 15_000:
                content = content[:15_000] + "\n\n[Minutes truncated]"
            parts.append(content)
            parts.append("\n\n")

    parts.append("## Meeting Transcript\n")
    parts.append(f"### {transcript['filename']}\n")
    content = transcript["content"]
    # Cap at ~60k chars (~15k tokens) to stay within context limits
    if len(content) > 60_000:
        content = content[:60_000] + "\n\n[Transcript truncated for length]"
    parts.append(content)
    parts.append("\n\n")

    parts.append(EXTRACT_PROMPT)
    return "".join(parts)


def build_minutes_extract_prompt(minutes_doc: dict, agendas: list[dict]) -> str:
    """Build a Phase 1 extraction prompt for meeting minutes (no transcript available)."""
    parts: list[str] = []

    if agendas:
        parts.append("## Relevant Agendas\n")
        for a in agendas:
            parts.append(f"### {a['filename']}\n")
            content = a["content"]
            if len(content) > 15_000:
                content = content[:15_000] + "\n\n[Agenda truncated]"
            parts.append(content)
            parts.append("\n\n")

    parts.append("## Meeting Minutes\n")
    parts.append(f"### {minutes_doc['filename']}\n")
    content = minutes_doc["content"]
    if len(content) > 60_000:
        content = content[:60_000] + "\n\n[Minutes truncated for length]"
    parts.append(content)
    parts.append("\n\n")

    parts.append(EXTRACT_PROMPT)
    return "".join(parts)


def build_newsletter_prompt(meeting_extracts: list[dict], budget_docs: list[dict] | None = None) -> str:
    """Build a Phase 2 prompt combining all per-meeting extracts into one newsletter."""
    parts: list[str] = []

    if budget_docs:
        parts.append("## Municipal Budget Context\n")
        parts.append("(Use this as background when discussing spending, contracts, or financial items.)\n\n")
        for doc in budget_docs:
            parts.append(f"### {doc['filename']}\n")
            content = doc["content"]
            if len(content) > 10_000:
                content = content[:10_000] + "\n\n[Budget document truncated]"
            parts.append(content)
            parts.append("\n\n")
        parts.append("---\n\n")

    parts.append("## This Week's Meeting Notes\n\n")
    for extract in meeting_extracts:
        parts.append(f"### {extract['source']}\n")
        parts.append(extract["notes"])
        parts.append("\n\n---\n\n")

    parts.append(NEWSLETTER_PROMPT)
    return "".join(parts)


def save_draft(analysis: str, label: str = "") -> Path:
    """Write the analysis to data/drafts/."""
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    suffix = f"_{label}" if label else ""
    filepath = DRAFTS_DIR / f"analysis_{timestamp}{suffix}.md"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"<!-- Generated: {datetime.now().isoformat()} -->\n\n")
        f.write(analysis)

    log.info("Saved draft: %s", filepath.name)
    return filepath


def _call_llm(provider: str, model: str, system_prompt: str, user_prompt: str) -> str:
    """Route to the correct LLM provider."""
    if provider == "openai":
        return analyze_with_openai(system_prompt, user_prompt, model=model)
    return analyze_with_anthropic(system_prompt, user_prompt, model=model)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Board Watch — Meeting Analyzer")
    parser.add_argument(
        "--provider",
        choices=["openai", "anthropic"],
        default="anthropic",
        help="LLM provider (default: anthropic)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the default model for the chosen provider",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Analyze a specific transcript file instead of all",
    )
    args = parser.parse_args()

    model = args.model
    if not model:
        model = "claude-sonnet-4-5-20250929" if args.provider == "anthropic" else "gpt-4o"

    # Load data
    if args.file:
        p = Path(args.file)
        transcripts = [{"filename": p.name, "content": p.read_text(encoding="utf-8")}]
    else:
        transcripts = load_text_files(TRANSCRIPTS_DIR)

    agendas = load_text_files(AGENDAS_DIR)
    minutes = load_text_files(MINUTES_DIR)
    budget_docs = load_text_files(BUDGET_DIR)

    if not transcripts and not minutes:
        log.error("No transcripts or minutes found. Run ingest_data.py first.")
        raise SystemExit(1)

    log.info(
        "Loaded %d transcript(s), %d agenda(s), %d minutes file(s), %d budget doc(s)",
        len(transcripts), len(agendas), len(minutes), len(budget_docs),
    )
    system_prompt = load_context()

    # -----------------------------------------------------------------------
    # Phase 1: Extract key facts from each transcript individually.
    # This keeps each LLM call within token limits.
    # -----------------------------------------------------------------------
    meeting_extracts: list[dict] = []
    call_count = 0

    for transcript in transcripts:
        if call_count > 0:
            log.info("Waiting 90s for rate limit cooldown…")
            time.sleep(90)

        log.info("Phase 1 — Extracting: %s", transcript["filename"])
        user_prompt = build_extract_prompt(transcript, agendas, minutes)
        log.info("Sending to %s (%s)…", args.provider, model)

        try:
            notes = _call_llm(args.provider, model, system_prompt, user_prompt)
        except Exception as exc:
            log.error("Extraction failed for %s: %s", transcript["filename"], exc)
            continue

        meeting_extracts.append({
            "source": transcript["filename"],
            "notes": notes,
        })
        call_count += 1
        log.info("Extracted notes from %s (%d chars)", transcript["filename"], len(notes))

    # -----------------------------------------------------------------------
    # Phase 1b: Process minutes as standalone sources for meetings
    # that have no corresponding transcript (e.g., Hospital Authority).
    # -----------------------------------------------------------------------
    transcript_names = {t["filename"].lower() for t in transcripts}
    for minutes_doc in minutes:
        # Skip minutes if we already have a transcript from the same board/date.
        # Filenames look like: 2026-01-27_mtleb_minutes_CM.txt vs
        #                      2026-01-27_Municipality_Commission_Meeting.txt
        # We do a simple date-prefix check to avoid double-processing.
        date_prefix = minutes_doc["filename"][:10]  # "YYYY-MM-DD"
        already_covered = any(t.startswith(date_prefix) for t in transcript_names)
        if already_covered:
            log.info("Skipping minutes %s — transcript exists for same date", minutes_doc["filename"])
            continue

        if call_count > 0:
            log.info("Waiting 90s for rate limit cooldown…")
            time.sleep(90)

        log.info("Phase 1b — Extracting from minutes: %s", minutes_doc["filename"])
        user_prompt = build_minutes_extract_prompt(minutes_doc, agendas)
        log.info("Sending to %s (%s)…", args.provider, model)

        try:
            notes = _call_llm(args.provider, model, system_prompt, user_prompt)
        except Exception as exc:
            log.error("Extraction failed for %s: %s", minutes_doc["filename"], exc)
            continue

        meeting_extracts.append({
            "source": minutes_doc["filename"],
            "notes": notes,
        })
        call_count += 1
        log.info("Extracted notes from minutes %s (%d chars)", minutes_doc["filename"], len(notes))

    if not meeting_extracts:
        log.error("No meeting extracts produced. Cannot generate newsletter.")
        raise SystemExit(1)

    # -----------------------------------------------------------------------
    # Phase 2: Combine all extracts into one consolidated weekly newsletter.
    # -----------------------------------------------------------------------
    if len(meeting_extracts) > 1:
        log.info("Waiting 90s for rate limit cooldown…")
        time.sleep(90)

    log.info("Phase 2 — Generating consolidated newsletter from %d meetings…", len(meeting_extracts))
    newsletter_prompt = build_newsletter_prompt(meeting_extracts, budget_docs=budget_docs)
    log.info("Sending to %s (%s)…", args.provider, model)

    try:
        newsletter = _call_llm(args.provider, model, system_prompt, newsletter_prompt)
    except Exception as exc:
        log.error("Newsletter generation failed: %s", exc)
        raise SystemExit(1)

    draft_path = save_draft(newsletter, label="weekly_digest")

    print("\n" + "=" * 80)
    print("  Lebo Board Watch — Weekly Digest")
    print("=" * 80)
    print(newsletter)
    print(f"\nDraft saved to: {draft_path}\n")


if __name__ == "__main__":
    main()
