import asyncio
import os
import json
import re
import html
import secrets
import string
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeAllPrivateChats,
)
from aiogram.filters import Command, CommandObject
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

RAW_ADMINS = str(
    os.getenv("ADMIN_ID", "7688706582")
)

ADMIN_IDS = [
    re.sub(r"[^0-9]", "", x)
    for x in RAW_ADMINS.split(",")
    if x.strip()
]

ADMIN_IDS = [x for x in ADMIN_IDS if x]

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")

# ================= BOT =================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher()
router = Router()

dp.include_router(router)

# ================= FILES =================

PREMIUM_FILE = "premium.json"
KEYS_FILE = "keys.json"
USERS_FILE = "users.json"

# ================= DATABASE =================

def load_db(filename):
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f:
                return json.load(f)
        except:
            return {}

    return {}


def save_db(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f, indent=4)

# ================= HELPERS =================

def is_admin(user_id):
    return str(user_id) in ADMIN_IDS


def add_user(user_id):
    users = load_db(USERS_FILE)

    if str(user_id) not in users:
        users[str(user_id)] = (
            datetime.now().isoformat()
        )

        save_db(USERS_FILE, users)


def check_tier(user_id):
    if is_admin(user_id):
        return "👑 Admin"

    premium = load_db(PREMIUM_FILE)

    uid = str(user_id)

    if uid in premium:
        try:
            expiry = datetime.fromisoformat(
                premium[uid]
            )

            if datetime.now() < expiry:
                return "💎 Premium"

        except:
            pass

    return "🔑 Free"

# ================= CALLBACK =================

@router.callback_query(F.data == "noop")
async def noop_callback(
    callback: CallbackQuery
):
    await callback.answer()

# ================= START =================

@router.message(Command("start"))
async def cmd_start(
    message: Message,
    state: FSMContext,
):
    await state.clear()

    add_user(message.from_user.id)

    username = (
        message.from_user.first_name
    )

    tier = check_tier(
        message.from_user.id
    )

    text = f"""
🤖 <b>BEAR CHECKER</b>

Hey, <b>{html.escape(username)}</b>!
Your tier: {tier}

━━━━━━━━━━━━━━
💳 <b>AVAILABLE GATES</b>
━━━━━━━━━━━━━━

🛒 Shopify
<code>/sh</code> → Single
<code>/msh</code> → Mass

💰 PayPal
<code>/pp</code> → Single
<code>/mpp</code> → Mass

━━━━━━━━━━━━━━
📂 <b>MASS SUPPORT</b>
━━━━━━━━━━━━━━

• Normal cards
• Reply to .txt file
• Upload .txt directly

━━━━━━━━━━━━━━
⚙️ <b>SYSTEM</b>
━━━━━━━━━━━━━━

<code>/redeem</code> → Redeem key
<code>/status</code> → Subscription
<code>/myid</code> → Telegram ID
"""

    if is_admin(message.from_user.id):
        text += """

━━━━━━━━━━━━━━
👑 <b>ADMIN</b>
━━━━━━━━━━━━━━

<code>/genkey</code> → Generate keys
<code>/broadcast</code> → Broadcast
<code>/users</code> → Statistics
"""

    text += "\n\n🔥 Powered By BEAR"

    await message.answer(text)

# ================= STATUS =================

@router.message(Command("status"))
async def cmd_status(
    message: Message,
    state: FSMContext,
):
    await state.clear()

    tier = check_tier(
        message.from_user.id
    )

    await message.answer(
        f"""
👤 <b>Status</b>

🎫 Tier:
{tier}
"""
    )

# ================= MYID =================

@router.message(Command("myid"))
async def cmd_myid(message: Message):

    uid = str(message.from_user.id)

    admin = (
        "✅ YES"
        if is_admin(uid)
        else "❌ NO"
    )

    await message.answer(
        f"""
🆔 <b>Your ID</b>

<code>{uid}</code>

👑 Admin:
{admin}
"""
    )

# ================= GENKEY =================

@router.message(Command("genkey"))
async def cmd_genkey(
    message: Message,
    command: CommandObject,
    state: FSMContext,
):
    await state.clear()

    if not is_admin(
        message.from_user.id
    ):
        return await message.answer(
            "❌ Admin only"
        )

    if not command.args:
        return await message.answer(
            "Usage:\n<code>/genkey 10 7d</code>"
        )

    try:
        parts = command.args.split()

        amount = int(parts[0])

        days = int(
            parts[1]
            .lower()
            .replace("d", "")
        )

        keys = load_db(KEYS_FILE)

        generated = []

        for _ in range(amount):

            key = "BEAR-" + "".join(
                secrets.choice(
                    string.ascii_uppercase
                    + string.digits
                )
                for _ in range(12)
            )

            keys[key] = days

            generated.append(
                f"<code>{key}</code>"
            )

        save_db(KEYS_FILE, keys)

        await message.answer(
            f"""
✅ Generated {amount} Keys

""" + "\n".join(generated)
        )

    except Exception as e:
        await message.answer(
            f"""
❌ Error

<code>{html.escape(str(e))}</code>
"""
        )

# ================= REDEEM =================

@router.message(Command("redeem"))
async def cmd_redeem(
    message: Message,
    command: CommandObject,
    state: FSMContext,
):
    await state.clear()

    key = command.args

    if not key:
        return await message.answer(
            "Usage:\n<code>/redeem KEY</code>"
        )

    keys = load_db(KEYS_FILE)

    if key not in keys:
        return await message.answer(
            "❌ Invalid key"
        )

    days = keys[key]

    premium = load_db(PREMIUM_FILE)

    uid = str(message.from_user.id)

    premium[uid] = (
        datetime.now()
        + timedelta(days=days)
    ).isoformat()

    del keys[key]

    save_db(PREMIUM_FILE, premium)
    save_db(KEYS_FILE, keys)

    await message.answer(
        f"""
✅ Premium Activated

Days:
{days}
"""
    )

# ================= USERS =================

@router.message(Command("users"))
async def cmd_users(
    message: Message,
    state: FSMContext,
):
    await state.clear()

    if not is_admin(
        message.from_user.id
    ):
        return

    users = load_db(USERS_FILE)

    premium = load_db(PREMIUM_FILE)

    await message.answer(
        f"""
📊 <b>BOT STATS</b>

👥 Users:
{len(users)}

💎 Premium:
{len(premium)}
"""
    )

# ================= BROADCAST =================

@router.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message,
    command: CommandObject,
    state: FSMContext,
):
    await state.clear()

    if not is_admin(
        message.from_user.id
    ):
        return

    if not command.args:
        return await message.answer(
            "Usage:\n<code>/broadcast Hello</code>"
        )

    users = load_db(USERS_FILE)

    sent = 0

    for uid in users:
        try:
            await bot.send_message(
                uid,
                f"""
📢 <b>Announcement</b>

{command.args}
"""
            )

            sent += 1

        except:
            pass

    await message.answer(
        f"""
✅ Broadcast Complete

Sent:
{sent}/{len(users)}
"""
    )

# ================= CHECKERS =================

@router.message(Command("sh"))
async def cmd_sh(message: Message):
    await message.answer(
        "🛒 Shopify single checker connected"
    )

@router.message(Command("msh"))
async def cmd_msh(message: Message):
    await message.answer(
        "🛒 Shopify mass checker connected\n\nSupports txt files"
    )

@router.message(Command("pp"))
async def cmd_pp(message: Message):
    await message.answer(
        "💰 PayPal single checker connected"
    )

@router.message(Command("mpp"))
async def cmd_mpp(message: Message):
    await message.answer(
        "💰 PayPal mass checker connected\n\nSupports txt files"
    )

# ================= BOT COMMANDS =================

async def setup_bot_commands():

    user_commands = [
        BotCommand(
            command="start",
            description="Open menu",
        ),
        BotCommand(
            command="status",
            description="Subscription",
        ),
        BotCommand(
            command="sh",
            description="Single Shopify",
        ),
        BotCommand(
            command="msh",
            description="Mass Shopify",
        ),
        BotCommand(
            command="pp",
            description="Single PayPal",
        ),
        BotCommand(
            command="mpp",
            description="Mass PayPal",
        ),
        BotCommand(
            command="redeem",
            description="Redeem key",
        ),
        BotCommand(
            command="myid",
            description="Your ID",
        ),
    ]

    admin_commands = (
        user_commands
        + [
            BotCommand(
                command="genkey",
                description="Generate keys",
            ),
            BotCommand(
                command="broadcast",
                description="Broadcast",
            ),
            BotCommand(
                command="users",
                description="Statistics",
            ),
        ]
    )

    await bot.set_my_commands(
        user_commands,
        scope=BotCommandScopeAllPrivateChats(),
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(
                    chat_id=int(admin_id)
                ),
            )

        except Exception as e:
            print(
                f"Admin command error {admin_id}: {e}"
            )

# ================= MAIN =================

async def main():

    print("🐻 BEAR CHECKER STARTED")

    print("ADMINS:", ADMIN_IDS)

    await setup_bot_commands()

    await dp.start_polling(bot)

# ================= RUN =================

if __name__ == "__main__":
    asyncio.run(main())
