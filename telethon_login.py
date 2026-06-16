"""
One-time Telethon login helper.

Connects with your account, requests the login code, then waits for the code
(and 2FA password if needed) to be dropped into the auth_input/ folder.
Prints clear STATUS markers so the setup process can react step by step.

Creates session_name.session on success, after which main.py never needs this.
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
PHONE = os.environ["TELEGRAM_PHONE"]

INPUT_DIR = Path("auth_input")
INPUT_DIR.mkdir(exist_ok=True)
CODE_FILE = INPUT_DIR / "code.txt"
PASSWORD_FILE = INPUT_DIR / "password.txt"


def status(msg: str):
    print(f"STATUS: {msg}", flush=True)


async def wait_for_file(path: Path, timeout: float = 600.0) -> str:
    waited = 0.0
    while waited < timeout:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                try:
                    path.unlink()
                except OSError:
                    pass
                return value
        await asyncio.sleep(2)
        waited += 2
    raise TimeoutError(f"Timed out waiting for {path}")


async def main():
    client = TelegramClient("session_name", API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        status("ALREADY_AUTHORIZED")
        me = await client.get_me()
        status(f"LOGGED_IN_AS {me.first_name} (@{me.username})")
        await client.disconnect()
        return

    await client.send_code_request(PHONE)
    status("CODE_SENT")

    code = await wait_for_file(CODE_FILE)
    status("CODE_RECEIVED")

    try:
        await client.sign_in(PHONE, code)
    except SessionPasswordNeededError:
        status("PASSWORD_NEEDED")
        password = await wait_for_file(PASSWORD_FILE)
        status("PASSWORD_RECEIVED")
        await client.sign_in(password=password)
    except Exception as e:
        status(f"ERROR {type(e).__name__}: {e}")
        await client.disconnect()
        sys.exit(1)

    me = await client.get_me()
    status(f"AUTH_OK LOGGED_IN_AS {me.first_name} (@{me.username})")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
