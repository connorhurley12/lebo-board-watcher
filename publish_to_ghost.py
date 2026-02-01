#!/usr/bin/env python3
"""
Board Watch — Publish to Ghost CMS
Creates a *draft* post on Ghost so you can review it before publishing.
"""

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
import jwt
import markdown

load_dotenv()
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)

DRAFTS_DIR = Path(__file__).parent / "data" / "drafts"

MONETIZATION_FOOTER = (
    "\n\n---\n\n"
    "<p>Keep Lebo Watch running: "
    '<a href="https://buymeacoffee.com/YOURPAGE">Buy Me a Coffee</a>.</p>'
)

# ---------------------------------------------------------------------------
# Ghost Admin API auth
# ---------------------------------------------------------------------------


def _make_ghost_token(admin_key: str) -> str:
    """
    Create a short-lived JWT for the Ghost Admin API.

    The GHOST_ADMIN_KEY is formatted as ``{id}:{secret}`` where *secret* is
    a hex-encoded 256-bit key.
    """
    key_id, secret_hex = admin_key.split(":")
    secret_bytes = bytes.fromhex(secret_hex)

    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "iat": now,
        "exp": now + 5 * 60,  # 5-minute expiry
        "aud": "/admin/",
    }
    headers = {"alg": "HS256", "typ": "JWT", "kid": key_id}

    return jwt.encode(payload, secret_bytes, algorithm="HS256", headers=headers)


# ---------------------------------------------------------------------------
# Draft creation
# ---------------------------------------------------------------------------


def create_draft(markdown_content: str, ghost_url: str, admin_key: str) -> dict:
    """
    Post a draft to Ghost.

    Returns the JSON response from the Ghost Admin API (includes the post URL).
    """
    token = _make_ghost_token(admin_key)

    # Convert Markdown → HTML and append the monetization footer
    html_body = markdown.markdown(markdown_content, extensions=["extra", "smarty"])
    html_body += MONETIZATION_FOOTER

    # Ghost expects content wrapped in a mobiledoc JSON structure
    mobiledoc = json.dumps(
        {
            "version": "0.3.1",
            "markups": [],
            "atoms": [],
            "cards": [["html", {"cardName": "html", "html": html_body}]],
            "sections": [[10, 0]],
        }
    )

    # Generate a weekly digest title with the current date
    title = f"Lebo Board Watch — Week of {datetime.now().strftime('%B %d, %Y')}"

    post_data = {
        "posts": [
            {
                "title": title,
                "mobiledoc": mobiledoc,
                "status": "draft",
                "tags": [
                    {"name": "Board Watch"},
                    {"name": "Mt. Lebanon"},
                ],
            }
        ]
    }

    api_endpoint = f"{ghost_url.rstrip('/')}/ghost/api/admin/posts/"
    headers = {
        "Authorization": f"Ghost {token}",
        "Content-Type": "application/json",
    }

    resp = requests.post(api_endpoint, headers=headers, json=post_data, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _find_latest_draft() -> Path | None:
    """Return the most recently modified .md file in data/drafts/."""
    if not DRAFTS_DIR.exists():
        return None
    files = sorted(DRAFTS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def main():
    parser = argparse.ArgumentParser(
        description="Board Watch — Publish draft to Ghost CMS"
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Path to a markdown draft. Defaults to the most recent file in data/drafts/.",
    )
    args = parser.parse_args()

    ghost_url = os.environ.get("GHOST_API_URL")
    admin_key = os.environ.get("GHOST_ADMIN_KEY")

    if not ghost_url or not admin_key:
        log.error("Set GHOST_API_URL and GHOST_ADMIN_KEY environment variables.")
        raise SystemExit(1)

    # Resolve the file to publish
    if args.file:
        draft_path = Path(args.file)
    else:
        draft_path = _find_latest_draft()

    if not draft_path or not draft_path.exists():
        log.error("No draft file found. Run analyze_meeting.py first.")
        raise SystemExit(1)

    log.info("Publishing draft: %s", draft_path.name)
    md_content = draft_path.read_text(encoding="utf-8")

    result = create_draft(md_content, ghost_url, admin_key)

    post = result["posts"][0]
    post_url = post.get("url", "")
    post_id = post.get("id", "")
    preview_url = f"{ghost_url.rstrip('/')}/p/{post.get('uuid', post_id)}/"

    print("\n" + "=" * 60)
    print("Draft created successfully!")
    print(f"  Title:   {post.get('title')}")
    print(f"  Status:  {post.get('status')}")
    print(f"  URL:     {post_url}")
    print(f"  Preview: {preview_url}")
    print("=" * 60 + "\n")
    print("Open the preview link above to review before publishing.")


if __name__ == "__main__":
    main()
