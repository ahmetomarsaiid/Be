import asyncio
import os
import secrets
import string
import json
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest

# --- IMPORT YOUR EXISTING BACKEND ---
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

# --- DATABASE LOGIC ---
def load_db(filename):
    if os.path.exists(filename):
        with open(filename, "r") as f: return json.load(f)
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

# --- FINITE STATE MACHINE (FSM) ---
class AppStates(StatesGroup):
    waiting_for_card = State()
    waiting_for_key = State()

# --- DYNAMIC UI MENUS ---
def kb_home(user_id):
    is_admin = str(user_id) == str(ADMIN_ID)
    
    # Everyone sees these base buttons
    kb = [
        [InlineKeyboardButton(text="💳 Single Check", callback_data="ui_single"),
         InlineKeyboardButton(text="📁 Mass Check", callback_data="ui_mass")],
        [InlineKeyboardButton(text="👤 My Profile", callback_data="ui_plan"),
         InlineKeyboardButton(text="🔑 Redeem Key", callback_data="ui_redeem")]
    ]
    
    # ONLY the Admin sees this extra row
    if is_admin:
        kb.append([InlineKeyboardButton(text="⚙️ Admin Control Panel", callback_data="ui_admin")])
        
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Return to Dashboard", callback_data="ui_home")]
    ])

def kb_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎟 Gen 1-Day Key", callback_data="gen_1d"),
         InlineKeyboardButton(text="🎟 Gen 7-Day Key", callback_data="gen_7d")],
        [InlineKeyboardButton(text="🎟 Gen 1-Month Key", callback_data="gen_30d"),
         InlineKeyboardButton(text="🎟 Gen Lifetime Key", callback_data="gen_life")],
        [InlineKeyboardButton(text="🔙 Return to Dashboard", callback_data="ui_home")]
    ])

async def clean_chat(message: Message):
    """Silently deletes user inputs to keep the chat looking like a clean app."""
    try: await message.delete()
    except TelegramBadRequest: pass

# --- MAIN DASHBOARD ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await clean_chat(message)
    
    tier = check_tier(message.from_user.id)
    text = (
        f"⚡️ <b>NEXUS CHECKER OS</b> ⚡️\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"Welcome, <b>{message.from_user.first_name}</b>.\n"
        f"Access Level: {tier}\n\n"
        f"<i>Select a module to deploy:</i>"
    )
    await message.answer(text, reply_markup=kb_home(message.from_user.id))

@router.callback_query(F.data == "ui_home")
async def nav_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    tier = check_tier(callback.from_user.id)
    text = (
        f"⚡️ <b>NEXUS CHECKER OS</b> ⚡️\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"Welcome, <b>{callback.from_user.first_name}</b>.\n"
        f"Access Level: {tier}\n\n"
        f"<i>Select a module to deploy:</i>"
    )
    await callback.message.edit_text(text, reply_markup=kb_home(callback.from_user.id))
    await callback.answer()

# --- PREMIUM FEATURE GATES ---
@router.callback_query(F.data == "ui_single")
async def nav_single(callback: CallbackQuery, state: FSMContext):
    # THE PREMIUM GATEKEEPER
    if check_tier(callback.from_user.id) == "🆓 FREE":
        # Throws a native Telegram pop-up instead of a chat message!
        return await callback.answer("⛔️ PREMIUM EXCLUSIVE!\n\nYou must redeem a key to use the checker modules.", show_alert=True)
        
    await callback.message.edit_text(
        "💳 <b>SINGLE CARD TERMINAL</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        "Please paste your card details below.\n"
        "Format: <code>CC|MM|YYYY|CVV</code>",
        reply_markup=kb_back()
    )
    await state.set_state(AppStates.waiting_for_card)
    await callback.answer()

@router.callback_query(F.data == "ui_mass")
async def nav_mass(callback: CallbackQuery):
    # THE PREMIUM GATEKEEPER
    if check_tier(callback.from_user.id) == "🆓 FREE":
        return await callback.answer("⛔️ PREMIUM EXCLUSIVE!\n\nYou must redeem a key to use the mass checker.", show_alert=True)
        
    await callback.message.edit_text(
        "📁 <b>MASS CHECKER</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        "<i>Mass check module is currently initializing. Please use the Single Check terminal.</i>",
        reply_markup=kb_back()
    )
    await callback.answer()

# --- SINGLE CHECKER LOGIC ---
@router.message(AppStates.waiting_for_card)
async def process_card(message: Message, state: FSMContext):
    await clean_chat(message)
    card_data = message.text.strip()
    
    try: parts = parse_cc_string(card_data)
    except ValueError:
        err = await message.answer("⚠️ <b>Invalid Format.</b> Use CC|MM|YYYY|CVV")
        await asyncio.sleep(2)
        await err.delete()
        return

    loading = await message.answer(
        f"⏳ <b>AUTHORIZING CONNECTION...</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Target: <code>{card_data}</code>\n"
        f"Status: <i>Pinging Shopify Gateway...</i>",
        reply_markup=kb_back()
    )

    try:
        # PINGING YOUR API.PY
        success, raw_message, gateway, price, currency = await process_card_async(
            parts['cc'], parts['mes'], parts['ano'], parts['cvv'], "https://shop.spam.com", proxy_str=None
        )

        category = classify_result(success, raw_message)
        clean_msg = extract_clean_response(raw_message)
        if category == 'charged': clean_msg = 'ORDER_PLACED'

        status_emoji = "🔥 <b>CHARGED</b>" if category == 'charged' else "✅ <b>APPROVED</b>" if category == 'approved' else "❌ <b>DECLINED</b>"

        result_text = (
            f"{status_emoji}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💳 <b>Card:</b> <code>{card_data}</code>\n"
            f"ツ <b>Response:</b> <code>{clean_msg}</code>\n"
            f"キ <b>Gateway:</b> {gateway or 'Shopify'}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>Powered by Nexus OS</i>"
        )
        await loading.edit_text(result_text, reply_markup=kb_back())
        
    except Exception as e:
        await loading.edit_text(f"⚠️ <b>Fatal Error:</b> {str(e)}", reply_markup=kb_back())

    await state.clear()

# --- KEY REDEMPTION ---
@router.callback_query(F.data == "ui_redeem")
async def nav_redeem(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🔑 <b>KEY REDEMPTION</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        "Please paste your license key in the chat.",
        reply_markup=kb_back()
    )
    await state.set_state(AppStates.waiting_for_key)
    await callback.answer()

@router.message(AppStates.waiting_for_key)
async def process_key(message: Message, state: FSMContext):
    await clean_chat(message)
    key_input = message.text.strip().upper()
    
    keys_db = load_db(KEYS_FILE)
    if key_input not in keys_db:
        warning = await message.answer("❌ <b>Invalid or Expired Key.</b>")
        await asyncio.sleep(2)
        await warning.delete()
        return

    duration = keys_db[key_input]
    if duration == 'life': expiry = datetime.now() + timedelta(days=36500)
    elif duration.endswith('d'): expiry = datetime.now() + timedelta(days=int(duration[:-1]))
    
    prem_db = load_db(PREMIUM_FILE)
    prem_db[str(message.from_user.id)] = expiry.isoformat()
    save_db(PREMIUM_FILE, prem_db)
    
    del keys_db[key_input]
    save_db(KEYS_FILE, keys_db)
    
    success = await message.answer(f"🎉 <b>Redeemed!</b> You have Premium until {expiry.strftime('%Y-%m-%d')}")
    await state.clear()
    
    # Auto-refresh the dashboard so their tier updates
    await message.answer(
        f"⚡️ <b>NEXUS CHECKER OS</b> ⚡️\n━━━━━━━━━━━━━━━━━━\n\nAccess Level: 💎 PREMIUM\n\n<i>Select a module to deploy:</i>",
        reply_markup=kb_home(message.from_user.id)
    )
    await asyncio.sleep(3)
    await success.delete()

# --- PROFILE & ADMIN ---
@router.callback_query(F.data == "ui_plan")
async def nav_plan(callback: CallbackQuery):
    tier = check_tier(callback.from_user.id)
    db = load_db(PREMIUM_FILE)
    
    if str(callback.from_user.id) == str(ADMIN_ID): expiry_text = "Never (Admin Override)"
    elif str(callback.from_user.id) in db:
        expiry = datetime.fromisoformat(db[str(callback.from_user.id)])
        expiry_text = expiry.strftime('%Y-%m-%d %H:%M:%S')
    else: expiry_text = "N/A"

    await callback.message.edit_text(
        f"👤 <b>USER PROFILE</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Telegram ID:</b> <code>{callback.from_user.id}</code>\n"
        f"<b>Access Level:</b> {tier}\n"
        f"<b>Expires On:</b> <code>{expiry_text}</code>\n\n"
        f"<i>To upgrade, click Redeem Key or contact the admin.</i>",
        reply_markup=kb_back()
    )
    await callback.answer()

@router.callback_query(F.data == "ui_admin")
async def nav_admin(callback: CallbackQuery):
    if str(callback.from_user.id) != str(ADMIN_ID): return await callback.answer("⛔️ Unauthorized.", show_alert=True)
    await callback.message.edit_text(
        "⚙️ <b>ADMIN TERMINAL</b>\n━━━━━━━━━━━━━━━━━━\n\nGenerate access keys below:",
        reply_markup=kb_admin()
    )
    await callback.answer()

@router.callback_query(F.data.startswith("gen_"))
async def admin_gen(callback: CallbackQuery):
    if str(callback.from_user.id) != str(ADMIN_ID): return
    duration = callback.data.split("_")[1]
    
    new_key = "NEXUS-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12))
    
    db = load_db(KEYS_FILE)
    db[new_key] = duration
    save_db(KEYS_FILE, db)
    
    await callback.message.edit_text(
        f"🎟 <b>LICENSE CREATED</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Key:</b> <code>{new_key}</code>\n"
        f"<b>Duration:</b> {duration.upper()}\n\n"
        f"<i>Tap the key to copy it. Send it to your user.</i>",
        reply_markup=kb_back()
    )
    await callback.answer()

async def main():
    print("Nexus OS Backend Online...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
