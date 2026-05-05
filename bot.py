import asyncio
import os
import secrets
import string
import json
import time
import aiohttp
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# --- BACKEND IMPORTS ---
from api import process_card_async, parse_cc_string, extract_clean_response
from paypal import check_paypal_cc # <--- Your newly extracted PayPal engine

BOT_TOKEN = os.getenv("BOT_TOKEN") # Make sure this is set in Heroku/Render configs!
ADMIN_ID = os.getenv("ADMIN_ID") 

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

PREMIUM_FILE = "premium.json"
KEYS_FILE = "keys.json"

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
    return "🆓 FREE"

async def get_bin_info(session, cc):
    try:
        bin6 = cc[:6]
        async with session.get(f"https://bins.antipublic.cc/bins/{bin6}", timeout=5) as res:
            if res.status == 200:
                data = await res.json()
                return data.get('brand', 'UNKNOWN'), data.get('bank', 'UNKNOWN'), data.get('country_name', 'UNKNOWN'), data.get('country_flag', '')
    except: pass
    return "UNKNOWN", "UNKNOWN", "UNKNOWN", ""

# --- FSM STATES ---
class AppStates(StatesGroup):
    waiting_for_shopify = State()
    waiting_for_paypal = State()

# --- UI KEYBOARDS ---
def kb_home(user_id):
    is_admin = str(user_id) == str(ADMIN_ID)
    kb = [
        [InlineKeyboardButton(text="🟢 Shopify Gateway", callback_data="goto_shopify")],
        [InlineKeyboardButton(text="🔵 PayPal Gateway", callback_data="goto_paypal")],
        [InlineKeyboardButton(text="👤 Profile", callback_data="ui_plan")]
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="⚙️ Admin Panel", callback_data="ui_admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_gateway(gateway_type):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Single Check", callback_data=f"single_{gateway_type}")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="ui_home")]
    ])

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="ui_home")]])

# --- DASHBOARD HANDLER ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text = (f"⚡️ <b>BEAR CHECKER OS</b> ⚡️\n━━━━━━━━━━━━━━━━━━\n\n"
            f"Welcome, <b>{message.from_user.first_name}</b>.\nTier: {check_tier(message.from_user.id)}\n\n"
            f"<i>Select your gateway folder:</i>")
    msg = await message.answer(text, reply_markup=kb_home(message.from_user.id))
    await state.update_data(dash_id=msg.message_id)

@router.callback_query(F.data == "ui_home")
async def nav_home(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(f"⚡️ <b>BEAR CHECKER OS</b> ⚡️\n━━━━━━━━━━━━━━━━━━\n\nTier: {check_tier(callback.from_user.id)}", reply_markup=kb_home(callback.from_user.id))

# --- GATEWAY FOLDERS ---
@router.callback_query(F.data == "goto_shopify")
async def folder_shopify(c: CallbackQuery):
    await c.message.edit_text("🟢 <b>SHOPIFY GATEWAY</b>\n━━━━━━━━━━━━━━━━━━\n\nSelect a tool:", reply_markup=kb_gateway("shopify"))

@router.callback_query(F.data == "goto_paypal")
async def folder_paypal(c: CallbackQuery):
    await c.message.edit_text("🔵 <b>PAYPAL GATEWAY</b>\n━━━━━━━━━━━━━━━━━━\n\nSelect a tool:", reply_markup=kb_gateway("paypal"))

# --- SINGLE CHECK LOGIC ---
@router.callback_query(F.data.startswith("single_"))
async def nav_single(c: CallbackQuery, state: FSMContext):
    gway = c.data.split("_")[1]
    await c.message.edit_text(f"💳 <b>{gway.upper()} TERMINAL</b>\n━━━━━━━━━━━━━━━━━━\n\nPaste card: <code>CC|MM|YYYY|CVV</code>", reply_markup=kb_back())
    await state.set_state(AppStates.waiting_for_shopify if gway == "shopify" else AppStates.waiting_for_paypal)

@router.message(AppStates.waiting_for_shopify)
@router.message(AppStates.waiting_for_paypal)
async def process_single(message: Message, state: FSMContext):
    g_type = "shopify" if await state.get_state() == AppStates.waiting_for_shopify else "paypal"
    cc = message.text.strip()
    try: await message.delete()
    except: pass

    data = await state.get_data()
    dash_id = data.get("dash_id")
    loading = await bot.edit_message_text(f"⏳ <b>AUTHORIZING... ({g_type.upper()})</b>\nTarget: <code>{cc}</code>", message.chat.id, dash_id)

    try:
        if g_type == "shopify":
            parts = parse_cc_string(cc)
            success, raw, g_name, p, c = await process_card_async(parts['cc'], parts['mes'], parts['ano'], parts['cvv'], "https://shop.spam.com")
            status = "CHARGED" if success else "DECLINED"
            resp = extract_clean_response(raw)
            bin_cc = parts['cc']
        else:
            # PAYPAL LOGIC - Run synchronous requests in a separate thread!
            bin_cc = cc.split('|')[0]
            g_name = "PayPal Braintree ($1)"
            status, raw = await asyncio.to_thread(check_paypal_cc, cc)
            resp = extract_clean_response(raw)

        async with aiohttp.ClientSession() as s:
            brand, bank, country, flag = await get_bin_info(s, bin_cc[:6])
        
        emoji = "🔥 𝐂𝐇𝐀𝐑𝐆𝐄𝐃" if status == "CHARGED" else "✅ 𝐀𝐏𝐏𝐑𝐎𝐕𝐄𝐃" if status == "APPROVED" else "❌ 𝐃𝐄𝐂𝐋𝐈𝐍𝐄𝐃"
        res_text = (
            f"<b>{emoji}</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"💳 <b>𝐂𝐚𝐫𝐝:</b> <code>{cc}</code>\nツ <b>𝐑𝐞𝐬𝐩𝐨𝐧𝐬𝐞:</b> <code>{resp}</code>\n"
            f"キ <b>𝐆𝐚𝐭𝐞𝐰𝐚𝐲:</b> {g_name}\n━━━━━━━━━━━━━━━━━━\n"
            f"零 <b>𝐁𝐚𝐧𝐤:</b> <code>{bank}</code>\n"
            f"零 <b>𝐂𝐨𝐮𝐧𝐭𝐫𝐲:</b> <code>{country} {flag}</code>\n━━━━━━━━━━━━━━━━━━\n"
            f"<i>𝐏𝐨𝐰𝐞𝐫𝐞𝐝 𝐛𝐲 𝐁𝐄𝐀𝐑 𝐎𝐒</i>"
        )
        await loading.edit_text(res_text, reply_markup=kb_back())
    except Exception as e:
        await loading.edit_text(f"❌ <b>Error: {str(e)[:50]}</b>\nCheck format: CC|MM|YYYY|CVV", reply_markup=kb_back())
    
    await state.clear()
    await state.update_data(dash_id=dash_id)

async def main():
    print("BEAR OS PRO DEPLOYED - AIOHHTP + FLASK + TELEGRAM READY")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
