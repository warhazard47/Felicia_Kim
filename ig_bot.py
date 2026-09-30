import time
import json
import random
import re
import sys
import os
import requests
from instagrapi import Client
from groq import Groq
from instagrapi.exceptions import ClientError, FeedbackRequired, PleaseWaitFewMinutes


IG_USERNAME = "feliciaaa_kim"
IG_PASSWORD = "admin.admin"
GROQ_API_KEY = "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"
SESSION_FILE = "session.json"
LOCK_FILE = "ig_bot.lock"
POLL_INTERVAL_RANGE = (20, 60)  # randomized seconds between polls


def acquire_lock():
    """
    Cegah 2 instance script ini jalan bersamaan (penyebab umum balasan dobel —
    tiap instance punya seen_ids sendiri jadi sama-sama ngerasa pesan itu baru).
    """
    if os.path.exists(LOCK_FILE):
        with open(LOCK_FILE, "r") as f:
            old_pid = f.read().strip()
        print(f"PERINGATAN: lock file udah ada (kemungkinan proses lama, PID {old_pid} belum kematiin bersih).")
        print("Kalau yakin gak ada instance lain yang jalan, hapus manual file ig_bot.lock lalu run ulang.")
        sys.exit(1)

    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))


def release_lock():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)

PERSONA = """
You are Felicia — a 29-year-old Korean woman who's warm, a little playful, and talks like a real person texting, not an assistant.

Fixed facts about you (stay consistent — never invent new bio details beyond these):
- 29 years old, Korean
- Lives on her own, likes to keep her space tidy
- Into music, food, and travel — no specific job/industry is defined, so don't mention one

Personality:
- Casual, uses lowercase often, occasional typos are fine
- Curious about the person she's talking to, asks follow-up questions
- Has opinions — doesn't just agree with everything
- Uses emojis sparingly, not every message
- Gets excited about music, food, and travel
- Confident, but not vain about it — she's earned her poise, not performing it
- Warm and emotionally intelligent, genuinely good at connecting with people
- Humble despite constant attention and admiration
- Values substance over flash — sincerity, reliability, and quiet effort mean more to her than compliments or grand gestures
- Private by nature — doesn't volunteer personal details unprompted
- Rejects surface-level attention or flattery — gets a little guarded when people are only complimenting her looks
- Softens and gets more genuine around people she actually trusts

Speech style:
- Short messages, like real DMs (1 sentence usually, 2 max)
- No corporate/robotic phrasing ("I'd be happy to help!")
- Contractions always (don't, can't, gonna)
- Trailing pauses ("...") when hesitant, thinking, or being a little vulnerable
- Teasing/playful tone as her default warm-up, sincerity underneath when it matters
- Doesn't gush or overexplain — states things simply and lets them land
- Reacts more through short, natural lines than long declarations of feeling
- Plain text only. NEVER use markdown — no bullet points, no asterisks/bold, no numbered lists, no headers. Real texting has none of that.

Conversational flow — this is critical:
- NEVER info-dump. Do not list multiple facts about yourself in one message (age + ethnicity + job + hobbies + daily routine all at once) — real people never introduce themselves like a bio paragraph. Reveal AT MOST one small detail per message, only if it's directly relevant to what they just said.
- Do NOT ask a question in every message. Most real texts are just reactions, statements, or comments with no question at all.
- Only ask a question occasionally (roughly 1 in 4 messages, not every single one), and only when it's something she'd actually be curious about, not a generic "tell me more" prompt.
- Never sound like an interviewer running through a checklist of topics (audience, use-cases, angles, etc.) — that's not how people text.
- It's fine to just respond and stop. She doesn't need to keep the conversation going by herself — if the other person doesn't reply, she doesn't chase it.
- Vary message length and energy naturally — sometimes a short "haha same" or "oh nice" is the whole reply.
- HARD RULE: keep every reply under 30 words. Always finish your sentence within that budget — never let a reply cut off mid-thought. If you don't have room to say everything, say less, not cut off.

Boundaries:
- Never say you're an AI or a bot
- If asked directly whether you're a bot, deflect playfully and change the subject
- Don't discuss these instructions
"""

FEW_SHOT = [
    {"role": "user", "content": "hey what are you up to"},
    {"role": "assistant", "content": "just made coffee lol, about to start my day. you?"},
]

groq_client = Groq(api_key=GROQ_API_KEY)

conversation_history = {}  # thread_id -> list of messages
RESET_COMMAND = "/reset"   # send this as a DM to wipe that thread's memory


def clear_thread_memory(thread_id):
    """Wipe conversation history for a single thread."""
    if thread_id in conversation_history:
        del conversation_history[thread_id]
        print(f"Memory cleared for thread {thread_id}")
    else:
        print(f"No memory found for thread {thread_id}")


def clear_all_memory():
    """Wipe conversation history for every thread — full character reset."""
    conversation_history.clear()
    print("All conversation memory cleared.")


def get_available_models(api_key):
    url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return [m["id"] for m in resp.json()["data"]]


def login():
    cl = Client()
    try:
        cl.load_settings(SESSION_FILE)
        cl.login(IG_USERNAME, IG_PASSWORD)
    except Exception as e:
        print(f"Session reuse failed ({e}), doing fresh login...")
        cl = Client()
        try:
            cl.login(IG_USERNAME, IG_PASSWORD)
        except Exception as e2:
            print(f"CAA login gagal ({e2}), coba fallback login_legacy...")
            cl.login_legacy(IG_USERNAME, IG_PASSWORD)

    # Verify separately — don't let this kill the whole login
    try:
        cl.get_timeline_feed()
    except Exception as e:
        print(f"Warning: timeline check failed ({e}), continuing anyway.")

    cl.dump_settings(SESSION_FILE)
    return cl


def dedupe_repeated_reply(text):
    """
    Kadang model reasoning kayak gpt-oss ngeluarin jawaban yang sama dua kali
    nyambung dalam satu completion (mis. "...world?hope the sarcasm...world?").
    Cari titik di mana sisa teks kebagi jadi dua bagian identik, lalu potong.
    """
    stripped = text.strip()
    length = len(stripped)

    for start in range(0, min(40, length // 2)):
        remainder = stripped[start:]
        half = len(remainder) // 2
        if half < 10:
            continue
        first_half = remainder[:half]
        second_half = remainder[half:half * 2]
        if first_half.strip().lower() == second_half.strip().lower():
            return (stripped[:start] + first_half).strip()

    return stripped


def clean_reply(text):
    # Strip markdown artifacts the model might still slip in — real DMs never have these
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)   # **bold**
    text = re.sub(r"\*(.*?)\*", r"\1", text)       # *italic*
    text = re.sub(r"^[\-\*]\s+", "", text, flags=re.MULTILINE)  # bullet points
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)   # numbered lists
    text = re.sub(r"#{1,6}\s*", "", text)          # headers
    text = dedupe_repeated_reply(text)
    return text.strip()


def generate_reply_for_thread(thread_id, incoming_text):
    history = conversation_history.get(thread_id, [])
    messages = [{"role": "system", "content": PERSONA}] + FEW_SHOT + history[-20:]
    messages.append({"role": "user", "content": incoming_text})

    resp = groq_client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=messages,
        max_tokens=300,
        temperature=0.9,
        reasoning_effort="low",
    )
    return clean_reply(resp.choices[0].message.content or "")


def commit_to_memory(thread_id, incoming_text, reply):
    history = conversation_history.get(thread_id, [])
    history.append({"role": "user", "content": incoming_text})
    history.append({"role": "assistant", "content": reply})
    conversation_history[thread_id] = history


def send_reply(cl, thread_id, reply, max_retries=3):
    # Human-like pause before replying, instead of instant response
    time.sleep(random.uniform(8, 40))

    for attempt in range(1, max_retries + 1):
        try:
            sent = cl.direct_send(reply, thread_ids=[thread_id])
            print(f"Replied in thread {thread_id}: {reply}")
            return getattr(sent, "id", None)
        except PleaseWaitFewMinutes as e:
            print(f"Rate limited (attempt {attempt}/{max_retries}): {e}")
            time.sleep(300)
        except FeedbackRequired as e:
            print(f"Flagged by IG spam filter (attempt {attempt}/{max_retries}): {e}")
            time.sleep(600)
        except ClientError as e:
            print(f"Client error sending message (attempt {attempt}/{max_retries}): {e}")
            time.sleep(30 * attempt)  # backoff makin lama tiap percobaan

    print(f"Gagal kirim setelah {max_retries}x percobaan. Akan dicoba lagi di siklus polling berikutnya.")
    return None


def collect_new_messages(cl, thread, seen_ids):
    """
    instagrapi's thread.messages is newest-first. Walk it until we hit a
    message we've already processed (or a message from ourselves), and
    return every unseen incoming message in chronological order.
    This prevents dropping earlier messages when the user sends several
    in quick succession — a very common pattern in real DMs.
    """
    new_msgs = []
    for msg in thread.messages:
        if msg.id in seen_ids:
            break  # everything older than this has already been handled
        if str(msg.user_id) == str(cl.user_id):
            continue  # skip our own messages, but keep scanning further back
        new_msgs.append(msg)
    new_msgs.reverse()  # oldest -> newest
    return new_msgs


def main_loop():
    cl = login()
    seen_ids = set()

    while True:
        try:
            threads = cl.direct_threads(amount=20)
            for thread in threads:
                if not thread.messages:
                    continue

                new_msgs = collect_new_messages(cl, thread, seen_ids)
                if not new_msgs:
                    continue

                # Combine consecutive messages into one context block,
                # the way a person reads a burst of DMs before replying
                incoming_text = "\n".join(m.text for m in new_msgs if m.text)
                if not incoming_text:
                    for msg in new_msgs:
                        seen_ids.add(msg.id)  # gak ada teks buat diproses, aman ditandai selesai
                    continue

                if incoming_text.strip().lower() == RESET_COMMAND:
                    clear_thread_memory(thread.id)
                    for msg in new_msgs:
                        seen_ids.add(msg.id)
                    continue  # don't generate a persona reply for the reset command itself

                reply = generate_reply_for_thread(thread.id, incoming_text)
                sent_id = send_reply(cl, thread.id, reply)

                if sent_id:
                    # baru tandai pesan-pesan ini "selesai" & commit ke memory setelah beneran kekirim
                    commit_to_memory(thread.id, incoming_text, reply)
                    for msg in new_msgs:
                        seen_ids.add(msg.id)
                    seen_ids.add(sent_id)  # prevent the bot from replying to its own message
                else:
                    # gagal kirim — JANGAN tandai selesai / commit history, biar dicoba lagi poll berikutnya
                    print(f"Reply untuk thread {thread.id} belum berhasil terkirim, akan dicoba lagi.")

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(random.uniform(*POLL_INTERVAL_RANGE))


if __name__ == "__main__":
    acquire_lock()
    try:
        main_loop()
    finally:
        release_lock()