#!/usr/bin/env python3
"""
Board Watch — Supabase persistence layer.

Gracefully degrades to no-op if SUPABASE_URL / SUPABASE_KEY are not set.
All public functions accept plain dicts (matching LLM output shapes) and
handle the mapping to database columns internally.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Client initialization
# ---------------------------------------------------------------------------

_supabase = None  # Module-level singleton
_init_attempted = False


def _get_client():
    """Lazy-init Supabase client. Returns None if env vars missing."""
    global _supabase, _init_attempted
    if _init_attempted:
        return _supabase

    _init_attempted = True
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")

    if not url or not key:
        log.info("SUPABASE_URL/SUPABASE_KEY not set — running in file-only mode")
        return None

    try:
        from supabase import create_client
        _supabase = create_client(url, key)
        log.info("Supabase client initialized")
    except Exception as exc:
        log.warning("Failed to initialize Supabase client: %s", exc)
        _supabase = None

    return _supabase


def is_enabled() -> bool:
    """Return True if Supabase is configured and reachable."""
    return _get_client() is not None


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------


def upsert_meeting(
    meeting_date: str,
    body: str,
    source_filename: str,
    source_type: str = "transcript",
    youtube_url: str | None = None,
    extract_text: str | None = None,
) -> Optional[str]:
    """Insert or update a meeting row. Returns the meeting UUID or None."""
    client = _get_client()
    if not client:
        return None

    row = {
        "meeting_date": meeting_date,
        "body": body,
        "source_filename": source_filename,
        "source_type": source_type,
    }
    if youtube_url:
        row["youtube_url"] = youtube_url
    if extract_text:
        row["extract_text"] = extract_text

    try:
        result = (
            client.table("meetings")
            .upsert(row, on_conflict="meeting_date,body")
            .execute()
        )
        meeting_id = result.data[0]["id"] if result.data else None
        log.info("Upserted meeting %s / %s -> %s", meeting_date, body, meeting_id)
        return meeting_id
    except Exception as exc:
        log.error("Failed to upsert meeting %s / %s: %s", meeting_date, body, exc)
        return None


def upsert_votes(meeting_id: str, votes: list[dict]) -> int:
    """Insert votes for a meeting. Deletes existing votes first for idempotency."""
    client = _get_client()
    if not client or not votes:
        return 0

    try:
        client.table("votes").delete().eq("meeting_id", meeting_id).execute()

        rows = []
        for v in votes:
            rows.append({
                "meeting_id": meeting_id,
                "motion": v.get("motion", ""),
                "result": v.get("result", ""),
                "unanimous": v.get("unanimous", True),
                "yes_names": v.get("yes", []),
                "no_names": v.get("no", []),
                "abstain_names": v.get("abstain", []),
                "context": v.get("context", ""),
            })

        result = client.table("votes").insert(rows).execute()
        count = len(result.data) if result.data else 0
        log.info("Inserted %d vote(s) for meeting %s", count, meeting_id)
        return count
    except Exception as exc:
        log.error("Failed to upsert votes for meeting %s: %s", meeting_id, exc)
        return 0


def upsert_spending(
    meeting_id: str, items: list[dict], fiscal_year: int | None = None
) -> int:
    """Insert spending items for a meeting. Deletes existing items first for idempotency."""
    client = _get_client()
    if not client or not items:
        return 0

    try:
        client.table("spending_items").delete().eq("meeting_id", meeting_id).execute()

        rows = []
        for s in items:
            rows.append({
                "meeting_id": meeting_id,
                "vendor": s.get("vendor", "Unknown"),
                "amount": float(s.get("amount", 0)),
                "description": s.get("description", ""),
                "category": s.get("category", "routine"),
                "project": s.get("project"),
                "budget_line": s.get("budget_line"),
                "fiscal_year": fiscal_year or datetime.now().year,
                "contract_term": s.get("contract_term"),
            })

        result = client.table("spending_items").insert(rows).execute()
        count = len(result.data) if result.data else 0
        log.info("Inserted %d spending item(s) for meeting %s", count, meeting_id)
        return count
    except Exception as exc:
        log.error("Failed to upsert spending for meeting %s: %s", meeting_id, exc)
        return 0


def upsert_official(name: str, body: str) -> Optional[str]:
    """Ensure an official exists in the officials table. Returns their UUID."""
    client = _get_client()
    if not client or not name:
        return None

    try:
        row = {"name": name, "body": body}
        result = (
            client.table("officials")
            .upsert(row, on_conflict="name,body")
            .execute()
        )
        return result.data[0]["id"] if result.data else None
    except Exception as exc:
        log.warning("Failed to upsert official %s / %s: %s", name, body, exc)
        return None


def sync_officials_from_votes(votes: list[dict], body: str):
    """Extract unique names from vote records and ensure they exist in officials."""
    if not is_enabled():
        return

    names: set[str] = set()
    for v in votes:
        for field in ("yes", "no", "abstain"):
            for name in v.get(field, []):
                stripped = name.strip()
                if stripped:
                    names.add(stripped)

    for name in names:
        upsert_official(name, body)


def upsert_newsletter(
    week_of: str,
    title: str,
    markdown_content: str,
    meeting_ids: list[str] | None = None,
    ghost_post_id: str | None = None,
    ghost_post_url: str | None = None,
) -> Optional[str]:
    """Insert or update a newsletter record. Returns UUID."""
    client = _get_client()
    if not client:
        return None

    row = {
        "week_of": week_of,
        "title": title,
        "markdown_content": markdown_content,
        "meeting_ids": meeting_ids or [],
    }
    if ghost_post_id:
        row["ghost_post_id"] = ghost_post_id
    if ghost_post_url:
        row["ghost_post_url"] = ghost_post_url

    try:
        result = (
            client.table("newsletters")
            .upsert(row, on_conflict="week_of")
            .execute()
        )
        newsletter_id = result.data[0]["id"] if result.data else None
        log.info("Upserted newsletter for week of %s -> %s", week_of, newsletter_id)
        return newsletter_id
    except Exception as exc:
        log.error("Failed to upsert newsletter for week of %s: %s", week_of, exc)
        return None


# ---------------------------------------------------------------------------
# Query helpers (for Phase 2 historical context)
# ---------------------------------------------------------------------------


def get_recent_spending_summary(lookback_days: int = 365) -> dict:
    """
    Aggregate spending by vendor and project within the lookback period.

    Returns:
        {"by_vendor": {name: {"total": float, "count": int, "items": [...]}},
         "by_project": {name: {"total": float, "count": int, "items": [...]}}}
    """
    client = _get_client()
    if not client:
        return {"by_vendor": {}, "by_project": {}}

    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    try:
        result = (
            client.table("spending_items")
            .select("vendor, amount, description, category, project, budget_line, contract_term, meeting_id, meetings(meeting_date, body)")
            .gte("created_at", cutoff)
            .order("amount", desc=True)
            .execute()
        )
    except Exception as exc:
        log.warning("Failed to query spending summary: %s", exc)
        return {"by_vendor": {}, "by_project": {}}

    by_vendor: dict = {}
    by_project: dict = {}

    for row in result.data or []:
        vendor = row.get("vendor", "Unknown")
        if vendor not in by_vendor:
            by_vendor[vendor] = {"total": 0.0, "count": 0, "items": []}
        by_vendor[vendor]["total"] += float(row.get("amount", 0))
        by_vendor[vendor]["count"] += 1
        by_vendor[vendor]["items"].append(row)

        project = row.get("project")
        if project:
            if project not in by_project:
                by_project[project] = {"total": 0.0, "count": 0, "items": []}
            by_project[project]["total"] += float(row.get("amount", 0))
            by_project[project]["count"] += 1
            by_project[project]["items"].append(row)

    return {"by_vendor": by_vendor, "by_project": by_project}


def get_dissent_summary(lookback_days: int = 365) -> dict:
    """
    Get non-unanimous votes and group by official who voted No or abstained.

    Returns:
        {name: {"no_count": int, "abstain_count": int, "topics": [str]}}
    """
    client = _get_client()
    if not client:
        return {}

    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    try:
        result = (
            client.table("votes")
            .select("motion, no_names, abstain_names, meetings(meeting_date, body)")
            .eq("unanimous", False)
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )
    except Exception as exc:
        log.warning("Failed to query dissent summary: %s", exc)
        return {}

    officials: dict = {}
    for row in result.data or []:
        motion = row.get("motion", "")
        for name in row.get("no_names") or []:
            if name not in officials:
                officials[name] = {"no_count": 0, "abstain_count": 0, "topics": []}
            officials[name]["no_count"] += 1
            officials[name]["topics"].append(motion)
        for name in row.get("abstain_names") or []:
            if name not in officials:
                officials[name] = {"no_count": 0, "abstain_count": 0, "topics": []}
            officials[name]["abstain_count"] += 1
            officials[name]["topics"].append(motion)

    return officials


def get_project_spending(project_name: str) -> list[dict]:
    """Get all spending items for a named project."""
    client = _get_client()
    if not client:
        return []

    try:
        result = (
            client.table("spending_items")
            .select("*, meetings(meeting_date, body)")
            .ilike("project", f"%{project_name}%")
            .order("created_at", desc=True)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        log.warning("Failed to query project spending for %s: %s", project_name, exc)
        return []
