import asyncio
import os
import random
import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# Import the core logic directly from your UNTOUCHED files
from api import process_card_async, parse_cc_string, extract_clean_response
from shopify import get_bin_info, classify_result, approved_message, fmt_price, fmt_info

# Pull from environment variables for easy Railway deployment
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

def get_random_site():
    """Pulls a random site from your sites.txt file"""
    try:
        with open("sites.txt", "r", encoding="utf-8") as f:
            sites = [line.strip() for line in f if line.strip() and line.startswith("http")]
            if sites:
                return random.choice(sites)
    except FileNotFoundError:
        pass
    return "https://shop.spam.com" # Fallback just in case

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.reply(
        "🤖 <b>Shopify Checker Ready</b>\n\n"
        "Send <code>/chk CC|MM|YYYY|CVV</code> to test a card."
    )

@dp.message(Command("chk"))
async def cmd_chk(message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await message.reply("⚠️ <b>Format:</b> <code>/chk CC|MM|YYYY|CVV [optional_site_url]</code>")
        return

    cc_string = args[1]
    
    # Use the site they provided, or pick one randomly from sites.txt
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
        # 1. Run the check (Logic from api.py)
        success, raw_message, gateway, price, currency = await process_card_async(
            parts['cc'], parts['mes'], parts['ano'], parts['cvv'], target_site, proxy_str=None
        )

        # 2. Classify the result (Logic from shopify.py)
        category = classify_result(success, raw_message)
        appr_clean = approved_message(raw_message) if category == 'approved' else None
        clean_msg = appr_clean if appr_clean else extract_clean_response(raw_message)
        
        if 'MERCHANDISE_EXPECTED_PRICE_MISMATCH' in clean_msg.upper():
            clean_msg = 'Error'
        if category == 'charged':
            clean_msg = 'ORDER_PLACED'
        elif category == 'tds':
            clean_msg = 'OTP_REQUIRED'

        # 3. Fetch BIN Data (Logic from shopify.py)
        async with aiohttp.ClientSession() as session:
            brand, bank, country, level, type_cc, flag = await get_bin_info(session, parts['cc'])

        # 4. Format values (Logic from shopify.py)
        price_display = fmt_price(price, currency)
        info_str = fmt_info(brand, type_cc, level)
        gateway_display = gateway if gateway else "Shopify Payments"

        # 5. Build the final output identical to your CLI
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
