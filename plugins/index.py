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

_lock = asyncio.Lock()

CANCEL_MARKUP = InlineKeyboardMarkup(
    [[InlineKeyboardButton("❌ Cancel", callback_data="index_cancel")]]
)


def _forward_info(message):
    chat = getattr(message, "forward_from_chat", None)
    msg_id = getattr(message, "forward_from_message_id", None)
    if chat and msg_id:
        return chat, msg_id
    origin = getattr(message, "forward_origin", None)
    if origin:
        chat = getattr(origin, "chat", None)
        msg_id = getattr(origin, "message_id", None)
        if chat and msg_id:
            return chat, msg_id
    return None, None


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
    chat_id = last_id = None
    if message.text and not message.forward_date:
        chat_id, last_id = _parse_link(message.text)
        if not chat_id:
            return await message.reply("Invalid link.")
    else:
        chat, last_id = _forward_info(message)
        if chat is None or getattr(chat, "type", None) != enums.ChatType.CHANNEL:
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


async def _fetch(bot, chat, ids, retries=3):
    for attempt in range(retries):
        try:
            return await asyncio.wait_for(bot.get_messages(chat_id=chat, message_ids=ids), timeout=45)
        except FloodWait as e:
            logger.warning(f"FloodWait {e.value}s while indexing")
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            logger.warning(f"get_messages failed ({attempt + 1}/{retries}): {e}")
            await asyncio.sleep(3 * (attempt + 1))
    return None


async def index_files_to_db(last_msg_id, chat, msg, bot):
    s = dict(saved=0, dup=0, err=0, deleted=0, no_media=0, skipped=0)
    start = last_edit = time.time()

    async with _lock:
        temp.INDEXING = True
        temp.CANCEL = False
        current = temp.CURRENT
        start_pos = current
        try:
            while current < last_msg_id and not temp.CANCEL:
                ids = list(range(current + 1, min(current + config.INDEX_BATCH_SIZE, last_msg_id) + 1))
                messages = await _fetch(bot, chat, ids)
                if messages is None:
                    s["err"] += len(ids)
                    current += len(ids)
                    temp.CURRENT = current
                    continue

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

                current += len(ids)
                temp.CURRENT = current

                if time.time() - last_edit >= 8:
                    last_edit = time.time()
                    done = current - start_pos
                    elapsed = max(time.time() - start, 1)
                    speed = done / elapsed
                    eta = (last_msg_id - current) / speed if speed else 0
                    try:
                        await msg.edit_text(
                            f"📊 <b>Indexing...</b> {current}/{last_msg_id} "
                            f"({current / last_msg_id * 100:.1f}%)\n"
                            f"⚡ {speed:.1f} msg/s | 🕒 ETA {fmt_time(eta)}\n\n"
                            f"📥 Saved: {s['saved']} | 🔁 Duplicate: {s['dup']}\n"
                            f"⏭ Skipped: {s['skipped']} | 🗑 Deleted: {s['deleted']} | ⚠️ Errors: {s['err']}",
                            reply_markup=CANCEL_MARKUP,
                        )
                    except FloodWait as e:
                        await asyncio.sleep(e.value)
                    except Exception:
                        pass

                await asyncio.sleep(config.INDEX_DELAY)

            cancelled = temp.CANCEL
            if not cancelled:
                temp.CURRENT = 0  # finished: next /index starts fresh
            title = f"⛔ <b>Indexing cancelled</b> at #{current}" if cancelled else "✅ <b>Indexing completed!</b>"
            await msg.edit_text(
                f"{title}\n\n"
                f"⏱ Time: {fmt_time(time.time() - start)}\n"
                f"📥 Saved: {s['saved']}\n🔁 Duplicate: {s['dup']}\n"
                f"⏭ Skipped (filtered): {s['skipped']}\n📭 Non-media: {s['no_media']}\n"
                f"🗑 Deleted: {s['deleted']}\n⚠️ Errors: {s['err']}"
            )
        except Exception as e:
            logger.exception("Indexing failed")
            try:
                await msg.edit_text(f"❌ Indexing failed: {str(e)[:200]}")
            except Exception:
                pass
        finally:
            temp.INDEXING = False
            temp.CANCEL = False
