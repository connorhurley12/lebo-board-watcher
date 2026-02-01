#!/usr/bin/env python3
"""
Board Watch — Meeting Analysis Engine
Reads transcripts and agendas, sends them to an LLM for a parent-friendly summary.
"""

import argparse
import logging
import os
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
DRAFTS_DIR = PROJECT_ROOT / "data" / "drafts"

# ---------------------------------------------------------------------------
# The Prompt
# ---------------------------------------------------------------------------

ANALYSIS_PROMPT = """\
You are a cynical, busy parent living in Mt. Lebanon, PA. Read this meeting transcript.
Ignore the 'Pledge of Allegiance' and 'Roll Call'.
Find the 3 items that will actually impact my life:
- Will my taxes go up?
- Is my kid's bus schedule changing?
- Are they digging up the street in front of the high school again?

Output format:

# 🚨 The 2-Minute Drill
(3 bullet points of the most urgent news)

# 🏛️ The Deep Dive
(A structured summary of the major debates, quoting specific commissioners/board members if they got heated.)
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

def build_user_prompt(transcript: dict, agendas: list[dict]) -> str:
    """Build a prompt for a single transcript, optionally paired with agendas."""
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

    parts.append("## Meeting Transcript\n")
    parts.append(f"### {transcript['filename']}\n")
    content = transcript["content"]
    # Cap at ~60k chars (~15k tokens) to stay within context limits
    if len(content) > 60_000:
        content = content[:60_000] + "\n\n[Transcript truncated for length]"
    parts.append(content)
    parts.append("\n\n")

    parts.append(ANALYSIS_PROMPT)
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
        default="openai",
        help="LLM provider (default: openai)",
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
        model = "gpt-4o" if args.provider == "openai" else "claude-sonnet-4-5-20250929"

    # Load data
    if args.file:
        p = Path(args.file)
        transcripts = [{"filename": p.name, "content": p.read_text(encoding="utf-8")}]
    else:
        transcripts = load_text_files(TRANSCRIPTS_DIR)

    agendas = load_text_files(AGENDAS_DIR)

    if not transcripts:
        log.error("No transcripts found. Run ingest_data.py first.")
        raise SystemExit(1)

    log.info("Loaded %d transcript(s) and %d agenda(s)", len(transcripts), len(agendas))
    system_prompt = load_context()

    # Process each transcript individually to stay within token limits
    for transcript in transcripts:
        log.info("Analyzing: %s", transcript["filename"])

        user_prompt = build_user_prompt(transcript, agendas)
        log.info("Sending to %s (%s)…", args.provider, model)

        try:
            analysis = _call_llm(args.provider, model, system_prompt, user_prompt)
        except Exception as exc:
            log.error("Analysis failed for %s: %s", transcript["filename"], exc)
            continue

        # Use a label derived from the transcript filename
        label = Path(transcript["filename"]).stem
        draft_path = save_draft(analysis, label=label)

        print("\n" + "=" * 80)
        print(f"  {transcript['filename']}")
        print("=" * 80)
        print(analysis)
        print(f"\nDraft saved to: {draft_path}\n")


if __name__ == "__main__":
    main()
