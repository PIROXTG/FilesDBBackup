import asyncio
import logging
import time

from pyrogram import Client, enums, filters
from pyrogram.errors import ChannelInvalid, FloodWait, UsernameInvalid, UsernameNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
from database.ia_filterdb import filedb
from utils import LINK_RE, fmt_time, get_media, should_index, temp

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BATCH_SIZE = 50
START_DELAY = 1.5
MIN_DELAY = 1.0
MAX_DELAY = 20.0
PREFETCH = 1
SPEED_UP_AFTER = 10
SLOW_CALL = 12.0
FETCH_TIMEOUT = 300
MAX_RETRIES = 5
UI_EDIT_INTERVAL = 10

_lock = asyncio.Lock()

CANCEL_MARKUP = InlineKeyboardMarkup(
    [[InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")]]
)


def _forward_info(message):
    origin = getattr(message, "forward_origin", None)
    if origin:
        return getattr(origin, "chat", None), getattr(origin, "message_id", None)
    return (
        getattr(message, "forward_from_chat", None),
        getattr(message, "forward_from_message_id", None),
    )


def _parse_link(text: str):
    match = LINK_RE.match(text.strip())
    if not match:
        return None, None
    chat_id = match.group(4)
    if chat_id.isnumeric():
        chat_id = int("-100" + chat_id)
    return chat_id, int(match.group(5))


async def _start_indexing(bot, message, chat_id, last_msg_id):
    if _lock.locked():
        return await message.reply("⏳ An indexing process is already running. Wait for it or cancel it.")

    try:
        await bot.get_chat(chat_id)
    except ChannelInvalid:
        return await message.reply("This may be a private channel/group. Make me an admin there first.")
    except (UsernameInvalid, UsernameNotModified):
        return await message.reply("Invalid link specified.")
    except Exception as e:
        logger.exception("get_chat failed")
        return await message.reply(f"Error: {e}")

    try:
        last = await bot.get_messages(chat_id, last_msg_id)
    except Exception:
        return await message.reply("Make sure I am an admin in the channel, especially if it is private.")

    if last.empty:
        return await message.reply("I can't read that message. Am I an admin of that chat?")

    progress = await message.reply("🚀 Starting indexing...", reply_markup=CANCEL_MARKUP)
    await index_files_to_db(last_msg_id, chat_id, progress, bot)


@Client.on_message(filters.command("index") & filters.user(config.ADMINS))
async def index_command(bot, message):
    if len(message.command) < 2:
        return await message.reply(
            "Usage: <code>/index https://t.me/channel_name/123</code>\n"
            "(the number is the LAST message id to index up to)"
        )
    chat_id, last_id = _parse_link(message.command[1])
    if not chat_id:
        return await message.reply("Invalid link format.")
    await _start_indexing(bot, message, chat_id, last_id)


@Client.on_message(
    filters.private
    & filters.incoming
    & filters.user(config.ADMINS)
    & (filters.forwarded | (filters.text & filters.regex(LINK_RE.pattern)))
)
async def index_from_link_or_forward(bot, message):
    if message.text and getattr(message, "forward_origin", None) is None:
        chat_id, last_id = _parse_link(message.text)
        if not chat_id:
            return await message.reply("Invalid link.")
    else:
        chat, last_id = _forward_info(message)
        if chat is None or last_id is None:
            return
        if getattr(chat, "type", None) != enums.ChatType.CHANNEL:
            return
        chat_id = chat.username or chat.id
    await _start_indexing(bot, message, chat_id, last_id)


@Client.on_callback_query(filters.regex(r"^index_cancel$") & filters.user(config.ADMINS))
async def index_cancel(bot, query):
    temp.CANCEL = True
    await query.answer("Cancelling indexing...")


@Client.on_message(filters.command("setskip") & filters.user(config.ADMINS))
async def set_skip(bot, message):
    if len(message.command) < 2:
        return await message.reply("Usage: <code>/setskip 1000</code>")
    try:
        temp.CURRENT = max(0, int(message.command[1]))
    except ValueError:
        return await message.reply("Skip number must be an integer.")
    await message.reply(f"✅ Indexing will start after message #{temp.CURRENT}")


@Client.on_message(filters.command("status") & filters.user(config.ADMINS))
async def indexing_status(bot, message):
    if _lock.locked():
        await message.reply(
            f"🔄 Indexing in progress\n📍 Position: {temp.CURRENT}\n"
            f"⏹ Cancel requested: {'Yes' if temp.CANCEL else 'No'}"
        )
    else:
        await message.reply(f"✅ No indexing running.\n📍 Next start position: {temp.CURRENT}")


class _Pace:
    def __init__(self):
        self.delay = START_DELAY
        self.clean = 0
        self.floods = 0

    def throttle(self):
        self.floods += 1
        self.clean = 0
        self.delay = min(MAX_DELAY, max(self.delay * 1.3, START_DELAY))

    def success(self):
        self.clean += 1
        if self.clean >= SPEED_UP_AFTER:
            self.clean = 0
            self.delay = max(MIN_DELAY, self.delay * 0.9)


async def _sleep(seconds):
    end = time.monotonic() + seconds
    while not temp.CANCEL:
        left = end - time.monotonic()
        if left <= 0:
            return
        await asyncio.sleep(min(1, left))


async def _fetch(bot, chat, ids, pace):
    attempt = 0
    while attempt < MAX_RETRIES:
        if temp.CANCEL:
            return None
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                bot.get_messages(chat_id=chat, message_ids=ids), timeout=FETCH_TIMEOUT
            )
        except FloodWait as e:
            pace.throttle()
            logger.warning(f"FloodWait {e.value}s (delay {pace.delay:.1f}s)")
            await _sleep(e.value + 1)
            continue
        except asyncio.TimeoutError:
            attempt += 1
            pace.throttle()
            logger.warning(f"get_messages timed out ({attempt}/{MAX_RETRIES}, delay {pace.delay:.1f}s)")
            continue
        except Exception as e:
            attempt += 1
            logger.warning(f"get_messages failed ({attempt}/{MAX_RETRIES}): {e}")
            await _sleep(2 * attempt)
            continue

        if time.monotonic() - started >= SLOW_CALL:
            pace.throttle()
            logger.info(f"Slow response, pacing adjusted (delay {pace.delay:.1f}s)")
        else:
            pace.success()
        return result
    return None


async def _producer(bot, chat, start, last_msg_id, queue, pace, state):
    pos = start
    try:
        while pos < last_msg_id and not temp.CANCEL:
            await _sleep(pace.delay)
            if temp.CANCEL:
                break
            ids = list(range(pos + 1, min(pos + BATCH_SIZE, last_msg_id) + 1))
            messages = await _fetch(bot, chat, ids, pace)
            await queue.put((ids[-1], len(ids), messages))
            pos = ids[-1]
    except asyncio.CancelledError:
        raise
    except Exception as e:
        state["error"] = e
    await queue.put(None)


async def _next_item(queue):
    while not temp.CANCEL:
        try:
            return await asyncio.wait_for(queue.get(), timeout=1)
        except asyncio.TimeoutError:
            continue
    return None


async def _process_batch(messages, s):
    tasks = []
    for m in messages:
        if not m or m.empty:
            s["deleted"] += 1
        elif not m.media:
            s["no_media"] += 1
        else:
            media = get_media(m)
            if media is None:
                s["skipped"] += 1
                continue
            media.caption = m.caption
            if should_index(media):
                tasks.append(filedb.save_file(media))
            else:
                s["skipped"] += 1

    if tasks:
        for res in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(res, Exception):
                s["err"] += 1
            else:
                saved, status = res
                if saved:
                    s["saved"] += 1
                elif status == 0:
                    s["dup"] += 1
                else:
                    s["err"] += 1


async def index_files_to_db(last_msg_id, chat, msg, bot):
    s = dict(saved=0, dup=0, err=0, deleted=0, no_media=0, skipped=0)
    start = last_edit = time.time()

    async with _lock:
        temp.INDEXING = True
        temp.CANCEL = False
        current = start_pos = temp.CURRENT
        pace = _Pace()
        state = {}
        queue = asyncio.Queue(maxsize=PREFETCH)
        producer = asyncio.create_task(
            _producer(bot, chat, start_pos, last_msg_id, queue, pace, state)
        )

        try:
            while True:
                item = await _next_item(queue)
                if item is None:
                    break

                batch_last, count, messages = item

                if messages is None:
                    if temp.CANCEL:
                        break
                    s["err"] += count
                else:
                    await _process_batch(messages, s)

                current = batch_last
                temp.CURRENT = current

                if temp.CANCEL:
                    break

                if time.time() - last_edit >= UI_EDIT_INTERVAL:
                    last_edit = time.time()
                    done = current - start_pos
                    speed = done / max(time.time() - start, 1)
                    eta = (last_msg_id - current) / speed if speed else 0
                    try:
                        await msg.edit_text(
                            f"📊 <b>Indexing...</b> {current}/{last_msg_id} "
                            f"({current / last_msg_id * 100:.1f}%)\n"
                            f"⚡ {speed:.0f} msg/s | 🕒 ETA {fmt_time(eta)}\n\n"
                            f"📥 Saved: {s['saved']} | 🔁 Duplicate: {s['dup']}\n"
                            f"⏭ Skipped: {s['skipped']} | 🗑 Deleted: {s['deleted']} | ⚠️ Errors: {s['err']}",
                            reply_markup=CANCEL_MARKUP,
                        )
                    except FloodWait as e:
                        await asyncio.sleep(e.value)
                    except Exception:
                        pass

            if state.get("error"):
                raise state["error"]

            cancelled = temp.CANCEL
            if not cancelled:
                temp.CURRENT = 0

            elapsed = max(time.time() - start, 1)
            title = f"⛔ <b>Indexing cancelled</b> at #{current}" if cancelled else "✅ <b>Indexing completed!</b>"
            await msg.edit_text(
                f"{title}\n\n"
                f"⏱ Time: {fmt_time(elapsed)} ({(current - start_pos) / elapsed:.0f} msg/s)\n"
                f"📥 Saved: {s['saved']}\n🔁 Duplicate: {s['dup']}\n"
                f"⏭ Skipped (filtered): {s['skipped']}\n📭 Non-media: {s['no_media']}\n"
                f"🗑 Deleted: {s['deleted']}\n⚠️ Errors: {s['err']}\n"
                f"🌊 Throttles hit: {pace.floods}"
            )
        except Exception as e:
            logger.exception("Indexing failed")
            try:
                await msg.edit_text(f"❌ Indexing failed at #{current}: {str(e)[:200]}")
            except Exception:
                pass
        finally:
            producer.cancel()
            temp.INDEXING = False
            temp.CANCEL = False
