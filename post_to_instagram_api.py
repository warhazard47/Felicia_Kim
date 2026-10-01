"""
post_to_instagram_api.py

Post to Instagram through the OFFICIAL Instagram API (Instagram Login), now or on a schedule.
Replaces the instagrapi version (post_to_instagram.py).

Flow for each post:
  1. Pick the first file in posts/ (jpg, jpeg, png, mp4, mov), name order.
  2. Generate the caption with Groq (vision model for photos, text model for videos).
  3. Upload the file to Cloudinary. Instagram only accepts media from a public HTTPS URL.
  4. Create an Instagram media container from that URL, wait until it's ready, publish it.
  5. Delete the temporary Cloudinary copy and move the file to posted/.

Videos are published as Reels (Instagram publishes all feed videos as Reels).
Schedule times are Asia/Jakarta (WIB). Scheduling = this process stays running until the time.

Environment variables (set them in the terminal, never paste them in chat):
    IG_ACCESS_TOKEN           token from the Meta dashboard (valid ~60 days)
    IG_USER_ID                defaults to 17841426851573335 (feliciaaa_kim)
    GROQ_API_KEY
    CLOUDINARY_CLOUD_NAME  CLOUDINARY_API_KEY  CLOUDINARY_API_SECRET

Usage:
    python post_to_instagram_api.py --check                 # test the token, print the account name
    python post_to_instagram_api.py --dry-run               # caption only, posts nothing (no token needed)
    python post_to_instagram_api.py --hint "Erwin Cup 2026" # post the next file now
    python post_to_instagram_api.py --at "2026-10-02 19:30" # post once at that WIB time
    python post_to_instagram_api.py --daily 19:30 --jitter 10
    python post_to_instagram_api.py --caption "my own text" # skip the AI caption
    python post_to_instagram_api.py --keep-hosted           # don't delete the Cloudinary copy
"""

import argparse
import base64
import hashlib
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from groq import Groq
from PIL import Image

import shared_context   # keep shared_context.py in the same folder

IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER_ID = os.environ.get("IG_USER_ID", "17841426851573335")
GRAPH_BASE = os.environ.get("IG_GRAPH_BASE", "https://graph.instagram.com")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUD_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUD_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")

POST_DIR = Path("posts")
POSTED_DIR = Path("posted")
TMP_JPG_PATH = Path("tmp_upload.jpg")
LOCAL_TZ = ZoneInfo("Asia/Jakarta")

VISION_MODEL = "qwen/qwen3.8-27b"     # same vision model as the other scripts (name unverified)
TEXT_MODEL = "openai/gpt-oss-20b"     # used for videos (no frame analysis)

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTS = {".mp4", ".mov"}
CONTAINER_TIMEOUT = 600   # seconds to wait for Instagram to process a container
MAX_WIDTH = 1440          # downscale larger photos before uploading

CAPTION_SYSTEM_PROMPT = """
You write Instagram captions in English for the account owner.

Rules:
- Base the caption on what's actually in the media (and the hint, if given). Be specific, not generic.
- 1 to 3 short sentences, casual and natural, like a real person posting, not marketing copy.
- Warm and a little playful. No cringe influencer phrases, no "Hey guys!".
- At most 2 emojis in total.
- {hashtag_rule}
- Output ONLY the caption text: no quotation marks, no markdown, no preface like "Here's a caption".
"""


# ---------- helpers ----------

def natural_key(path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


def pick_next_media():
    POST_DIR.mkdir(exist_ok=True)
    files = [p for p in POST_DIR.iterdir()
             if p.is_file() and p.suffix.lower() in IMAGE_EXTS | VIDEO_EXTS]
    if not files:
        return None
    files.sort(key=natural_key)
    return files[0]


def strip_reasoning(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    return text.strip().strip('"').strip()


def to_jpeg(src_path):
    """Instagram image posts must be JPEG. Convert (also handles PNG)."""
    with Image.open(src_path) as im:
        im = im.convert("RGB")
        if im.width > MAX_WIDTH:   # Instagram doesn't use more than 1440 px; a smaller file uploads more reliably
            im = im.resize((MAX_WIDTH, round(im.height * MAX_WIDTH / im.width)), Image.Resampling.LANCZOS)
        im.save(TMP_JPG_PATH, "JPEG", quality=92)
    return TMP_JPG_PATH


# ---------- caption generation ----------

def generate_caption(groq_client, media_path, hint=None, hashtags=True):
    hashtag_rule = ("Add 3 to 5 relevant hashtags on a new line at the end."
                    if hashtags else "Do not use hashtags.")
    system_prompt = CAPTION_SYSTEM_PROMPT.format(hashtag_rule=hashtag_rule)
    hint_text = f"Extra context from the account owner: {hint}" if hint else "No extra context given."

    if media_path.suffix.lower() in IMAGE_EXTS:
        jpg_path = to_jpeg(media_path)
        with open(jpg_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        resp = groq_client.chat.completions.create(
            model=VISION_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": f"Write a caption for this photo. {hint_text}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]},
            ],
            temperature=0.9,
            max_completion_tokens=250,
        )
    else:
        resp = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": (
                    f"Write a caption for a short video. The file is named '{media_path.stem}'. {hint_text}")},
            ],
            temperature=0.9,
            max_tokens=500,
            reasoning_effort="low",
        )
    caption = strip_reasoning(resp.choices[0].message.content)
    return caption[:2200]   # Instagram's caption limit


# ---------- Cloudinary (temporary public hosting) ----------

def cloudinary_upload(file_path, resource_type, attempts=3):
    """Upload a file, return (public https url, public_id). Retries on network errors."""
    url = f"https://api.cloudinary.com/v1_1/{CLOUD_NAME}/{resource_type}/upload"
    for attempt in range(1, attempts + 1):
        ts = str(int(time.time()))
        signature = hashlib.sha1(f"timestamp={ts}{CLOUD_SECRET}".encode()).hexdigest()
        try:
            with open(file_path, "rb") as f:
                r = requests.post(
                    url,
                    data={"api_key": CLOUD_KEY, "timestamp": ts, "signature": signature},
                    files={"file": f},
                    timeout=600,
                )
        except requests.exceptions.RequestException as e:
            if attempt == attempts:
                raise RuntimeError(f"Cloudinary upload failed after {attempts} attempts (network error): {e}")
            wait = 3 * attempt
            print(f"  network error during upload ({e.__class__.__name__}), retrying in {wait}s "
                  f"(attempt {attempt}/{attempts})...")
            time.sleep(wait)
            continue
        if not r.ok:
            raise RuntimeError(f"Cloudinary upload failed ({r.status_code}): {r.text[:300]}")
        data = r.json()
        return data["secure_url"], data["public_id"]


def cloudinary_delete(public_id, resource_type):
    ts = str(int(time.time()))
    signature = hashlib.sha1(f"public_id={public_id}&timestamp={ts}{CLOUD_SECRET}".encode()).hexdigest()
    r = requests.post(
        f"https://api.cloudinary.com/v1_1/{CLOUD_NAME}/{resource_type}/destroy",
        data={"public_id": public_id, "api_key": CLOUD_KEY, "timestamp": ts, "signature": signature},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"Cloudinary delete failed ({r.status_code}): {r.text[:200]}")


# ---------- Instagram API ----------

def _headers():
    return {"Authorization": f"Bearer {IG_ACCESS_TOKEN}"}


def _check(resp):
    """Return parsed JSON, or raise a readable error from Instagram's error payload."""
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not resp.ok or "error" in data:
        err = data.get("error", {})
        raise RuntimeError(
            f"Instagram API error (HTTP {resp.status_code}, code {err.get('code')}): "
            f"{err.get('message', resp.text[:300])}"
        )
    return data


def ig_get(path, params=None):
    return _check(requests.get(f"{GRAPH_BASE}/{path}", params=params, headers=_headers(), timeout=60))


def ig_post(path, data):
    return _check(requests.post(f"{GRAPH_BASE}/{path}", data=data, headers=_headers(), timeout=120))


def create_container(kind, media_url, caption):
    data = {"caption": caption}
    if kind == "image":
        data["image_url"] = media_url
    else:
        data["media_type"] = "REELS"
        data["video_url"] = media_url
    return ig_post(f"{IG_USER_ID}/media", data)["id"]


def wait_until_ready(container_id):
    """Poll until Instagram finishes processing the container."""
    deadline = time.time() + CONTAINER_TIMEOUT
    while time.time() < deadline:
        status = ig_get(container_id, {"fields": "status_code"}).get("status_code")
        print(f"  container status: {status}")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the media (status {status}).")
        time.sleep(5)
    raise RuntimeError("Timed out waiting for Instagram to process the media.")


def publish_container(container_id):
    return ig_post(f"{IG_USER_ID}/media_publish", {"creation_id": container_id})["id"]


def check_token():
    me = ig_get("me", {"fields": "user_id,username"})
    print(f"Token OK. Account: @{me.get('username')} (user_id {me.get('user_id')})")
    if str(me.get("user_id")) != str(IG_USER_ID):
        print(f"WARNING: IG_USER_ID is {IG_USER_ID} but the token belongs to {me.get('user_id')}.")


# ---------- posting ----------

def archive(media_path):
    POSTED_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M%S")
    dest = POSTED_DIR / f"{stamp}_{media_path.name}"
    shutil.move(str(media_path), str(dest))
    return dest


def missing_settings():
    needed = {
        "IG_ACCESS_TOKEN": IG_ACCESS_TOKEN,
        "CLOUDINARY_CLOUD_NAME": CLOUD_NAME,
        "CLOUDINARY_API_KEY": CLOUD_KEY,
        "CLOUDINARY_API_SECRET": CLOUD_SECRET,
    }
    return [k for k, v in needed.items() if not v]


def post_next(args):
    media_path = pick_next_media()
    if media_path is None:
        print(f"No media files found in '{POST_DIR}/'. Nothing to post.")
        return False
    print(f"Selected media: {media_path}")

    if args.caption:
        caption = args.caption
    else:
        try:
            caption = generate_caption(Groq(api_key=GROQ_API_KEY), media_path,
                                       hint=args.hint, hashtags=not args.no_hashtags)
        except Exception as e:
            print(f"Caption generation failed ({e}). Not posting.")
            return False
        if not caption:
            print("Model returned an empty caption. Not posting.")
            return False

    print(f"\nCaption:\n{caption}\n")

    if args.dry_run:
        print("Dry run: nothing was uploaded or posted.")
        return True

    missing = missing_settings()
    if missing:
        print("Missing environment variables: " + ", ".join(missing))
        return False

    kind = "image" if media_path.suffix.lower() in IMAGE_EXTS else "video"
    public_id = None
    try:
        upload_path = to_jpeg(media_path) if kind == "image" else media_path
        print("Uploading to Cloudinary...")
        url, public_id = cloudinary_upload(upload_path, kind)

        print("Creating Instagram media container...")
        container_id = create_container(kind, url, caption)
        wait_until_ready(container_id)

        print("Publishing...")
        media_id = publish_container(container_id)
    except Exception as e:
        print(f"Posting failed: {e}")
        print("The file was left in place so it can be retried.")
        return False
    finally:
        if TMP_JPG_PATH.exists():
            TMP_JPG_PATH.unlink()

    print(f"Posted successfully (media id: {media_id}).")

    try:
        shared_context.add_post(caption, args.hint, media_id)
    except Exception as e:
        print(f"Couldn't save post context ({e}), continuing.")

    if public_id and not args.keep_hosted:
        try:
            cloudinary_delete(public_id, kind)
            print("Temporary Cloudinary copy deleted.")
        except Exception as e:
            print(f"Couldn't delete the Cloudinary copy ({e}); you can remove it manually.")

    print(f"Moved to {archive(media_path)}")
    return True


# ---------- scheduling ----------

def parse_at(value):
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError:
        sys.exit('--at must look like "2026-10-01 19:30" (24h, Asia/Jakarta time).')
    return dt.replace(tzinfo=LOCAL_TZ)


def next_daily_occurrence(hhmm, jitter_minutes=0):
    try:
        hour, minute = map(int, hhmm.split(":"))
        assert 0 <= hour < 24 and 0 <= minute < 60
    except Exception:
        sys.exit("--daily must look like HH:MM (24h), e.g. 19:30")

    now = datetime.now(LOCAL_TZ)
    base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    target = base + timedelta(minutes=random.uniform(-jitter_minutes, jitter_minutes)) if jitter_minutes else base
    if target <= now:
        base += timedelta(days=1)
        target = base + timedelta(minutes=random.uniform(-jitter_minutes, jitter_minutes)) if jitter_minutes else base
    return target


def wait_until(target):
    print(f"Waiting until {target.strftime('%Y-%m-%d %H:%M:%S %Z')} ... (Ctrl+C to cancel)")
    while True:
        remaining = (target - datetime.now(LOCAL_TZ)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60))


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description="Post to Instagram (official API) now or on a schedule.")
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--at", type=str, help='One-off schedule, "YYYY-MM-DD HH:MM" in Asia/Jakarta time.')
    when.add_argument("--daily", type=str, help="Post one file every day at HH:MM (Asia/Jakarta) until the folder is empty.")
    parser.add_argument("--jitter", type=int, default=0, help="With --daily: randomize the time by +/- N minutes.")
    parser.add_argument("--hint", type=str, default=None, help="Extra context for the caption model.")
    parser.add_argument("--caption", type=str, default=None, help="Use this caption instead of generating one.")
    parser.add_argument("--no-hashtags", action="store_true", help="Generate captions without hashtags.")
    parser.add_argument("--keep-hosted", action="store_true", help="Keep the temporary Cloudinary copy after posting.")
    parser.add_argument("--dry-run", action="store_true", help="Generate and print the caption only.")
    parser.add_argument("--check", action="store_true", help="Test the access token and exit.")
    args = parser.parse_args()

    if args.check:
        if not IG_ACCESS_TOKEN:
            sys.exit("Set IG_ACCESS_TOKEN first.")
        try:
            check_token()
        except Exception as e:
            sys.exit(f"Token check failed: {e}")
        return

    if not args.caption and not GROQ_API_KEY:
        sys.exit("Set the GROQ_API_KEY environment variable first (or pass --caption).")
    if not args.dry_run:
        missing = missing_settings()
        if missing:
            sys.exit("Set these environment variables first: " + ", ".join(missing))

    if pick_next_media() is None:
        sys.exit(f"No media files found in '{POST_DIR}/'. Add a jpg/png/mp4 there first.")

    if args.daily:
        while True:
            wait_until(next_daily_occurrence(args.daily, args.jitter))
            ok = post_next(args)
            if pick_next_media() is None:
                print("Folder is empty, stopping the daily schedule.")
                break
            if not ok:
                print("Post failed; will try again at the next scheduled time.")
    elif args.at:
        target = parse_at(args.at)
        if target <= datetime.now(LOCAL_TZ):
            sys.exit("That time is already in the past (Asia/Jakarta time).")
        wait_until(target)
        post_next(args)
    else:
        post_next(args)


if __name__ == "__main__":
    main()