"""
Football News Translator Bot
=============================

What this does:
1. Listens to 2+ Russian football news channels (using YOUR Telegram account)
2. When a new post appears, translates it RU -> UZ using Claude
3. Sends you the translation (with original media if any) + Approve/Reject buttons
4. If you tap Approve -> posts it to your channel via your bot
5. If you tap Reject -> just marks it skipped, nothing gets posted

Run with: python main.py
First run will ask for a login code sent to your Telegram app (one-time only).
"""

import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path

# Windows consoles default to a legacy codepage (e.g. cp1251) that can't encode
# emoji or some Cyrillic, which would crash logging when we log post snippets.
# Force UTF-8 on stdout/stderr so the bot never dies on a Unicode log line.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from dotenv import load_dotenv
from telethon import TelegramClient
from anthropic import AsyncAnthropic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

# ---------------------------------------------------------------------------
# Setup & config
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("football-bot")

TELEGRAM_API_ID = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_PHONE = os.environ["TELEGRAM_PHONE"]

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])
TARGET_CHANNEL = os.environ["TARGET_CHANNEL"]

SOURCE_CHANNELS = [c.strip() for c in os.environ["SOURCE_CHANNELS"].split(",") if c.strip()]

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TRANSLATION_MODEL = "claude-haiku-4-5-20251001"

MEDIA_DIR = Path("media")
MEDIA_DIR.mkdir(exist_ok=True)

DB_PATH = "pending_posts.db"

TRANSLATION_SYSTEM_PROMPT = """You are a professional sports translator working for an Uzbek football news Telegram channel.

Translate the given Russian football news text into natural, fluent Uzbek (Latin script).

Rules:
- Keep player names, club names, league names, and scores accurate and recognizable
- Match the energetic, engaging tone typical of football news channels
- Preserve emoji, line breaks, and any formatting from the original
- Do not add commentary, hashtags, or notes of your own
- Output ONLY the translated text, nothing else
"""

# ---------------------------------------------------------------------------
# Database (tracks posts waiting for approval)
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            translated_text TEXT,
            media_path TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def save_pending_post(translated_text: str, media_path: str | None) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO pending_posts (translated_text, media_path) VALUES (?, ?)",
        (translated_text, media_path),
    )
    conn.commit()
    post_id = cur.lastrowid
    conn.close()
    return post_id


def get_pending_post(post_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT translated_text, media_path, status FROM pending_posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    conn.close()
    return row


def set_post_status(post_id: int, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE pending_posts SET status = ? WHERE id = ?", (status, post_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


async def translate_to_uzbek(text: str) -> str:
    if not text.strip():
        return ""
    response = await anthropic_client.messages.create(
        model=TRANSLATION_MODEL,
        max_tokens=1024,
        system=TRANSLATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Telegram bot (sends approval requests, posts to channel)
# ---------------------------------------------------------------------------

bot_app = Application.builder().token(BOT_TOKEN).build()


def build_approval_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Post", callback_data=f"approve:{post_id}"),
                InlineKeyboardButton("❌ Skip", callback_data=f"reject:{post_id}"),
            ]
        ]
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_USER_ID:
        return  # ignore anyone who isn't you

    action, post_id_str = query.data.split(":")
    post_id = int(post_id_str)

    row = get_pending_post(post_id)
    if row is None:
        await query.edit_message_caption(caption="⚠️ Not found (already handled?)") \
            if query.message.caption else await query.edit_message_text("⚠️ Not found (already handled?)")
        return

    translated_text, media_path, status = row

    if status != "pending":
        return  # already handled, ignore double-clicks

    if action == "approve":
        try:
            if media_path:
                with open(media_path, "rb") as f:
                    await context.bot.send_photo(
                        chat_id=TARGET_CHANNEL, photo=f, caption=translated_text
                    )
            else:
                await context.bot.send_message(chat_id=TARGET_CHANNEL, text=translated_text)

            set_post_status(post_id, "posted")
            new_caption_suffix = "\n\n✅ Posted to channel"
        except Exception as e:
            log.exception("Failed to post to channel")
            new_caption_suffix = f"\n\n⚠️ Failed to post: {e}"
    else:
        set_post_status(post_id, "skipped")
        new_caption_suffix = "\n\n❌ Skipped"

    new_text = (translated_text or "") + new_caption_suffix
    try:
        if query.message.caption is not None:
            await query.edit_message_caption(caption=new_text, reply_markup=None)
        else:
            await query.edit_message_text(text=new_text, reply_markup=None)
    except Exception:
        log.exception("Failed to edit message after action")

    # clean up downloaded media file once we're done with it
    if media_path and os.path.exists(media_path):
        try:
            os.remove(media_path)
        except OSError:
            pass


bot_app.add_handler(CallbackQueryHandler(handle_callback))


# ---------------------------------------------------------------------------
# Telethon client (reads the source channels using YOUR account)
# ---------------------------------------------------------------------------

# NOTE: the TelegramClient is created inside main() rather than at import time.
# On Python 3.14, instantiating it at module level raises "no running event loop"
# because Telethon grabs the loop in __init__ and 3.14 no longer auto-creates one
# outside an async context.

# How often (seconds) to poll the source channels for new posts.
POLL_INTERVAL_SECONDS = 30


async def process_message(tg_client, message):
    """Translate a single source-channel message and send it for approval."""
    text = message.message or ""

    log.info("New post from source channel: %s", (text[:60] + "...") if text else "[media only]")

    try:
        translated = await translate_to_uzbek(text) if text else ""
    except Exception:
        log.exception("Translation failed")
        translated = "[Tarjima xato bilan yakunlandi. Original matn:]\n\n" + text

    media_path = None
    if message.photo:
        media_path = str(MEDIA_DIR / f"{message.id}.jpg")
        await message.download_media(file=media_path)

    post_id = save_pending_post(translated, media_path)
    keyboard = build_approval_keyboard(post_id)

    caption_text = translated or "[Media-only post, no text]"

    if media_path:
        with open(media_path, "rb") as f:
            await bot_app.bot.send_photo(
                chat_id=ADMIN_USER_ID,
                photo=f,
                caption=caption_text,
                reply_markup=keyboard,
            )
    else:
        await bot_app.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=caption_text,
            reply_markup=keyboard,
        )


async def poll_sources(tg_client, source_entities, last_ids):
    """Poll each source channel for messages newer than the last one we saw.

    We poll instead of using events.NewMessage because user-account clients do
    not reliably receive real-time updates for *broadcast* channels (read-only
    channels like these). Polling get_messages is reliable for that channel type.
    """
    while True:
        for ent in source_entities:
            try:
                # Newest first; min_id excludes everything we've already handled.
                new_msgs = await tg_client.get_messages(
                    ent, min_id=last_ids[ent.id], limit=50
                )
            except Exception:
                log.exception("Failed to poll channel id=%s", ent.id)
                continue

            if not new_msgs:
                continue

            # Process oldest -> newest so approvals arrive in chronological order.
            for message in reversed(new_msgs):
                if message.id <= last_ids[ent.id]:
                    continue
                try:
                    await process_message(tg_client, message)
                except Exception:
                    log.exception("Failed to process message id=%s", message.id)
                last_ids[ent.id] = max(last_ids[ent.id], message.id)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    init_db()

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    log.info("Approval bot is polling for button presses...")

    tg_client = TelegramClient("session_name", TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await tg_client.start(phone=TELEGRAM_PHONE)

    # Resolve the source channels to concrete entities, and seed each channel's
    # "last seen" id with its current newest message so we only forward NEW posts
    # from now on (not the existing backlog).
    source_entities = []
    last_ids = {}
    for name in SOURCE_CHANNELS:
        try:
            ent = await tg_client.get_entity(name)
            source_entities.append(ent)
            latest = await tg_client.get_messages(ent, limit=1)
            last_ids[ent.id] = latest[0].id if latest else 0
            log.info("Watching source channel %s -> id=%s title=%r (from msg id %s)",
                     name, ent.id, getattr(ent, "title", "?"), last_ids[ent.id])
        except Exception:
            log.exception("Could not resolve source channel %s", name)

    log.info("Polling %d source channel(s) every %ds", len(source_entities), POLL_INTERVAL_SECONDS)

    try:
        await poll_sources(tg_client, source_entities, last_ids)
    finally:
        await tg_client.disconnect()
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
