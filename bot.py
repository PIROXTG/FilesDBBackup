import json
import logging
import os

from pyrogram import Client

import config
from database.forward_db import forward_db
from database.ia_filterdb import filedb
from utils import RESTART_FILE, set_commands, temp

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(name)s][%(lineno)d][%(levelname)s] -> %(message)s",
    force=True,
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)


class Bot(Client):
    def __init__(self):
        os.makedirs("sessions", exist_ok=True)
        super().__init__(
            name="FileSaverBot",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            bot_token=config.BOT_TOKEN,
            plugins=dict(root="plugins"),
            workdir="sessions",
            workers=50,
            max_concurrent_transmissions=10,
        )

    async def start(self, *args, **kwargs):
        if not await filedb.init():
            logging.error("❌ No MongoDB URI is reachable - check config.FILE_URIS")
        if config.PROGRESS_URI and not await forward_db.init():
            logging.error("❌ Progress MongoDB is unreachable - check config.PROGRESS_URI")
        await super().start(*args, **kwargs)

        me = await self.get_me()
        temp.U_NAME = me.username
        await set_commands(self)
        logging.info(f"@{me.username} started")
        await self._notify_started(me)

    async def stop(self, *args, **kwargs):
        await super().stop(*args, **kwargs)
        await filedb.close()
        await forward_db.close()
        logging.info("Bot stopped")

    async def _notify_started(self, me):
        if os.path.exists(RESTART_FILE):
            try:
                with open(RESTART_FILE) as f:
                    data = json.load(f)
                await self.edit_message_text(data["chat_id"], data["message_id"], "✅ Restarted successfully!")
            except Exception as e:
                logging.warning(f"Could not edit restart message: {e}")
            finally:
                try:
                    os.remove(RESTART_FILE)
                except OSError:
                    pass
            return
        for admin_id in config.ADMINS:
            try:
                await self.send_message(admin_id, f"✅ @{me.username} started.")
            except Exception:
                pass


if __name__ == "__main__":
    Bot().run()