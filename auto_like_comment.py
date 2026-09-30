"""
auto_like_comment.py

Auto-like + auto-comment (SEKALI saja) pada postingan Instagram milik akun
sendiri, berdasarkan tanggal. Komentar dibuat kontekstual berdasarkan gambar
+ caption postingan (pakai model vision di Groq), bukan template berulang.

Contoh: jalankan script tgl 17 -> dia cari postingan PERTAMA yang di-post
tanggal 17, like, lalu komen 1x sesuai konteks gambar (mis. montage game ->
komen soal game itu).

Usage:
    python auto_like_comment.py                # target = hari ini
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

IG_USERNAME = "feliciaaa_kim"       # akun yang LOGIN (yang ngirim like/comment)
IG_PASSWORD = "admin.admin"
TARGET_USERNAME = "neerdygeeks"     # akun yang mau di-like & di-komen (bisa beda dari IG_USERNAME)
GROQ_API_KEY = "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"
SESSION_FILE = "session.json"
STATE_FILE = "auto_like_comment_state.json"   # tracks media/comment yang udah diproses
LOCAL_TZ = ZoneInfo("Asia/Jakarta")
VISION_MODEL = "qwen/qwen3.8-27b"
TEXT_MODEL = "openai/gpt-oss-20b"
TMP_IMAGE_PATH = "tmp_post_image.jpg"
REPLY_POLL_RANGE = (30, 90)  # detik, jeda acak antar cek reply baru

PERSONA = """
Kamu adalah Felicia — 29 tahun, hangat, sedikit playful, ngomong kayak orang beneran
ngetik chat/komen, bukan asisten.

Kepribadian:
- Santai, kadang bercanda/menggoda, tapi bisa serius kalau topiknya emang butuh itu
- Kalau lawan bicara nge-troll/bercanda soal sesuatu, dia ikutan main-main balik,
  bukan defensif atau menjelaskan panjang lebar
- Kalau ditanya sesuatu yang genuinely informatif/teknis, dia jawab dengan jujur
  dan singkat, tetep santai tapi gak ngasal
- Percaya diri, gak baperan, tapi tetep hangat
- Gak suka jawaban yang kaku/formal kayak customer service

Gaya:
- Pendek, 1 kalimat biasanya
- Bahasa gaul/casual Indonesia, bukan textbook
- Gak pernah pakai markdown atau hashtag
- Sesuaikan nada: kalau orangnya bercanda -> balas becanda; kalau orangnya
  ngajak diskusi serius/detail -> balas lebih informatif dikit, tapi tetep santai
"""

groq_client = Groq(api_key=GROQ_API_KEY)


# ---------- state (biar gak komen dobel di post yang sama) ----------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("media", {})  # jaga-jaga file lama gak punya key ini
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


# ---------- cari postingan pertama di tanggal target ----------

def get_first_post_of_date(cl, target_user_id, target_date, amount=50):
    medias = cl.user_medias(target_user_id, amount=amount)
    print(f"Fetched {len(medias)} media dari akun target.")

    matching = []
    for m in medias:
        taken_local = m.taken_at.astimezone(LOCAL_TZ).date()
        if taken_local == target_date:
            matching.append(m)

    if not matching:
        return None

    # ambil yang paling awal (pertama) di tanggal itu
    matching.sort(key=lambda m: m.taken_at)
    return matching[0]


# ---------- ambil gambar postingan buat dianalisis ----------

def download_media_image(cl, media):
    # Untuk foto & carousel, ambil foto pertama. Untuk video/reels, ambil thumbnail.
    if media.media_type == 2:  # video/reels
        url = media.thumbnail_url
    elif media.media_type == 8 and media.resources:  # carousel
        url = media.resources[0].thumbnail_url
    else:  # foto biasa
        url = media.thumbnail_url

    path = cl.photo_download_by_url(url, filename=TMP_IMAGE_PATH)
    return str(path)


def encode_image_base64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


# ---------- generate komentar kontekstual pakai vision model ----------

def generate_comment(image_path, caption):
    base64_image = encode_image_base64(image_path)

    system_prompt = """
Kamu bikin SATU komentar Instagram yang natural, singkat (maks 1 kalimat pendek),
berdasarkan isi gambar dan caption postingan berikut. Jangan pakai template basi
kayak "keren banget!" atau "mantap!" doang — harus spesifik nyambung ke apa yang
ada di gambar/caption (misalnya kalau itu montage game, sebut elemen dari game-nya;
kalau itu foto makanan, sebut makanannya). Gaya bahasa casual, kayak orang beneran
komen di IG, bukan bahasa formal/AI. Jangan pakai hashtag. Jangan pakai emoji lebih
dari 1. Output HANYA teks komentarnya, tanpa tanda kutip, tanpa embel-embel lain.
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
                        "text": f"Caption postingan: {caption or '(tidak ada caption)'}"
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


# ---------- like + comment, dengan error handling ----------

def like_media_if_needed(cl, media):
    # baca dulu status like-nya sebelum like, biar gak nembak like berulang
    try:
        fresh = cl.media_info(media.pk)
        already_liked = getattr(fresh, "has_liked", False)
    except Exception as e:
        print(f"Gagal cek status like ({e}), lanjut coba like langsung.")
        already_liked = False

    if already_liked:
        print(f"Media {media.pk} udah di-like sebelumnya, skip like.")
        return

    try:
        cl.media_like(media.pk)
        print(f"Liked media {media.pk}")
    except Exception as e:
        print(f"Gagal like: {e}")


# ---------- post komentar (awal) ----------

def post_comment(cl, media, comment_text):
    try:
        comment = cl.media_comment(media.pk, comment_text)
        print(f"Commented on media {media.pk}: {comment_text}")
        return comment
    except PleaseWaitFewMinutes as e:
        print(f"Rate limited: {e}")
    except FeedbackRequired as e:
        print(f"Kena spam filter IG: {e}")
    except ClientError as e:
        print(f"Client error saat komen: {e}")
    return None


# ---------- generate balasan ke reply orang (teks aja, gak perlu vision lagi) ----------

def generate_reply_to_comment(original_caption, our_comment_text, their_reply_text):
    system_prompt = PERSONA + """

Konteks: kamu lagi bales reply orang di kolom komentar postingan Instagram
kamu sendiri. Balas SATU kalimat pendek aja. Jangan pakai hashtag.
"""
    context = (
        f"Caption postingan: {original_caption or '(tidak ada caption)'}\n"
        f"Komentar awal kamu: {our_comment_text}\n"
        f"Reply orang ke komentar kamu: {their_reply_text}"
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


# ---------- loop: baca reply baru ke komentar kita, terus bales ----------

def watch_and_reply_to_comment_thread(cl, media, our_comment):
    """
    Polling loop: cek reply-reply baru ke `our_comment` di media ini,
    lalu balas satu-satu tanpa perlu unlike/re-comment dari awal.
    Jalan terus sampai di-stop manual (Ctrl+C).
    """
    state = load_state()
    media_key = str(media.pk)
    replied_ids = set(state["media"].get(media_key, {}).get("replied_comment_ids", []))

    print(f"Mulai memantau reply ke komentar {our_comment.pk} di media {media.pk}...")

    while True:
        try:
            replies = cl.media_comment_replies(media.pk, our_comment.pk)
            for r in replies:
                if str(r.pk) in replied_ids:
                    continue
                if str(r.user.pk) == str(cl.user_id):
                    continue  # skip reply dari diri sendiri

                reply_text = generate_reply_to_comment(
                    media.caption_text, our_comment.text, r.text
                )

                if not reply_text:
                    print(f"Balasan kosong dari model buat reply {r.pk}, skip dulu (dicoba lagi nanti).")
                    continue

                # jeda manusiawi sebelum bales, biar gak instant/robotik
                time.sleep(random.uniform(15, 60))

                try:
                    posted = cl.media_comment(media.pk, reply_text, replied_to_comment_id=our_comment.pk)
                    print(f"Balas reply {r.pk}: {reply_text}")
                    # tandai balasan kita sendiri sebagai "udah diproses" juga,
                    # biar gak ke-detect lagi jadi reply baru di siklus berikutnya
                    if posted and getattr(posted, "pk", None):
                        replied_ids.add(str(posted.pk))
                except PleaseWaitFewMinutes as e:
                    print(f"Rate limited: {e}")
                    time.sleep(300)
                    continue
                except FeedbackRequired as e:
                    print(f"Kena spam filter IG: {e}")
                    time.sleep(600)
                    continue
                except ClientError as e:
                    print(f"Client error saat balas reply: {e}")
                    continue

                replied_ids.add(str(r.pk))
                state["media"].setdefault(media_key, {})["replied_comment_ids"] = list(replied_ids)
                save_state(state)

        except Exception as e:
            print(f"Error saat polling reply: {e}")

        time.sleep(random.uniform(*REPLY_POLL_RANGE))


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description="Auto like + 1 komen kontekstual, lalu pantau & balas reply ke komentar itu.")
    parser.add_argument("--date", type=str, default=None, help="Format YYYY-MM-DD. Default: hari ini.")
    parser.add_argument("--media-pk", type=str, default=None,
                         help="Langsung lanjut ke media ini (skip pencarian by date) — dipakai kalau media udah pernah diproses sebelumnya.")
    parser.add_argument("--continue", dest="continue_last", action="store_true",
                         help="Lanjutin percakapan di media TERAKHIR yang udah diproses (gak perlu tau media_pk manual).")
    parser.add_argument("--no-watch", action="store_true", help="Skip mode pantau reply, cuma like+komen sekali lalu keluar.")
    args = parser.parse_args()

    state = load_state()
    cl = login()

    if args.continue_last:
        if not state["media"]:
            print("Belum ada media yang pernah diproses sebelumnya, gak ada yang bisa di-continue.")
            return
        last_media_pk = list(state["media"].keys())[-1]
        media = cl.media_info(last_media_pk)
        print(f"Melanjutkan percakapan di media {last_media_pk}.")
    elif args.media_pk:
        media = cl.media_info(args.media_pk)
    else:
        if state["media"] and not args.date:
            print("PERHATIAN: udah ada percakapan aktif di state (media lain). "
                  "Kalau maksudnya mau nerusin obrolan itu, pakai --continue, bukan cari postingan baru.")

        target_date = (
            datetime.strptime(args.date, "%Y-%m-%d").date()
            if args.date else datetime.now(LOCAL_TZ).date()
        )
        target_user_id = cl.user_id_from_username(TARGET_USERNAME)
        media = get_first_post_of_date(cl, target_user_id, target_date)
        if media is None:
            print(f"Gak ada postingan ditemukan untuk tanggal {target_date}.")
            print("Kalau media ini udah pernah diproses sebelumnya, coba jalankan lagi pakai --media-pk <pk> (lihat auto_like_comment_state.json).")
            return

    media_key = str(media.pk)
    media_state = state["media"].get(media_key)

    like_media_if_needed(cl, media)

    if media_state and media_state.get("comment_pk"):
        # udah pernah komen sebelumnya — jangan komen ulang, pakai data yang udah disimpan
        print(f"Media {media.pk} udah pernah dikomen sebelumnya, lanjut ke mode pantau reply.")
        comment_text = media_state.get("comment_text", "")

        if not comment_text:
            # entry lama dari sebelum comment_text disimpan — cari manual
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
            print("Gagal posting komentar awal, berhenti.")
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