"""
One-off pipeline test.

Simulates a new source-channel post WITHOUT waiting for a real one:
  1. translates a sample Russian football post via Claude
  2. saves it to the pending DB (same as main.py does)
  3. sends YOU the approval DM with the ✅ Post / ❌ Skip buttons

The already-running main.py handles the button press (same callback format),
so tapping ✅ Post will publish to your target channel — a true end-to-end test.

Run: python test_pipeline.py
"""

import asyncio
import os

from dotenv import load_dotenv
from anthropic import AsyncAnthropic
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

import main  # reuse DB helpers, prompt, model, keyboard

load_dotenv()

ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])
BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

SAMPLE_RU = (
    "\U0001F525 ОФИЦИАЛЬНО: Лионель Месси продлил контракт с «Интер Майами» до 2028 года!\n\n"
    "Аргентинский нападающий забил 20 голов в текущем сезоне MLS и помог команде "
    "выйти в плей-офф. «Я счастлив остаться здесь», — заявил Месси."
)


async def run():
    main.init_db()

    client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    print("Translating sample RU -> UZ via Claude...")
    resp = await client.messages.create(
        model=main.TRANSLATION_MODEL,
        max_tokens=1024,
        system=main.TRANSLATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": SAMPLE_RU}],
    )
    translated = resp.content[0].text.strip()
    print("\n--- Uzbek translation ---\n" + translated + "\n-------------------------\n")

    post_id = main.save_pending_post(translated, None)
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Post", callback_data=f"approve:{post_id}"),
            InlineKeyboardButton("❌ Skip", callback_data=f"reject:{post_id}"),
        ]]
    )

    bot = Bot(BOT_TOKEN)
    async with bot:
        await bot.send_message(
            chat_id=ADMIN_USER_ID,
            text="\U0001F9EA TEST POST\n\n" + translated,
            reply_markup=keyboard,
        )
    print(f"Approval message sent to admin (post_id={post_id}). Check Telegram and tap a button.")


if __name__ == "__main__":
    asyncio.run(run())
