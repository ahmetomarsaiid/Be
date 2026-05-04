import asyncio
import os
import random
import aiohttp
import json
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# Import the core logic directly from your UNTOUCHED files
from api import process_card_async, parse_cc_string, extract_clean_response
from shopify import get_bin_info, classify_result, approved_message, fmt_price, fmt_info

# Pull from environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID = os.getenv("ADMIN_ID") 

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

PREMIUM_FILE = "premium.json"

# --- PREMIUM DATABASE LOGIC ---
def load_premium():
    """Loads the premium users from the JSON file."""
    if os.path.exists(PREMIUM_FILE):
        with open(PREMIUM_FILE, "r") as f:
            return json.load(f)
    return {}

def save_premium(data):
    """Saves the premium users to the JSON file."""
    with open(PREMIUM_FILE, "w") as f:
        json.dump(data, f)

def is_premium(user_id):
    """Checks if a user is the admin or has an active subscription."""
    if str(user_id) == str(ADMIN_ID):
        return True # Admin always has access
        
    data = load_premium()
    uid_str = str(user_id)
    
    if uid_str in data:
        expiry = datetime.fromisoformat(data[uid_str])
        if datetime.now() < expiry:
            return True
        else:
            # Subscription expired, remove them
            del data[uid_str]
            save_premium(data)
            
    return False

# --- HELPER FUNCTIONS ---
def get_random_site():
    try:
        with open("sites.txt", "r", encoding="utf-8") as f:
            sites = [line.strip() for line in f if line.strip() and line.startswith("http")]
            if sites:
                return random.choice(sites)
    except FileNotFoundError:
        pass
    return "https://shop.spam.com" 

# --- BOT COMMANDS ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.reply(
        "🤖 <b>Shopify Checker Ready</b>\n\n"
        "Send <code>/chk CC|MM|YYYY|CVV</code> to test a card.\n"
        "Send <code>/myplan</code> to check your subscription."
    )

@dp.message(Command("myplan"))
async def cmd_myplan(message: Message):
    uid_str = str(message.from_user.id)
    if uid_str == str(ADMIN_ID):
        await message.reply("👑 <b>Admin Account</b>\nStatus: Lifetime Access")
        return
        
    data = load_premium()
    if uid_str in data:
        expiry = datetime.fromisoformat(data[uid_str])
        if datetime.now() < expiry:
            await message.reply(f"💎 <b>Premium Active</b>\nExpires on: <code>{expiry.strftime('%Y-%m-%d %H:%M:%S')}</code>")
            return
            
    await message.reply("🚫 You do not have an active premium subscription. Contact the admin to purchase access.")

@dp.message(Command("addprem"))
async def cmd_addprem(message: Message):
    # Only Admin can use this
    if str(message.from_user.id) != str(ADMIN_ID):
        return

    args = message.text.split()
    if len(args) != 3:
        await message.reply(
            "⚠️ <b>Usage:</b> <code>/addprem [user_id] [duration]</code>\n"
            "<b>Example:</b> <code>/addprem 123456789 7d</code>\n"
            "<b>Units:</b> h (hours), d (days), m (months), y (years), lifetime"
        )
        return
        
    target_id = args[1]
    duration = args[2].lower()
    
    if duration == 'lifetime':
        expiry = datetime.now() + timedelta(days=36500) # 100 years
    else:
        unit = duration[-1]
        try:
            val = int(duration[:-1])
            if unit == 'h': expiry = datetime.now() + timedelta(hours=val)
            elif unit == 'd': expiry = datetime.now() + timedelta(days=val)
            elif unit == 'm': expiry = datetime.now() + timedelta(days=val*30)
            elif unit == 'y': expiry = datetime.now() + timedelta(days=val*365)
            else: raise ValueError
        except:
            await message.reply("❌ <b>Invalid format!</b> Use numbers followed by h, d, m, y (e.g., 12h, 1m).")
            return
            
    data = load_premium()
    data[target_id] = expiry.isoformat()
    save_premium(data)
    await message.reply(f"✅ <b>Success!</b>\nUser <code>{target_id}</code> now has premium until {expiry.strftime('%Y-%m-%d')}.")

@dp.message(Command("delprem"))
async def cmd_delprem(message: Message):
    # Only Admin can use this
    if str(message.from_user.id) != str(ADMIN_ID):
        return

    args = message.text.split()
    if len(args) != 2:
        await message.reply("⚠️ <b>Usage:</b> <code>/delprem [user_id]</code>")
        return
        
    target_id = args[1]
    data = load_premium()
    
    if target_id in data:
        del data[target_id]
        save_premium(data)
        await message.reply(f"🗑️ <b>Revoked:</b> User <code>{target_id}</code> premium access has been removed.")
    else:
        await message.reply("❌ User not found in the database.")

@dp.message(Command("chk"))
async def cmd_chk(message: Message):
    # --- SUBSCRIPTION CHECK ---
    if not is_premium(message.from_user.id):
        await message.reply("⛔️ <b>Access Denied:</b> You need an active premium subscription to use this bot.")
        return

    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await message.reply("⚠️ <b>Format:</b> <code>/chk CC|MM|YYYY|CVV [optional_site_url]</code>")
        return

    cc_string = args[1]
    target_site = args[2] if len(args) == 3 else get_random_site()
    if not target_site.startswith('http'):
        target_site = 'https://' + target_site

    try:
        parts = parse_cc_string(cc_string)
    except ValueError:
        await message.reply("⚠️ <b>Invalid CC Format.</b> Use CC|MM|YYYY|CVV")
        return

    status_msg = await message.reply("⏳ <i>Processing against Shopify gateway...</i>")

    try:
        success, raw_message, gateway, price, currency = await process_card_async(
            parts['cc'], parts['mes'], parts['ano'], parts['cvv'], target_site, proxy_str=None
        )

        category = classify_result(success, raw_message)
        appr_clean = approved_message(raw_message) if category == 'approved' else None
        clean_msg = appr_clean if appr_clean else extract_clean_response(raw_message)
        
        if 'MERCHANDISE_EXPECTED_PRICE_MISMATCH' in clean_msg.upper():
            clean_msg = 'Error'
        if category == 'charged':
            clean_msg = 'ORDER_PLACED'
        elif category == 'tds':
            clean_msg = 'OTP_REQUIRED'

        async with aiohttp.ClientSession() as session:
            brand, bank, country, level, type_cc, flag = await get_bin_info(session, parts['cc'])

        price_display = fmt_price(price, currency)
        info_str = fmt_info(brand, type_cc, level)
        gateway_display = gateway if gateway else "Shopify Payments"

        if category == 'charged':
            status_emoji = "🔥 <b>CHARGED</b>"
        elif category == 'approved':
            status_emoji = "✅ <b>APPROVED</b>"
        elif category == 'tds':
            status_emoji = "❎ <b>3DS</b>"
        elif category == 'declined':
            status_emoji = "❌ <b>DECLINED</b>"
        else:
            status_emoji = "⚠️ <b>ERROR</b>"

        final_text = (
            f"ア <b>Card:</b> <code>{cc_string}</code>\n"
            f"カ <b>Status:</b> {status_emoji}\n"
            f"ツ <b>Response:</b> <code>{clean_msg}</code>\n"
            f"キ <b>Gateway:</b> {gateway_display}\n"
            f"千 <b>Price:</b> {price_display}\n"
            f"━━━━━━━━━━━━━\n"
            f"零 <b>Info:</b> {info_str}\n"
            f"零 <b>Bank:</b> {bank}\n"
            f"零 <b>Country:</b> {country} {flag}\n"
            f"━━━━━━━━━━━━━\n"
            f"🌐 <b>Site:</b> <code>{target_site}</code>\n"
            f"力 <b>Dev:</b> @Xoarch"
        )

        await status_msg.edit_text(final_text)

    except Exception as e:
        await status_msg.edit_text(f"⚠️ <b>Critical Error:</b> {str(e)}")

async def main():
    print("Bot is starting up...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
