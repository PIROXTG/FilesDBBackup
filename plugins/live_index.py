import asyncio
import logging

from pyrogram import Client, filters
from pyrogram.errors import FloodWait

import config
from database.ia_filterdb import filedb
from utils import get_media, should_index

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

media_filter = filters.document | filters.video | filters.audio

_queue = None
_worker = None


async def _queue_worker():
    while True:
        media = await _queue.get()
        try:
            saved, status = await filedb.save_file(media)
            name = getattr(media, "file_name", None)
            if saved:
                logger.info(f"Live indexed: {name}")
            elif status == 0:
                logger.info(f"Live index duplicate: {name}")
            else:
                logger.error(f"Live index failed: {name}")
        except Exception as e:
            logger.error(f"Live index worker error: {e}")
        finally:
            _queue.task_done()
        await asyncio.sleep(0.05)


@Client.on_message(filters.chat(config.CHANNELS) & media_filter)
async def live_index(bot, message):
    global _queue, _worker

    if _queue is None:
        _queue = asyncio.Queue(maxsize=2000)
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_queue_worker())

    try:
        media = get_media(message)
        if media is None:
            return
        media.caption = message.caption
        if not should_index(media):
            logger.info(f"Live index skipped: {getattr(media, 'file_name', None)}")
            return
        await _queue.put(media)
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception as e:
        logger.error(f"live_index error on message {message.id}: {e}", exc_info=True)
