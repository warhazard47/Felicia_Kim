"""
shared_context.py

Tiny helper that lets the three scripts share what Felicia has been up to:
- post_to_instagram.py  -> add_post(caption, hint, media_pk) after every successful post
- ig_bot.py             -> prompt_block() is added to her DM system prompt
- auto_reply_comments.py-> prompt_block() is added to her comment-reply prompt

Data lives in context.json (same folder as the scripts). Keep this file next to them.
"""

import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

CONTEXT_FILE = "context.json"
LOCAL_TZ = ZoneInfo("Asia/Jakarta")
MAX_STORED = 10       # posts kept in the file
MAX_IN_PROMPT = 3     # most recent posts shown to the model
MAX_AGE_DAYS = 14     # older posts are no longer "recent"


def _load():
    try:
        with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("recent_posts", [])
    return data


def _save(data):
    tmp = CONTEXT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONTEXT_FILE)   # atomic, so readers never see a half-written file


def add_post(caption, hint=None, media_pk=None):
    """Record a post that was just published."""
    data = _load()
    data["recent_posts"].append({
        "posted_at": datetime.now(LOCAL_TZ).isoformat(),
        "caption": caption or "",
        "hint": hint or "",
        "media_pk": str(media_pk) if media_pk else "",
    })
    data["recent_posts"] = data["recent_posts"][-MAX_STORED:]
    _save(data)


def _when(posted_at, now):
    days = (now.date() - posted_at.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def prompt_block():
    """Text to append to a system prompt. Empty string if there's nothing recent."""
    now = datetime.now(LOCAL_TZ)
    items = []
    for p in reversed(_load()["recent_posts"]):          # newest first
        try:
            posted_at = datetime.fromisoformat(p["posted_at"])
        except (KeyError, ValueError):
            continue
        if (now - posted_at).days > MAX_AGE_DAYS:
            continue

        caption = re.sub(r"#\w+", "", p.get("caption", ""))      # hashtags add nothing here
        caption = " ".join(caption.split())
        line = f"- Posted {_when(posted_at, now)}: \"{caption}\""
        if p.get("hint"):
            line += f" (what it was about: {p['hint']})"
        items.append(line)
        if len(items) >= MAX_IN_PROMPT:
            break

    if not items:
        return ""

    return (
        "\n\nWhat's been going on in your life lately (from your own recent Instagram posts):\n"
        + "\n".join(items)
        + "\n\nThese are real things that happened to you, so you can talk about them if the "
        "conversation goes there (for example if someone asks about the run, or mentions running "
        "or the event). Rules for using this:\n"
        "- Never bring these up unprompted, and never recite them like a list.\n"
        "- Only use what's written above. Don't invent extra details (finish times, placements, "
        "who you went with, places) that aren't listed.\n"
        "- If the conversation moves to a different topic, follow it. Don't steer back."
    )
