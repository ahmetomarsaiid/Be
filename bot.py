import asyncio
import os
import secrets
import string
import json
import time
import re
import aiohttp
import html
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ================= IMPORTS =================

from api import (
    process_card_async,
    parse_cc_string,
    extract_clean_response,
)

from paypal import check_paypal_cc

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

# ================= HELPERS =================

def is_admin(user_id):
    return str(user_id) in ADMIN_IDS


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

    db = load_db(PREMIUM_FILE)

    uid = str(user_id)

    if uid in db:
        try:
            expiry = datetime.fromisoformat(
                db[uid]
            )

            if datetime.now() < expiry:
                return "💎 Premium"

        except:
            pass

    return "🔑 Free"


async def get_bin_info(session, cc):
    try:
        bin6 = cc[:6]

        async with session.get(
            f"https://bins.antipublic.cc/bins/{bin6}",
            timeout=5,
        ) as res:

            if res.status == 200:
                data = await res.json()

                return (
                    data.get("brand", "Unknown"),
                    data.get("bank", "Unknown"),
                    data.get(
                        "country_name",
                        "Unknown"
                    ),
                    data.get("country_flag", ""),
                    data.get("type", "Unknown"),
                )

    except:
        pass

    return (
        "Unknown",
        "Unknown",
        "Unknown",
        "",
        "Unknown",
    )

# ================= STATES =================

class AppStates(StatesGroup):
    waiting_shopify_single = State()
    waiting_shopify_mass = State()
    waiting_paypal_single = State()
    waiting_paypal_mass = State()

# ================= UI =================

def generate_stats_keyboard(app, dec, err):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✅ {app}",
                    callback_data="noop",
                ),
                InlineKeyboardButton(
                    text=f"❌ {dec}",
                    callback_data="noop",
                ),
                InlineKeyboardButton(
                    text=f"⚠️ {err}",
                    callback_data="noop",
                ),
            ]
        ]
    )


def format_single_hit(
    status,
    checker,
    result,
    cc,
    country,
    flag,
    bank,
    brand,
    c_type,
    elapsed,
    tier,
    username,
):
    if status in [
        "APPROVED",
        "CHARGED",
    ]:
        header = "𝗔𝗣𝗣𝗥𝗢𝗩𝗘𝗗 ✅"

    elif status == "DECLINED":
        header = "𝗗𝗘𝗖𝗟𝗜𝗡𝗘𝗗 ❌"

    else:
        header = "𝗘𝗥𝗥𝗢𝗥 ⚠️"

    safe_result = html.escape(
        str(result)
    )

    return f"""
<b>{header}</b>

<b>CC ⇾</b>
<code>{cc}</code>

<b>Gateway ⇾</b>
{checker}

<b>Response ⇾</b>
<code>{safe_result}</code>

<b>BIN ⇾</b>
{brand} — {c_type}

<b>Bank ⇾</b>
{bank}

<b>Country ⇾</b>
{country} {flag}

<b>Time ⇾</b>
{elapsed:.2f}s

<b>Tier ⇾</b>
{tier}

<b>By ⇾</b>
@{username}
"""

# ================= CALLBACK =================

@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
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
        f"@{message.from_user.username}"
        if message.from_user.username
        else message.from_user.first_name
    )

    tier = check_tier(
        message.from_user.id
    )

    text = f"""
🐻 <b>BEAR CHECKER</b>

👋 {html.escape(username)}
🎫 {tier}

━━━━━━━━━━━━
🛒 <b>SHOPIFY</b>
━━━━━━━━━━━━

<code>/sh</code> → Single Check
<code>/msh</code> → Mass Check

➜ Supports:
• Normal cards
• Reply to .txt file

━━━━━━━━━━━━
💰 <b>PAYPAL</b>
━━━━━━━━━━━━

<code>/pp</code> → Single Check
<code>/mpp</code> → Mass Check

➜ Supports:
• Normal cards
• Reply to .txt file

━━━━━━━━━━━━
🔑 <b>SYSTEM</b>
━━━━━━━━━━━━

<code>/redeem</code> → Redeem Key
<code>/status</code> → Subscription
<code>/myid</code> → Telegram ID
"""

    if is_admin(message.from_user.id):
        text += """

━━━━━━━━━━━━
👑 <b>ADMIN</b>
━━━━━━━━━━━━

<code>/genkey</code> → Generate Keys
<code>/broadcast</code> → Message Users
<code>/users</code> → Bot Stats
"""

    text += "\n\n🔥 <b>Powered By BEAR</b>"

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

🎫 {tier}
"""
    )

# ================= MYID =================

@router.message(Command("myid"))
async def cmd_myid(message: Message):
    uid = str(message.from_user.id)

    admin_status = (
        "✅ YES"
        if is_admin(uid)
        else "❌ NO"
    )

    await message.answer(
        f"""
🆔 <b>Your Telegram ID</b>

<code>{uid}</code>

👑 Admin:
{admin_status}
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

        count = int(parts[0])

        duration = int(
            parts[1]
            .lower()
            .replace("d", "")
        )

        keys_db = load_db(KEYS_FILE)

        generated = []

        for _ in range(count):

            key = "BEAR-" + "".join(
                secrets.choice(
                    string.ascii_uppercase
                    + string.digits
                )
                for _ in range(12)
            )

            keys_db[key] = duration

            generated.append(
                f"<code>{key}</code>"
            )

        save_db(KEYS_FILE, keys_db)

        await message.answer(
            f"✅ Generated {count} keys\n\n"
            + "\n".join(generated)
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

    keys_db = load_db(KEYS_FILE)

    if key not in keys_db:
        return await message.answer(
            "❌ Invalid key"
        )

    days = keys_db[key]

    premium = load_db(PREMIUM_FILE)

    uid = str(message.from_user.id)

    premium[uid] = (
        datetime.now()
        + timedelta(days=days)
    ).isoformat()

    del keys_db[key]

    save_db(PREMIUM_FILE, premium)
    save_db(KEYS_FILE, keys_db)

    await message.answer(
        f"✅ Premium activated for {days} days"
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

    premium = load_db(
        PREMIUM_FILE
    )

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
""",
            )

            sent += 1

            await asyncio.sleep(0.05)

        except:
            pass

    await message.answer(
        f"""
✅ Broadcast Complete

Sent:
{sent}/{len(users)}
"""
    )

# ================= CHECKER =================

async def process_checker(
    message,
    text,
    checker,
):
    cards = re.findall(
        r"\d{15,16}\|\d{2}\|\d{2,4}\|\d{3,4}",
        text,
    )

    if not cards:
        return await message.answer(
            "❌ No valid cards found"
        )

    tier = check_tier(
        message.from_user.id
    )

    username = (
        message.from_user.username
        or message.from_user.first_name
    )

    app = 0
    dec = 0
    err = 0

    start_time = time.time()

    msg = await message.answer(
        "⏳ Checking...",
        reply_markup=generate_stats_keyboard(
            app,
            dec,
            err,
        ),
    )

    async with aiohttp.ClientSession() as session:

        for cc in cards:

            try:
                parts = parse_cc_string(cc)

                cc_clean = parts["cc"]
                mes = parts["mes"]
                ano = parts["ano"]
                cvv = parts["cvv"]

                (
                    brand,
                    bank,
                    country,
                    flag,
                    c_type,
                ) = await get_bin_info(
                    session,
                    cc_clean,
                )

                if "Shopify" in checker:

                    (
                        success,
                        raw,
                        _,
                        _,
                        _,
                    ) = await process_card_async(
                        cc_clean,
                        mes,
                        ano,
                        cvv,
                        "https://shop.app",
                    )

                    response = (
                        extract_clean_response(
                            raw
                        )
                    )

                    status = (
                        "APPROVED"
                        if success
                        else "DECLINED"
                    )

                else:

                    (
                        status,
                        raw,
                    ) = await asyncio.to_thread(
                        check_paypal_cc,
                        cc,
                    )

                    response = (
                        extract_clean_response(
                            raw
                        )
                    )

                if status in [
                    "APPROVED",
                    "CHARGED",
                ]:
                    app += 1

                elif (
                    status
                    == "DECLINED"
                ):
                    dec += 1

                else:
                    err += 1

                elapsed = (
                    time.time()
                    - start_time
                )

                result = format_single_hit(
                    status,
                    checker,
                    response,
                    cc,
                    country,
                    flag,
                    bank,
                    brand,
                    c_type,
                    elapsed,
                    tier,
                    username,
                )

                await message.answer(result)

                await msg.edit_reply_markup(
                    reply_markup=generate_stats_keyboard(
                        app,
                        dec,
                        err,
                    )
                )

            except Exception as e:

                err += 1

                await message.answer(
                    f"""
⚠️ Error

<code>{html.escape(str(e))}</code>
"""
                )

# ================= SINGLE SHOPIFY =================

@router.message(Command("sh"))
async def cmd_sh(
    message: Message,
    command: CommandObject,
    state: FSMContext,
):
    await state.clear()

    if not command.args:
        return await message.answer(
            "Usage:\n<code>/sh CC|MM|YYYY|CVV</code>"
        )

    await process_checker(
        message,
        command.args,
        "Shopify Single",
    )

# ================= MASS SHOPIFY =================

@router.message(Command("msh"))
async def cmd_msh(
    message: Message,
    command: CommandObject,
    state: FSMContext,
):
    await state.clear()

    text = command.args or ""

    # Reply to txt file
    if (
        message.reply_to_message
        and message.reply_to_message.document
    ):
        file = await bot.get_file(
            message.reply_to_message.document.file_id
        )

        downloaded = await bot.download_file(
            file.file_path
        )

        text += "\n" + downloaded.read().decode(
            "utf-8",
            errors="ignore",
        )

    # Direct txt upload
    elif message.document:

        file = await bot.get_file(
            message.document.file_id
        )

        downloaded = await bot.download_file(
            file.file_path
        )

        text += "\n" + downloaded.read().decode(
            "utf-8",
            errors="ignore",
        )

    if not text.strip():
        return await message.answer(
            "Reply to .txt file or send cards"
        )

    await process_checker(
        message,
        text,
        "Shopify Mass",
    )

# ================= SINGLE PAYPAL =================

@router.message(Command("pp"))
async def cmd_pp(
    message: Message,
    command: CommandObject,
    state: FSMContext,
):
    await state.clear()

    if not command.args:
        return await message.answer(
            "Usage:\n<code>/pp CC|MM|YYYY|CVV</code>"
        )

    await process_checker(
        message,
        command.args,
        "PayPal Single",
    )

# ================= MASS PAYPAL =================

@router.message(Command("mpp"))
async def cmd_mpp(
    message: Message,
    command: CommandObject,
    state: FSMContext,
):
    await state.clear()

    text = command.args or ""

    # Reply to txt file
    if (
        message.reply_to_message
        and message.reply_to_message.document
    ):
        file = await bot.get_file(
            message.reply_to_message.document.file_id
        )

        downloaded = await bot.download_file(
            file.file_path
        )

        text += "\n" + downloaded.read().decode(
            "utf-8",
            errors="ignore",
        )

    # Direct txt upload
    elif message.document:

        file = await bot.get_file(
            message.document.file_id
        )

        downloaded = await bot.download_file(
            file.file_path
        )

        text += "\n" + downloaded.read().decode(
            "utf-8",
            errors="ignore",
        )

    if not text.strip():
        return await message.answer(
            "Reply to .txt file or send cards"
        )

    await process_checker(
        message,
        text,
        "PayPal Mass",
    )

# ================= BOT COMMANDS =================

async def setup_bot_commands(bot):

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
                f"Failed admin cmds {admin_id}: {e}"
            )

# ================= MAIN =================

async def main():

    print("🐻 BEAR CHECKER STARTED")

    print("ADMINS:", ADMIN_IDS)

    await setup_bot_commands(bot)

    await dp.start_polling(bot)

# ================= RUN =================

if __name__ == "__main__":
    asyncio.run(main())
