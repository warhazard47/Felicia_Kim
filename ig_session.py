"""
ig_session.py

One login routine shared by ig_bot.py, auto_reply_comments.py and post_to_instagram.py.

Order of attempts:
1. Load session.json (keeps the SAME device fingerprint between runs, which
   Instagram trusts far more than a new device every time).
2. Normal password login. Recent instagrapi versions reuse the saved session if it is
   still valid, and otherwise log in again on that same device. The result is saved.
3. LAST RESORT: the browser sessionid cookie. Instagram often rejects a browser
   session for the mobile API and logs the browser out, so this is only a fallback.

Keep this file in the same folder as the other scripts.
"""

import os

from instagrapi import Client


def login(username, password, session_file="session.json", sessionid=""):
    cl = Client()

    if os.path.exists(session_file):
        try:
            cl.set_settings(cl.load_settings(session_file))
        except Exception as e:
            print(f"Couldn't read {session_file} ({e}), starting with a fresh device profile.")
            cl = Client()

    if password:
        try:
            cl.login(username, password)
            cl.dump_settings(session_file)
            return cl
        except Exception as e:
            print(f"Password login failed: {e}")

    if sessionid:
        print("Falling back to the browser session ID (Instagram often rejects these)...")
        try:
            cl.login_by_sessionid(sessionid)
            cl.get_timeline_feed()   # make sure Instagram actually accepts it
            cl.dump_settings(session_file)
            return cl
        except Exception as e:
            print(f"Session ID login failed too: {e}")

    raise SystemExit(
        "Could not log in. Not retrying, because repeated failed logins can make Instagram "
        "restrict the account. Check the login notes, fix the cause, then run again."
    )
