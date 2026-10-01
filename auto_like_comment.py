"""
auto_like_comment.py

Auto-like + auto-comment (ONCE only) on Instagram posts from a target account,
based on date. The comment is contextual, generated from the image + caption
(using a vision model on Groq) instead of a repeated template.

Example: run the script on the 17th -> it finds the FIRST post published on the
17th, likes it, then leaves 1 comment that fits the image (e.g. a game montage
-> a comment about that game).

Usage:
    python auto_like_comment.py                # target = today
    python auto_like_comment.py --date 2026-09-17
"""

import argparse
import base64
import json
import os
import random
import time
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from groq import Groq
from instagrapi import Client
from instagrapi.exceptions import ClientError, FeedbackRequired, PleaseWaitFewMinutes

# Credentials are read from environment variables (don't hardcode secrets in source).
#   export IG_PASSWORD="..."   export GROQ_API_KEY="..."
IG_USERNAME = os.environ.get("IG_USERNAME", "feliciaaa_kim")   # the account that LOGS IN (sends the like/comment)
IG_PASSWORD = os.environ.get("IG_PASSWORD", "admin.admin")
TARGET_USERNAME = "neerdygeeks"     # the account whose posts get liked & commented on (can differ from IG_USERNAME)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
SESSION_FILE = "session.json"
STATE_FILE = "auto_like_comment_state.json"   # tracks media/comments already processed
LOCAL_TZ = ZoneInfo("Asia/Jakarta")           # dates are still resolved in Indonesian time
VISION_MODEL = "qwen/qwen3.8-27b"
TEXT_MODEL = "openai/gpt-oss-20b"
TMP_IMAGE_PATH = "tmp_post_image.jpg"
REPLY_POLL_RANGE = (30, 90)  # seconds, random pause between checks for new replies

PERSONA = """
You are Felicia — 29 years old, warm, a little playful, and you talk like a real
person typing a chat/comment, not like an assistant.

Language: always write in English.

Personality:
- Easygoing, sometimes jokes around or teases, but can be serious when the topic calls for it
- If the other person is trolling or joking about something, she plays along
  instead of getting defensive or explaining at length
- If asked something genuinely informative/technical, she answers honestly
  and briefly — still casual, but not making stuff up
- Confident, not easily offended, but still warm
- Hates stiff/formal replies that sound like customer service
- MBTI type ENFJ (Extroverted, Intuitive, Feeling, Judge)

Style:
- Short, usually 1 sentence
- Casual, everyday English, not textbook
- Never uses markdown or hashtags
- Match the tone: if the person is joking -> joke back; if the person wants a
  serious/detailed discussion -> reply a bit more informatively, but still casual
- Knows how to talk with an ISTP like user you want to talk to
"""

groq_client = Groq(api_key=GROQ_API_KEY)


# ---------- state (so we don't comment twice on the same post) ----------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("media", {})  # in case an old file doesn't have this key
    return state


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------- login ----------

def login():
    cl = Client()
    try:
        cl.load_settings(SESSION_FILE)
        cl.login(IG_USERNAME, IG_PASSWORD)
    except Exception as e:
        print(f"Session reuse failed ({e}), doing fresh login...")
        cl = Client()
        cl.login(IG_USERNAME, IG_PASSWORD)

    try:
        cl.get_timeline_feed()
    except Exception as e:
        print(f"Warning: timeline check failed ({e}), continuing anyway.")

    cl.dump_settings(SESSION_FILE)
    return cl


# ---------- find the first post on the target date ----------

def get_first_post_of_date(cl, target_user_id, target_date, amount=50):
    medias = cl.user_medias(target_user_id, amount=amount)
    print(f"Fetched {len(medias)} media from the target account.")

    matching = []
    for m in medias:
        taken_local = m.taken_at.astimezone(LOCAL_TZ).date()
        if taken_local == target_date:
            matching.append(m)

    if not matching:
        return None

    # pick the earliest (first) post on that date
    matching.sort(key=lambda m: m.taken_at)
    return matching[0]


# ---------- grab the post image for analysis ----------

def download_media_image(cl, media):
    # For photos & carousels, take the first photo. For video/reels, take the thumbnail.
    if media.media_type == 2:  # video/reels
        url = media.thumbnail_url
    elif media.media_type == 8 and media.resources:  # carousel
        url = media.resources[0].thumbnail_url
    else:  # regular photo
        url = media.thumbnail_url

    path = cl.photo_download_by_url(url, filename=TMP_IMAGE_PATH)
    return str(path)


def encode_image_base64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


# ---------- generate a contextual comment with the vision model ----------

def generate_comment(image_path, caption):
    base64_image = encode_image_base64(image_path)

    system_prompt = """
You write ONE natural Instagram comment in English, short (max 1 short sentence),
based on the image and caption of the post below. Don't use stale templates like
just "so cool!" or "awesome!" — it must be specific and connect to what's in the
image/caption (for example if it's a game montage, mention something from that
game; if it's a food photo, mention the food). Casual language, like a real
person commenting on IG, not formal/AI-sounding. No hashtags. No more than 1
emoji. Output ONLY the comment text, with no quotation marks and nothing else.
"""

    resp = groq_client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Post caption: {caption or '(no caption)'}"
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                    }
                ]
            }
        ],
        temperature=1,
        max_completion_tokens=100,
    )
    return resp.choices[0].message.content.strip()


# ---------- like + comment, with error handling ----------

def like_media_if_needed(cl, media):
    # check the like status first so we don't fire repeated likes
    try:
        fresh = cl.media_info(media.pk)
        already_liked = getattr(fresh, "has_liked", False)
    except Exception as e:
        print(f"Couldn't check like status ({e}), trying to like directly.")
        already_liked = False

    if already_liked:
        print(f"Media {media.pk} was already liked, skipping like.")
        return

    try:
        cl.media_like(media.pk)
        print(f"Liked media {media.pk}")
    except Exception as e:
        print(f"Failed to like: {e}")


# ---------- post the initial comment ----------

def post_comment(cl, media, comment_text):
    try:
        comment = cl.media_comment(media.pk, comment_text)
        print(f"Commented on media {media.pk}: {comment_text}")
        return comment
    except PleaseWaitFewMinutes as e:
        print(f"Rate limited: {e}")
    except FeedbackRequired as e:
        print(f"Hit IG spam filter: {e}")
    except ClientError as e:
        print(f"Client error while commenting: {e}")
    return None


# ---------- generate a reply to someone's reply (text only, no vision needed) ----------

def generate_reply_to_comment(original_caption, our_comment_text, their_reply_text):
    system_prompt = PERSONA + """

Context: you're replying to someone's reply in the comment section of your own
Instagram post. Reply with ONE short sentence only. No hashtags.
"""
    context = (
        f"Post caption: {original_caption or '(no caption)'}\n"
        f"Your original comment: {our_comment_text}\n"
        f"Their reply to your comment: {their_reply_text}"
    )

    resp = groq_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": context},
        ],
        temperature=0.9,
        max_tokens=400,
        reasoning_effort="low",
    )
    return (resp.choices[0].message.content or "").strip()


# ---------- loop: read new replies to our comment, then respond ----------

def watch_and_reply_to_comment_thread(cl, media, our_comment):
    """
    Polling loop: check for new replies to `our_comment` on this media,
    then answer them one by one without needing to unlike/re-comment from scratch.
    Runs until stopped manually (Ctrl+C).
    """
    state = load_state()
    media_key = str(media.pk)
    replied_ids = set(state["media"].get(media_key, {}).get("replied_comment_ids", []))

    print(f"Started watching replies to comment {our_comment.pk} on media {media.pk}...")

    while True:
        try:
            replies = cl.media_comment_replies(media.pk, our_comment.pk)
            for r in replies:
                if str(r.pk) in replied_ids:
                    continue
                if str(r.user.pk) == str(cl.user_id):
                    continue  # skip replies from ourselves

                reply_text = generate_reply_to_comment(
                    media.caption_text, our_comment.text, r.text
                )

                if not reply_text:
                    print(f"Model returned an empty reply for {r.pk}, skipping for now (will retry later).")
                    continue

                # human-like pause before replying, so it isn't instant/robotic
                time.sleep(random.uniform(15, 60))

                try:
                    posted = cl.media_comment(media.pk, reply_text, replied_to_comment_id=our_comment.pk)
                    print(f"Replied to {r.pk}: {reply_text}")
                    # mark our own reply as "already processed" too,
                    # so it isn't detected as a new reply on the next cycle
                    if posted and getattr(posted, "pk", None):
                        replied_ids.add(str(posted.pk))
                except PleaseWaitFewMinutes as e:
                    print(f"Rate limited: {e}")
                    time.sleep(300)
                    continue
                except FeedbackRequired as e:
                    print(f"Hit IG spam filter: {e}")
                    time.sleep(600)
                    continue
                except ClientError as e:
                    print(f"Client error while replying: {e}")
                    continue

                replied_ids.add(str(r.pk))
                state["media"].setdefault(media_key, {})["replied_comment_ids"] = list(replied_ids)
                save_state(state)

        except Exception as e:
            print(f"Error while polling replies: {e}")

        time.sleep(random.uniform(*REPLY_POLL_RANGE))


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description="Auto like + 1 contextual comment, then watch & reply to replies on that comment.")
    parser.add_argument("--date", type=str, default=None, help="Format YYYY-MM-DD. Default: today.")
    parser.add_argument("--media-pk", type=str, default=None,
                         help="Jump straight to this media (skip the date search) — use when the media was already processed before.")
    parser.add_argument("--continue", dest="continue_last", action="store_true",
                         help="Continue the conversation on the LAST processed media (no need to know the media_pk).")
    parser.add_argument("--no-watch", action="store_true", help="Skip reply-watching mode, just like + comment once and exit.")
    args = parser.parse_args()

    state = load_state()
    cl = login()

    if args.continue_last:
        if not state["media"]:
            print("No media has been processed before, nothing to continue.")
            return
        last_media_pk = list(state["media"].keys())[-1]
        media = cl.media_info(last_media_pk)
        print(f"Continuing the conversation on media {last_media_pk}.")
    elif args.media_pk:
        media = cl.media_info(args.media_pk)
    else:
        if state["media"] and not args.date:
            print("NOTE: there's already an active conversation in state (another media). "
                  "If you meant to keep going with that one, use --continue instead of searching for a new post.")

        target_date = (
            datetime.strptime(args.date, "%Y-%m-%d").date()
            if args.date else datetime.now(LOCAL_TZ).date()
        )
        target_user_id = cl.user_id_from_username(TARGET_USERNAME)
        media = get_first_post_of_date(cl, target_user_id, target_date)
        if media is None:
            print(f"No post found for {target_date}.")
            print("If this media was already processed before, try running again with --media-pk <pk> (see auto_like_comment_state.json).")
            return

    media_key = str(media.pk)
    media_state = state["media"].get(media_key)

    like_media_if_needed(cl, media)

    if media_state and media_state.get("comment_pk"):
        # already commented before — don't comment again, use the saved data
        print(f"Media {media.pk} was already commented on, moving on to reply-watching mode.")
        comment_text = media_state.get("comment_text", "")

        if not comment_text:
            # old entry from before comment_text was saved — look it up manually
            for c in cl.media_comments(media.pk):
                if str(c.pk) == str(media_state["comment_pk"]):
                    comment_text = c.text
                    break
            media_state["comment_text"] = comment_text
            state["media"][media_key] = media_state
            save_state(state)

        our_comment = SimpleNamespace(pk=media_state["comment_pk"], text=comment_text)
    else:
        image_path = download_media_image(cl, media)
        comment_text = generate_comment(image_path, media.caption_text)
        our_comment = post_comment(cl, media, comment_text)
        if os.path.exists(image_path):
            os.remove(image_path)

        if our_comment is None:
            print("Failed to post the initial comment, stopping.")
            return

        state["media"][media_key] = {
            "comment_pk": our_comment.pk,
            "comment_text": our_comment.text,
            "replied_comment_ids": [],
        }
        save_state(state)

    if not args.no_watch:
        watch_and_reply_to_comment_thread(cl, media, our_comment)


if __name__ == "__main__":
    main()