import os
import sys
import asyncio
from telethon import TelegramClient, events
from dotenv import load_dotenv

load_dotenv()

API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
LOGGER_BOT_TOKEN = os.getenv('LOGGER_BOT_TOKEN')

class LoggerBot:
    def __init__(self):
        self.bot = TelegramClient('logger_bot', API_ID, API_HASH)

    async def start(self):
        await self.bot.start(bot_token=LOGGER_BOT_TOKEN)
        print("✓ Logger bot started and ready to receive logs")
        await self.bot.run_until_disconnected()

async def main():
    print("=" * 50)
    print("Telegram Logger Bot")
    print("=" * 50)

    if not all([API_ID, API_HASH, LOGGER_BOT_TOKEN]):
        print("❌ Error: Missing environment variables!")
        print("Required: API_ID, API_HASH, LOGGER_BOT_TOKEN")
        sys.exit(1)

    print("✓ Logger bot running...")
    bot = LoggerBot()
    await bot.start()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n✓ Logger bot stopped")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        sys.exit(1)
