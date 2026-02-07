#!/usr/bin/env python3
"""
Board Watch — Meeting Analysis Engine
Reads transcripts and agendas, sends them to an LLM for a parent-friendly summary.
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

import db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONTEXT_FILE = PROJECT_ROOT / "project_context.md"
TRANSCRIPTS_DIR = PROJECT_ROOT / "data" / "transcripts"
AGENDAS_DIR = PROJECT_ROOT / "data" / "agendas"
MINUTES_DIR = PROJECT_ROOT / "data" / "minutes"
BUDGET_DIR = PROJECT_ROOT / "data" / "budget"
VOTES_DIR = PROJECT_ROOT / "data" / "votes"
DRAFTS_DIR = PROJECT_ROOT / "data" / "drafts"
EXTRACT_CACHE_DIR = PROJECT_ROOT / "data" / "extracts"

# Seconds to wait between LLM calls to avoid rate limits.
# Anthropic Sonnet has higher throughput; OpenAI GPT-4 needs more spacing.
RATE_LIMIT_DELAY = {"anthropic": 30, "openai": 60}
# Longer delay before Phase 2 — the consolidated prompt is large and the
# token bucket needs time to refill after many Phase 1 calls.
PHASE2_DELAY = {"anthropic": 120, "openai": 90}

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
- **Citizen Comment Tally:** For each topic raised during public/citizen comment, count how many speakers spoke FOR vs. AGAINST. Note the topic and the sentiment breakdown (e.g., "Leaf blowers: 8 against the ban, 1 for the ban").

Use the term "Commissioners" for the Muni meeting and "Directors" for the School Board.

Be thorough and factual. Do not editorialize. Output as a structured list.

## Vote Log

After your structured list, output a VOTE LOG section. For EVERY formal vote taken during the meeting \
(motions, ordinances, resolutions, appointments, consent agendas), output one JSON object per line inside \
a fenced code block tagged `vote-log`. Include unanimous and split votes alike.

Format:
```vote-log
{"meeting": "Commission Meeting", "motion": "Approve minutes of Jan 13 meeting", "result": "Passed 5-0", "unanimous": true, "yes": [], "no": [], "abstain": [], "context": ""}
{"meeting": "Commission Meeting", "motion": "Ordinance 715 - Leaf blower restrictions", "result": "Passed 4-1", "unanimous": false, "yes": ["Jones", "Lee", "Garcia", "Patel"], "no": ["Smith"], "abstain": [], "context": "Smith cited enforcement cost concerns"}
```

Rules for the vote log:
- For unanimous votes, leave "yes"/"no"/"abstain" as empty lists (individual names are not needed).
- For split votes, you MUST list every name in the correct column.
- For abstentions or recusals, put the name in "abstain" and note the reason in "context".
- "context" should be a brief explanation only for split votes or abstentions (empty string otherwise).
- If no formal votes occurred, output an empty code block: ```vote-log\n```

## Spending Log

After your vote log, output a SPENDING LOG section. For EVERY appropriation, contract award, \
purchase, change order, bill list approval, or significant expenditure mentioned in the meeting, \
output one JSON object per line inside a fenced code block tagged `spending-log`.

Format:
```spending-log
{"vendor": "Insight Pipe Contracting LLC", "amount": 1124196.00, "description": "2026 sanitary and storm sewer lining project", "category": "contract", "project": "2026 Sewer Lining", "budget_line": "Sanitary and Storm Sewer Funds", "contract_term": "base_year"}
{"vendor": "Robinson Pipe Cleaning Company", "amount": 400150.00, "description": "Annual storm and sanitary sewer cleaning and televising - renewal", "category": "contract", "project": "Sewer Cleaning Program", "budget_line": "Sanitary and Storm Water Funds", "contract_term": "renewal_1"}
{"vendor": "N/A", "amount": 7160832.12, "description": "November expenditure list approval", "category": "routine", "project": null, "budget_line": null, "contract_term": null}
```

Rules for the spending log:
- "amount" must be a number (no dollar signs, no commas). Use 0.00 if amount is unclear.
- "category" must be one of: "contract", "change_order", "consultant", "capital", "routine".
  - "contract" = new contract award or contract renewal.
  - "change_order" = modification to an existing contract increasing scope or cost.
  - "consultant" = payment to a consulting, design, or strategy firm for a study/report.
  - "capital" = equipment purchase, vehicle purchase, or infrastructure investment.
  - "routine" = expenditure list approval, bill list, or recurring operational payment.
- "project" = the named project if one exists (e.g., "2026 Sewer Lining", "Rockwood Park Playground"). Use null if no project name.
- "budget_line" = the fund or budget line mentioned (e.g., "Sanitary Sewer Fund", "2026 Operating Budget"). Use null if not stated.
- "contract_term" = "base_year", "renewal_1", "renewal_2", "renewal_3" if this is a multi-year contract. Use null otherwise.
- If no spending items are mentioned, output an empty code block: ```spending-log\n```
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
- **When mentioning specific parcels or lesser-known locations (like "Hidden Hollow," "Robb Hollow," etc.):** Always include a brief geographic context so every reader can place it (e.g., "Hidden Hollow, the wooded area bordering the golf course" or "Robb Hollow, the park off Cochran Rd"). Not every resident knows parcel names.
- **When discussing Resident Comments (like the Leaf Blower guy):** Treat this as a "Signal." Is this a lone wolf, or is the Board receptive? (e.g., "Did the Commissioners ask follow-up questions, or did they just say 'Thank you'?").
- **When discussing Zoning:** Always mention the specific street names involved (e.g., "Washington Rd," "Beverly Rd").

**IMPORTANT — No Duplicate Topics (STRICT):**
Each topic must appear in ONE analytical section only. This is a hard rule — violations ruin the reader experience.

**The Deep Dive is the "first pick."** Any topic covered in The Deep Dive MUST NOT reappear in The Smoke Detector, The Checkbook, or Field & Facility Watch. If a zoning controversy is in The Deep Dive, do NOT also put it in "Zoning & Development Watch." If a spending item is in The Deep Dive, do NOT also put it in "Spending Watch."

**Priority rules when a topic fits multiple sections:**
- A topic with real debate, conflict, or multi-speaker input → **Deep Dive** (not Smoke Detector)
- A controversial spending item without much debate → **Smoke Detector** (not The Checkbook)
- A routine contract renewal (even a large one) with no real debate → **Checkbook** for the dollar amount AND/OR **Smoke Detector** if it's no-bid or unusual — but NOT Deep Dive. Deep Dive is reserved for stories with genuine complexity, conflict, or multi-speaker deliberation. A simple "they renewed it again" is not Deep Dive material.
- A parks/facility topic that is also a Deep Dive-worthy debate → **Deep Dive** (not Field & Facility Watch)
- An upcoming date from any section → also include a one-line entry in **Save the Date** (the calendar is a quick-reference list, not analysis — this is the ONE exception to the no-duplication rule)

**Before writing each section, mentally check:** "Did I already cover this topic above?" If yes, skip it and find something new.

**Structure:**
Use the following Markdown structure exactly:

# 🚨 The Headlines
(Select exactly 3 headlines that impact a resident's daily life or wallet. Use this hierarchy to pick:
1. **Tier 1 (Immediate Impact):** Tax hikes, fee increases, trash costs, or major construction closing a road.
2. **Tier 2 (Neighborhood Wars):** Zoning battles, developer vs. resident conflicts, parking fights.
3. **Tier 3 (School Safety/Quality):** Redistricting, curriculum changes, or safety protocols.

**BANNED from Headlines:**
- Retirements or procedural appointments (e.g., "Jane Doe appointed to Library Board").
- "Tabled" items — UNLESS there was a public fight. If it was tabled for a technicality, skip it.
- "Clean Audits" or "Bond Reviews" — unless they found fraud or a major shortfall.

**Drafting Rule:** Each headline must focus on the *conflict* or the *cost*, not the procedural step.
- Bad: "Trash Study Update Given."
- Good: "Municipal Trash Service Could Cost Taxpayers $2M More Per Year."

Format each as a punchy, click-worthy bullet. These are teasers only — do not repeat the full analysis here.)

# 🏛️ The Deep Dive
(Pick the top topics from ANY meeting this week that involve real debate, conflict, or significant decisions — typically 3-5 topics, but more if the week was eventful. Prioritize variety across meeting bodies: don't let one meeting dominate if other bodies had interesting stories too.

For each topic, write it in the style of an investigative summary using this structure:

1. **The Numbers:** Lead with the specific dollar figure, cost difference, or measurable impact. (e.g., "$6.2M vs. $4.5M" or "76 parking spaces proposed where 96 are required"). If the topic isn't financial, lead with the concrete stakes (e.g., "6 candidates for 1 seat").
2. **The Hidden Detail:** Find the one specific detail a resident would have missed if they weren't in the room — the offhand admission, the awkward silence, the buried caveat. (e.g., "The Board admitted one broken truck could shut down the entire operation" or "The developer did zero outreach to adjacent properties").
3. **The "Next Step" Prediction:** Based on the Board's tone and body language cues in the transcript, what will likely happen next? Be specific. (e.g., "Given the $2M gap, expect the Board to renew the private contract rather than going in-house" or "The Board tabled all three motions — expect revised plans at the March meeting").

This section is for topics with substance — not routine approvals or one-line mentions.)

# 🗣️ Quote of the Week
(Find the most interesting, passionate, or funny quote from any meeting this week. Always include the speaker's role and which meeting it came from. If none exists, omit this section.)

# 💸 The Checkbook
(From across all meetings, list the top 3 largest *routine* dollar amounts — contracts, vendor payments, capital purchases. Format each as: "**$Amount:** [Item Description] (Who gets the money)". Do NOT include spending items that are controversial — those belong in the Smoke Detector. If no dollar amounts were mentioned, omit this section.)

# 🏟️ Field & Facility Watch
(Quick-hit updates on sports and parks facilities from any meeting. Scan for: Turf, Grass, Permits, Lights, Ice Rink, Pool, Courts, Wildcat Fields, Middle Field, Main Park, Bird Park, Robb Hollow, Hidden Hollow, or Coaching Appointments. Summarize each in 1 sentence. Do NOT include items already covered in The Deep Dive. If none of these topics came up, omit this section.)

# 🕵️‍♂️ The Smoke Detector
(Find items that would make a resident angry or worried — from any meeting this week that was NOT already covered in The Deep Dive. NEVER repeat a topic from The Deep Dive here — find something new.

Do NOT include:
- New business openings (this is PR, not news).
- Routine applications (like "Bird Town" or "Tree City" designations).
- "Clean audits" (this is expected, not newsworthy).

DO include:

1. **The "Wait, What?" Financials:** Any cost estimate that is significantly higher than the current budget or existing contract. Use the **Structured Spending Log** and **Historical Context** above as your primary source. Flag jumps where in-house or proposed costs exceed current spending by 25%+. Highlight the delta (e.g., "Current contract: $4.5M → Proposed in-house: $6.2M — a 38% increase"). If a vendor appears multiple times in the historical data, note the cumulative total (e.g., "This is the 3rd payment to Gateway Engineers this year, totaling $221k").

2. **Legal/Liability Threats:** Mentions of "Executive Session," "Litigation," "Settlement," "Solicitor's Advice," or any indication of potential legal exposure. Note what topic triggered the legal discussion if disclosed.

3. **The "Quiet No":** When a Board receives a resident request and kills it with "further study," "we'll look into it," or "this needs more research" — without committing to a timeline or next step. This is the bureaucratic pocket veto. Name the request, the requester (if public), and whether any follow-up was promised.

4. **Zoning Fights:** Specific mentions of "parking variances," "traffic studies," "setback variances," "multi-family," "ADU," "density," or "character of the neighborhood" — especially where residents testified *against* a developer. Flag any project that puts something "big" next to something "small." Always include street names.

5. **Split Vote Alert:** Use the **Structured Vote Log** above. Report any vote that is NOT unanimous (e.g., 4-1 or 3-2). Identify EXACTLY who voted "No" or abstained and summarize their reason. Omit routine unanimous approvals entirely.

Format each item as: "⚠️ **[Category]:** [Headline] — [Why it's risky or controversial for residents]."

If none of these patterns appear, omit this section.)

# 📉 The Disconnect Index
(Compare what residents said during Public Comment with how the Board actually voted on the same topic. Follow these steps:

1. **Analyze the Room:** Count how many residents spoke FOR and AGAINST each topic during citizen/public comments.
2. **Analyze the Vote:** How did the Board vote on that same topic? (Approved, denied, delayed, tabled, etc.)
3. **Calculate the Gap:** If the majority of public speakers favored one outcome but the Board voted differently (against, delayed, or tabled), flag it as a Disconnect Alert.

Format each disconnect as:
"⚠️ **The Room vs. The Board:** [X] residents spoke [for/against] [topic], but the Board voted to [action]. [X]% of speakers were on the opposite side of the final vote."

If public comment and board votes were aligned, or if no clear public comment occurred on voted items, omit this section entirely. Only flag genuine disconnects where residents showed up to speak and the outcome went the other way.)

# 📅 Save the Date
(Filter dates from ALL meetings using this strict logic:

**KEEP:**
- **Public Utility:** "No School" days, trash delay/schedule changes, tax deadlines.
- **High Stakes Meetings:** Public hearings, zoning appeals, or any meeting where a specific controversial vote is scheduled.
- **Future Only:** DISCARD any date that has already passed.

**DISCARD:**
- Generic "Regular Meeting" dates — UNLESS a specific controversial topic is on that meeting's agenda.
- Internal scheduling meetings, awards banquets, or staff development days that don't affect residents.

Format each as: "**[Date]:** [Event Name] ([Why you should go / What is being decided])."

OK to include dates from topics covered in earlier sections — this is a call-to-action calendar, not analysis. If no actionable future dates were mentioned, omit this section.)
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


def load_text_files(directory: Path, lookback_days: int | None = None) -> list[dict]:
    """Load .txt files from a directory, optionally filtered by date prefix.

    Files are expected to start with a YYYY-MM-DD date prefix.  When
    *lookback_days* is set, only files whose date is within that window
    are loaded.
    """
    files = []
    if not directory.exists():
        return files

    if lookback_days is not None:
        cutoff = datetime.now() - timedelta(days=lookback_days)
    else:
        cutoff = None

    for fp in sorted(directory.glob("*.txt")):
        if cutoff is not None:
            date_str = fp.name[:10]  # "YYYY-MM-DD"
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    continue
            except ValueError:
                pass  # Filename doesn't start with a date — include it
        files.append({
            "filename": fp.name,
            "content": fp.read_text(encoding="utf-8"),
        })
    return files


# ---------------------------------------------------------------------------
# Vote Parsing
# ---------------------------------------------------------------------------

def parse_votes(llm_output: str, source: str) -> list[dict]:
    """Extract structured vote records from the vote-log fenced block in LLM output."""
    votes: list[dict] = []
    match = re.search(r"```vote-log\s*\n(.*?)```", llm_output, re.DOTALL)
    if not match:
        return votes
    block = match.group(1).strip()
    if not block:
        return votes
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            record["source_file"] = source
            votes.append(record)
        except json.JSONDecodeError:
            log.warning("Skipping malformed vote-log line in %s: %s", source, line[:80])
    return votes


def parse_spending(llm_output: str, source: str) -> list[dict]:
    """Extract structured spending records from the spending-log fenced block in LLM output."""
    items: list[dict] = []
    match = re.search(r"```spending-log\s*\n(.*?)```", llm_output, re.DOTALL)
    if not match:
        return items
    block = match.group(1).strip()
    if not block:
        return items
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            record["source_file"] = source
            items.append(record)
        except json.JSONDecodeError:
            log.warning("Skipping malformed spending-log line in %s: %s", source, line[:80])
    return items


def save_votes(all_votes: list[dict]) -> Path | None:
    """Persist the week's vote records to data/votes/ as JSON."""
    if not all_votes:
        return None
    VOTES_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filepath = VOTES_DIR / f"votes_{timestamp}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(all_votes, f, indent=2, ensure_ascii=False)
    log.info("Saved %d vote record(s) to %s", len(all_votes), filepath.name)
    return filepath


def format_votes_for_newsletter(all_votes: list[dict]) -> str:
    """Build a concise text summary of votes for inclusion in the Phase 2 prompt.

    Only noteworthy votes (split votes, abstentions) get full detail.
    Unanimous votes are summarized in aggregate.
    """
    if not all_votes:
        return ""

    noteworthy = [v for v in all_votes if not v.get("unanimous", True)]
    unanimous = [v for v in all_votes if v.get("unanimous", True)]

    parts: list[str] = ["## Structured Vote Log\n"]

    if noteworthy:
        parts.append("### Non-Unanimous / Noteworthy Votes\n")
        for v in noteworthy:
            parts.append(f"- **{v.get('meeting', 'Unknown')}** — {v.get('motion', 'N/A')}\n")
            parts.append(f"  Result: {v.get('result', 'N/A')}\n")
            if v.get("no"):
                parts.append(f"  Opposed: {', '.join(v['no'])}\n")
            if v.get("abstain"):
                parts.append(f"  Abstained: {', '.join(v['abstain'])}\n")
            if v.get("context"):
                parts.append(f"  Context: {v['context']}\n")
            parts.append("\n")

    if unanimous:
        parts.append(f"### Unanimous Votes ({len(unanimous)} total)\n")
        for v in unanimous:
            parts.append(f"- {v.get('meeting', 'Unknown')}: {v.get('motion', 'N/A')} ({v.get('result', 'Passed')})\n")

    return "".join(parts)


def format_spending_for_newsletter(all_spending: list[dict]) -> str:
    """Build a text summary of spending items for inclusion in the Phase 2 prompt."""
    if not all_spending:
        return ""

    parts: list[str] = ["## Structured Spending Log\n\n"]
    sorted_items = sorted(all_spending, key=lambda s: float(s.get("amount", 0)), reverse=True)

    for s in sorted_items:
        amount = float(s.get("amount", 0))
        line = f"- **${amount:,.2f}** — {s.get('description', 'N/A')}"
        if s.get("vendor") and s["vendor"] != "N/A":
            line += f" (Vendor: {s['vendor']})"
        if s.get("project"):
            line += f" [Project: {s['project']}]"
        if s.get("category"):
            line += f" [{s['category']}]"
        parts.append(line + "\n")

    return "".join(parts)


def build_historical_context() -> str:
    """Query Supabase for historical spending and vote patterns.

    Returns a text block to prepend to the Phase 2 prompt, or empty string
    if Supabase is not enabled or no historical data exists.
    """
    import db

    if not db.is_enabled():
        return ""

    parts: list[str] = ["## Historical Context (from database)\n\n"]
    has_content = False

    # Spending by vendor — highlight repeat vendors
    summary = db.get_recent_spending_summary(lookback_days=365)
    repeat_vendors = {
        v: data for v, data in summary["by_vendor"].items()
        if data["count"] >= 2 and v not in ("N/A", "Unknown")
    }
    if repeat_vendors:
        has_content = True
        parts.append("### Repeat Vendors (2+ payments this year)\n")
        for vendor, data in sorted(
            repeat_vendors.items(), key=lambda x: x[1]["total"], reverse=True
        ):
            parts.append(
                f"- **{vendor}**: {data['count']} payments totaling ${data['total']:,.2f}\n"
            )
        parts.append("\n")

    # Project cumulative spending
    if summary["by_project"]:
        has_content = True
        parts.append("### Project Spending Totals\n")
        for project, data in sorted(
            summary["by_project"].items(), key=lambda x: x[1]["total"], reverse=True
        ):
            parts.append(
                f"- **{project}**: ${data['total']:,.2f} across {data['count']} line items\n"
            )
        parts.append("\n")

    # Non-unanimous vote patterns by official
    dissent = db.get_dissent_summary(lookback_days=365)
    if dissent:
        has_content = True
        parts.append("### Dissent Patterns (non-unanimous votes)\n")
        for name, data in sorted(
            dissent.items(), key=lambda x: x[1]["no_count"], reverse=True
        ):
            topics_preview = "; ".join(data["topics"][:3])
            no_str = f"voted No on {data['no_count']} item(s)" if data["no_count"] else ""
            abstain_str = f"abstained on {data['abstain_count']} item(s)" if data["abstain_count"] else ""
            combined = ", ".join(filter(None, [no_str, abstain_str]))
            parts.append(f"- **{name}**: {combined} (e.g., {topics_preview})\n")
        parts.append("\n")

    return "".join(parts) if has_content else ""


# ---------------------------------------------------------------------------
# LLM Calls
# ---------------------------------------------------------------------------

def analyze_with_openai(system_prompt: str, user_prompt: str, model: str = "gpt-4o", max_tokens: int = 4000) -> str:
    """Call the OpenAI Chat Completions API."""
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        temperature=0.7,
    )
    return response.choices[0].message.content


_anthropic_client = None


def _get_anthropic_client():
    """Reuse a single Anthropic client (keeps the TCP connection alive)."""
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            timeout=300.0,
            max_retries=3,
        )
    return _anthropic_client


def analyze_with_anthropic(system_prompt: str, user_prompt: str, model: str = "claude-sonnet-4-5-20250514", max_tokens: int = 4000) -> str:
    """Call the Anthropic Messages API."""
    client = _get_anthropic_client()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.7,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        timeout=300.0,
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _match_by_date(source_filename: str, docs: list[dict]) -> list[dict]:
    """Return only docs whose date prefix matches the source file's date."""
    date_prefix = source_filename[:10]  # "YYYY-MM-DD"
    return [d for d in docs if d["filename"].startswith(date_prefix)]


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
    if len(content) > 50_000:
        content = content[:50_000] + "\n\n[Transcript truncated for length]"
    parts.append(content)
    parts.append("\n\n")

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
    if len(content) > 50_000:
        content = content[:50_000] + "\n\n[Minutes truncated for length]"
    parts.append(content)
    parts.append("\n\n")

    return "".join(parts)


def build_newsletter_prompt(
    meeting_extracts: list[dict],
    budget_docs: list[dict] | None = None,
    all_votes: list[dict] | None = None,
    all_spending: list[dict] | None = None,
    historical_context: str = "",
) -> str:
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

    if historical_context:
        parts.append(historical_context)
        parts.append("\n---\n\n")

    if all_votes:
        parts.append(format_votes_for_newsletter(all_votes))
        parts.append("\n---\n\n")

    if all_spending:
        parts.append(format_spending_for_newsletter(all_spending))
        parts.append("\n---\n\n")

    parts.append("## This Week's Meeting Notes\n\n")
    for extract in meeting_extracts:
        parts.append(f"### {extract['source']}\n")
        parts.append(extract["notes"])
        parts.append("\n\n---\n\n")

    parts.append("Generate the newsletter now based on the meeting notes above.")
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


def _call_llm(provider: str, model: str, system_prompt: str, user_prompt: str, max_retries: int = 3, max_tokens: int = 4000) -> str:
    """Route to the correct LLM provider with retry + exponential backoff.

    When provider is 'anthropic', retryable failures (connection errors,
    timeouts, overloaded) will first retry with Anthropic. After all retries
    are exhausted, falls back to OpenAI as a last resort (if OPENAI_API_KEY
    is set).
    """
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            if provider == "openai":
                return analyze_with_openai(system_prompt, user_prompt, model=model, max_tokens=max_tokens)
            return analyze_with_anthropic(system_prompt, user_prompt, model=model, max_tokens=max_tokens)
        except Exception as exc:
            last_exc = exc
            is_retryable = any(
                keyword in str(exc).lower()
                for keyword in ("connection", "timeout", "overloaded", "529", "503", "rate")
            )
            if is_retryable and attempt < max_retries:
                wait = 30 * (2 ** (attempt - 1))  # 30s, 60s, 120s
                log.warning(
                    "LLM call failed (attempt %d/%d): %s — retrying in %ds…",
                    attempt, max_retries, exc, wait,
                )
                time.sleep(wait)
            elif is_retryable and provider == "anthropic":
                # Fallback chain: Sonnet → Haiku → OpenAI
                if "sonnet" in model:
                    log.warning(
                        "Anthropic Sonnet failed after %d attempts: %s — falling back to Haiku…",
                        max_retries, exc,
                    )
                    return analyze_with_anthropic(system_prompt, user_prompt, model="claude-haiku-4-5", max_tokens=max_tokens)
                elif os.environ.get("OPENAI_API_KEY"):
                    log.warning(
                        "Anthropic Haiku failed: %s — falling back to OpenAI (gpt-4o)…",
                        exc,
                    )
                    return analyze_with_openai(system_prompt, user_prompt, model="gpt-4o", max_tokens=max_tokens)
                else:
                    raise
            else:
                raise


# ---------------------------------------------------------------------------
# Filename Helpers
# ---------------------------------------------------------------------------


def _extract_body_from_filename(filename: str) -> str:
    """Extract meeting body name from transcript/minutes filename.

    Examples:
        '2026-01-28_Municipality_Commission_Meeting_-_01272026.txt' -> 'Commission Meeting'
        '2026-01-27_SchoolBoard_Regular_Meeting_-_01272026.txt' -> 'Regular Meeting'
        '2026-01-27_mtleb_minutes_CM.txt' -> 'CM'
    """
    name = filename.rsplit(".", 1)[0]  # strip extension
    if len(name) > 11:
        name = name[11:]  # strip "YYYY-MM-DD_" date prefix
    parts = name.split("_")
    # Remove leading source identifier
    known_sources = {"Municipality", "SchoolBoard", "SchoolBoardPresentations", "mtleb", "minutes"}
    while parts and parts[0] in known_sources:
        parts.pop(0)
    # Remove trailing date-like parts (e.g., "01272026")
    while parts and parts[-1].replace("-", "").isdigit():
        parts.pop()
    # Remove trailing dash separator
    while parts and parts[-1] == "-":
        parts.pop()
    return " ".join(parts) or "Unknown Meeting"


def _extract_url_from_content(content: str) -> str | None:
    """Extract YouTube URL from transcript header lines."""
    for line in content.split("\n")[:10]:
        if line.startswith("URL:"):
            return line[4:].strip()
    return None


# ---------------------------------------------------------------------------
# Extraction Cache — persist Phase 1 results so re-runs skip completed files
# ---------------------------------------------------------------------------


def _cache_key(filename: str) -> Path:
    """Return the cache file path for a given source filename."""
    safe_name = filename.replace("/", "_").rsplit(".", 1)[0]
    return EXTRACT_CACHE_DIR / f"{safe_name}.json"


def _load_cached_extract(filename: str) -> dict | None:
    """Load a cached extraction result. Returns None if not cached."""
    cache_path = _cache_key(filename)
    if not cache_path.exists():
        return None
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        log.info("Loaded cached extract for %s", filename)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to load cache for %s: %s", filename, exc)
        return None


def _save_cached_extract(filename: str, notes: str, votes: list[dict], spending: list[dict]):
    """Save an extraction result to the cache."""
    EXTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _cache_key(filename)
    data = {
        "source": filename,
        "notes": notes,
        "votes": votes,
        "spending": spending,
        "cached_at": datetime.now().isoformat(),
    }
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        log.warning("Failed to save cache for %s: %s", filename, exc)


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
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        default=False,
        help="Skip files that already have cached extractions and only process new/failed ones",
    )
    parser.add_argument(
        "--digest-only",
        action="store_true",
        default=False,
        help="Skip Phase 1 entirely — load all cached extracts and regenerate the newsletter (Phase 2 only)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=14,
        help="Only process transcripts/minutes from the last N days (default: 14)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        default=False,
        help="Clear the extraction cache before running (forces re-extraction of everything)",
    )
    args = parser.parse_args()

    # Handle --clear-cache
    if args.clear_cache and EXTRACT_CACHE_DIR.exists():
        import shutil
        shutil.rmtree(EXTRACT_CACHE_DIR)
        log.info("Cleared extraction cache")

    model = args.model
    if not model:
        model = "claude-sonnet-4-5-20250514" if args.provider == "anthropic" else "gpt-4o"

    # Load data — only files within the lookback window
    lookback = args.lookback_days
    if args.file:
        p = Path(args.file)
        transcripts = [{"filename": p.name, "content": p.read_text(encoding="utf-8")}]
    else:
        transcripts = load_text_files(TRANSCRIPTS_DIR, lookback_days=lookback)

    agendas = load_text_files(AGENDAS_DIR, lookback_days=lookback)
    minutes = load_text_files(MINUTES_DIR, lookback_days=lookback)
    budget_docs = load_text_files(BUDGET_DIR)

    if not transcripts and not minutes:
        log.error("No transcripts or minutes found. Run scripts/ingest_data.py first.")
        raise SystemExit(1)

    log.info(
        "Loaded %d transcript(s), %d agenda(s), %d minutes file(s), %d budget doc(s)",
        len(transcripts), len(agendas), len(minutes), len(budget_docs),
    )
    context = load_context()

    # Build separate system prompts for Phase 1 (extraction) and Phase 2 (newsletter).
    # Putting the extraction instructions in the system prompt avoids sending the
    # 4,500-char EXTRACT_PROMPT in every user message — significant token savings
    # when processing many meetings.
    extract_system_prompt = (context + "\n\n" + EXTRACT_PROMPT) if context else EXTRACT_PROMPT
    newsletter_system_prompt = (context + "\n\n" + NEWSLETTER_PROMPT) if context else NEWSLETTER_PROMPT

    # -----------------------------------------------------------------------
    # --digest-only: Skip Phase 1, load all cached extracts, jump to Phase 2
    # -----------------------------------------------------------------------
    if args.digest_only:
        meeting_extracts: list[dict] = []
        all_votes: list[dict] = []
        all_spending: list[dict] = []

        if not EXTRACT_CACHE_DIR.exists():
            log.error("No extraction cache found at %s. Run a full analysis first.", EXTRACT_CACHE_DIR)
            raise SystemExit(1)

        # Only load caches matching current transcripts/minutes filenames.
        # This prevents stale extracts from prior weeks from bloating the prompt.
        current_sources = {t["filename"].rsplit(".", 1)[0] for t in transcripts}
        current_sources |= {m["filename"].rsplit(".", 1)[0] for m in minutes}

        cache_files = sorted(EXTRACT_CACHE_DIR.glob("*.json"))
        if current_sources:
            cache_files = [f for f in cache_files if f.stem in current_sources]

        if not cache_files:
            log.error("No matching cached extracts found. Run a full analysis first.")
            raise SystemExit(1)

        for cache_path in cache_files:
            try:
                with open(cache_path, encoding="utf-8") as f:
                    cached = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Skipping corrupt cache file %s: %s", cache_path.name, exc)
                continue

            votes = cached.get("votes", [])
            spending = cached.get("spending", [])
            all_votes.extend(votes)
            all_spending.extend(spending)
            meeting_extracts.append({
                "source": cached.get("source", cache_path.stem),
                "notes": cached.get("notes", ""),
            })
            log.info(
                "Loaded cached extract: %s (%d votes, %d spending items)",
                cached.get("source", cache_path.name), len(votes), len(spending),
            )

        if not meeting_extracts:
            log.error("No valid cached extracts found. Run a full analysis first.")
            raise SystemExit(1)

        log.info("Digest-only mode: loaded %d cached extract(s)", len(meeting_extracts))

        # Save votes locally
        log.info("Collected %d vote record(s) across all meetings", len(all_votes))
        save_votes(all_votes)

        # Build historical context + Phase 2
        historical_context = build_historical_context()
        if historical_context:
            log.info("Built historical context from Supabase (%d chars)", len(historical_context))

        log.info("Phase 2 — Generating consolidated newsletter from %d meetings…", len(meeting_extracts))
        newsletter_prompt = build_newsletter_prompt(
            meeting_extracts,
            budget_docs=budget_docs,
            all_votes=all_votes,
            all_spending=all_spending,
            historical_context=historical_context,
        )
        log.info("Sending to %s (%s)…", args.provider, model)

        try:
            newsletter = _call_llm(args.provider, model, newsletter_system_prompt, newsletter_prompt, max_tokens=8000)
        except Exception as exc:
            log.error("Newsletter generation failed: %s", exc)
            raise SystemExit(1)

        draft_path = save_draft(newsletter, label="weekly_digest")

        print("\n" + "=" * 80)
        print("  Lebo Board Watch — Weekly Digest")
        print("=" * 80)
        print(newsletter)
        print(f"\nDraft saved to: {draft_path}\n")
        return

    # -----------------------------------------------------------------------
    # Phase 1: Extract key facts from each transcript individually.
    # This keeps each LLM call within token limits.
    # -----------------------------------------------------------------------
    meeting_extracts: list[dict] = []
    all_votes: list[dict] = []
    all_spending: list[dict] = []
    meeting_id_map: dict[str, str] = {}  # filename -> Supabase meeting UUID
    call_count = 0

    for transcript in transcripts:
        # Check cache first when --retry-failed is set
        if args.retry_failed:
            cached = _load_cached_extract(transcript["filename"])
            if cached:
                votes = cached.get("votes", [])
                spending = cached.get("spending", [])
                all_votes.extend(votes)
                all_spending.extend(spending)
                meeting_extracts.append({
                    "source": cached["source"],
                    "notes": cached["notes"],
                })
                log.info(
                    "Using cached extract for %s (%d votes, %d spending items)",
                    transcript["filename"], len(votes), len(spending),
                )
                continue

        if call_count > 0:
            delay = RATE_LIMIT_DELAY.get(args.provider, 60)
            log.info("Waiting %ds for rate limit cooldown…", delay)
            time.sleep(delay)

        log.info("Phase 1 — Extracting: %s", transcript["filename"])
        matched_agendas = _match_by_date(transcript["filename"], agendas)
        matched_minutes = _match_by_date(transcript["filename"], minutes)
        user_prompt = build_extract_prompt(transcript, matched_agendas, matched_minutes)
        log.info("Sending to %s (%s)…", args.provider, model)

        try:
            notes = _call_llm(args.provider, model, extract_system_prompt, user_prompt)
        except Exception as exc:
            log.error("Extraction failed for %s: %s", transcript["filename"], exc)
            continue

        votes = parse_votes(notes, source=transcript["filename"])
        all_votes.extend(votes)
        spending = parse_spending(notes, source=transcript["filename"])
        all_spending.extend(spending)
        meeting_extracts.append({
            "source": transcript["filename"],
            "notes": notes,
        })
        call_count += 1
        log.info(
            "Extracted notes from %s (%d chars, %d votes, %d spending items)",
            transcript["filename"], len(notes), len(votes), len(spending),
        )

        # Cache the successful extraction
        _save_cached_extract(transcript["filename"], notes, votes, spending)

        # Persist to Supabase
        if db.is_enabled():
            meeting_date = transcript["filename"][:10]
            body_name = _extract_body_from_filename(transcript["filename"])
            youtube_url = _extract_url_from_content(transcript["content"])
            meeting_id = db.upsert_meeting(
                meeting_date=meeting_date,
                body=body_name,
                source_filename=transcript["filename"],
                source_type="transcript",
                youtube_url=youtube_url,
                extract_text=notes,
            )
            if meeting_id:
                meeting_id_map[transcript["filename"]] = meeting_id
                db.upsert_votes(meeting_id, votes)
                db.upsert_spending(meeting_id, spending, fiscal_year=int(meeting_date[:4]))
                db.sync_officials_from_votes(votes, body=body_name)

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

        # Check cache first when --retry-failed is set
        if args.retry_failed:
            cached = _load_cached_extract(minutes_doc["filename"])
            if cached:
                votes = cached.get("votes", [])
                spending = cached.get("spending", [])
                all_votes.extend(votes)
                all_spending.extend(spending)
                meeting_extracts.append({
                    "source": cached["source"],
                    "notes": cached["notes"],
                })
                log.info(
                    "Using cached extract for %s (%d votes, %d spending items)",
                    minutes_doc["filename"], len(votes), len(spending),
                )
                continue

        if call_count > 0:
            delay = RATE_LIMIT_DELAY.get(args.provider, 60)
            log.info("Waiting %ds for rate limit cooldown…", delay)
            time.sleep(delay)

        log.info("Phase 1b — Extracting from minutes: %s", minutes_doc["filename"])
        matched_agendas = _match_by_date(minutes_doc["filename"], agendas)
        user_prompt = build_minutes_extract_prompt(minutes_doc, matched_agendas)
        log.info("Sending to %s (%s)…", args.provider, model)

        try:
            notes = _call_llm(args.provider, model, extract_system_prompt, user_prompt)
        except Exception as exc:
            log.error("Extraction failed for %s: %s", minutes_doc["filename"], exc)
            continue

        votes = parse_votes(notes, source=minutes_doc["filename"])
        all_votes.extend(votes)
        spending = parse_spending(notes, source=minutes_doc["filename"])
        all_spending.extend(spending)
        meeting_extracts.append({
            "source": minutes_doc["filename"],
            "notes": notes,
        })
        call_count += 1
        log.info(
            "Extracted notes from minutes %s (%d chars, %d votes, %d spending items)",
            minutes_doc["filename"], len(notes), len(votes), len(spending),
        )

        # Cache the successful extraction
        _save_cached_extract(minutes_doc["filename"], notes, votes, spending)

        # Persist to Supabase
        if db.is_enabled():
            meeting_date = minutes_doc["filename"][:10]
            body_name = _extract_body_from_filename(minutes_doc["filename"])
            meeting_id = db.upsert_meeting(
                meeting_date=meeting_date,
                body=body_name,
                source_filename=minutes_doc["filename"],
                source_type="minutes",
                extract_text=notes,
            )
            if meeting_id:
                meeting_id_map[minutes_doc["filename"]] = meeting_id
                db.upsert_votes(meeting_id, votes)
                db.upsert_spending(meeting_id, spending, fiscal_year=int(meeting_date[:4]))
                db.sync_officials_from_votes(votes, body=body_name)

    if not meeting_extracts:
        log.error("No meeting extracts produced. Cannot generate newsletter.")
        raise SystemExit(1)

    # -----------------------------------------------------------------------
    # Local file persistence (votes + spending summary)
    # -----------------------------------------------------------------------
    log.info("Collected %d vote record(s) across all meetings", len(all_votes))
    noteworthy_count = sum(1 for v in all_votes if not v.get("unanimous", True))
    log.info("  ↳ %d noteworthy (non-unanimous / abstentions)", noteworthy_count)
    votes_path = save_votes(all_votes)
    if votes_path:
        log.info("Vote log saved to: %s", votes_path)

    log.info("Collected %d spending item(s) across all meetings", len(all_spending))

    # -----------------------------------------------------------------------
    # Build historical context from Supabase for Phase 2
    # -----------------------------------------------------------------------
    historical_context = build_historical_context()
    if historical_context:
        log.info("Built historical context from Supabase (%d chars)", len(historical_context))

    # -----------------------------------------------------------------------
    # Phase 2: Combine all extracts into one consolidated weekly newsletter.
    # -----------------------------------------------------------------------
    if len(meeting_extracts) > 1:
        delay = PHASE2_DELAY.get(args.provider, 90)
        log.info("Waiting %ds before Phase 2 (token bucket refill)…", delay)
        time.sleep(delay)

    log.info("Phase 2 — Generating consolidated newsletter from %d meetings…", len(meeting_extracts))
    newsletter_prompt = build_newsletter_prompt(
        meeting_extracts,
        budget_docs=budget_docs,
        all_votes=all_votes,
        all_spending=all_spending,
        historical_context=historical_context,
    )
    log.info("Sending to %s (%s)…", args.provider, model)

    try:
        newsletter = _call_llm(args.provider, model, newsletter_system_prompt, newsletter_prompt, max_tokens=8000)
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
