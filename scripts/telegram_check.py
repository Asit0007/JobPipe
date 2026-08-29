"""Verify Telegram delivery, and find your chat id if it is missing.

`notify.py` fails soft on purpose: with no token it prints the queue to stdout
and returns False, so a broken config looks exactly like "nothing to send".
This makes the difference visible, and does the one genuinely fiddly part --
discovering your chat id -- rather than sending you to a third-party bot for it.

    make telegram-check PY=./.venv/bin/python
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpipe.config import env          # noqa: E402

OK, WARN, BAD = "  ok  ", " warn ", " FAIL "
API = "https://api.telegram.org/bot{token}/{method}"


def line(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}")


def call(token: str, method: str, **params):
    r = httpx.get(API.format(token=token, method=method), params=params, timeout=20)
    return r.status_code, r.json() if r.headers.get(
        "content-type", "").startswith("application/json") else {}


def main() -> int:
    token = env("TELEGRAM_BOT_TOKEN", "")
    chat = env("TELEGRAM_CHAT_ID", "")

    if not token:
        line(BAD, "TELEGRAM_BOT_TOKEN is empty in .env")
        print("""
  Open Telegram -> @BotFather -> /newbot -> pick a name and a username.
  It replies with a token like 8123456789:AAH... . Put it in .env as
  TELEGRAM_BOT_TOKEN, then run this again to discover your chat id.

  Note an empty value reads as 'configured' to a careless grep -- the key
  being present in .env is not the same as it being set.""")
        return 1

    status, body = call(token, "getMe")
    if status != 200 or not body.get("ok"):
        line(BAD, f"getMe returned {status}: {body.get('description', body)}")
        print("  The token is wrong or the bot was deleted. Re-issue via @BotFather.")
        return 1
    bot = body["result"]
    line(OK, f"bot @{bot.get('username')} ({bot.get('first_name')})")

    if not chat:
        line(WARN, "TELEGRAM_CHAT_ID is empty -- looking for it in recent updates")
        status, body = call(token, "getUpdates", limit=20)
        seen = {}
        for u in body.get("result", []):
            msg = u.get("message") or u.get("channel_post") or {}
            c = msg.get("chat") or {}
            if c.get("id") is not None:
                who = c.get("username") or c.get("title") or c.get("first_name") or "?"
                seen[c["id"]] = f"{who} ({c.get('type')})"
        if not seen:
            print(f"""
  No messages yet. Open Telegram, find @{bot.get('username')}, and send it
  any message (/start works). Then run this again -- the chat id will appear
  here. A bot cannot message you until you have messaged it first.""")
            return 1
        print("\n  Candidate chat ids:")
        for cid, who in seen.items():
            print(f"    TELEGRAM_CHAT_ID={cid}    {who}")
        print("\n  Put the right one in .env, then run this again to send a test.")
        return 1

    r = httpx.post(API.format(token=token, method="sendMessage"), timeout=20, json={
        "chat_id": chat,
        "text": "*jobpipe* is wired up.\nYou apply. I don't.",
        "parse_mode": "Markdown",
    })
    if r.status_code != 200:
        line(BAD, f"sendMessage returned {r.status_code}: {r.text[:200]}")
        print("  A 400 'chat not found' usually means the id is wrong, or you have")
        print("  not sent the bot a message yet.")
        return 1

    line(OK, f"test message delivered to chat {chat}")
    print("\n`make notify` will now push the prepared queue instead of printing it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
