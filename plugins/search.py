import asyncio
import html
import logging
import math
import secrets
from collections import OrderedDict

from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup

import config
from database.ia_filterdb import filedb
from utils import LINK_RE, get_size

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_QUERIES = OrderedDict()


def _remember(query: str) -> str:
    token = secrets.token_hex(3)
    _QUERIES[token] = query
    while len(_QUERIES) > 2000:
        _QUERIES.popitem(last=False)
    return token


async def _build_page(uid, token, query, offset):
    per = config.RESULTS_PER_PAGE
    files, total = await filedb.search_files(query, offset, per)
    if not files:
        return None, None

    pages = max(1, math.ceil(total / per))
    page = offset // per + 1

    rows = []
    for f in files:
        label = f"[{get_size(f.get('file_size'))}] {f.get('file_name') or 'Untitled'}"
        if len(label) > 60:
            label = label[:59] + "…"
        data = f"f#{uid}#{f['file_id']}"
        if len(data.encode()) <= 64:
            rows.append([Btn(label, callback_data=data)])

    nav = []
    if offset > 0:
        nav.append(Btn("⬅️ Prev", callback_data=f"pg#{uid}#{token}#{max(offset - per, 0)}"))
    nav.append(Btn(f"📄 {page}/{pages}", callback_data="noop"))
    if offset + per < total:
        nav.append(Btn("Next ➡️", callback_data=f"pg#{uid}#{token}#{offset + per}"))
    rows.append(nav)

    text = (
        f"<b>🔍 Results for:</b> <code>{html.escape(query)}</code>\n"
        f"<b>📁 Found:</b> {total} file(s)\n\n"
        f"<i>Tap a file to receive it.</i>"
    )
    return text, Markup(rows)


async def do_search(client, message, query: str):
    if not message.from_user:
        return
    private = message.chat.type == enums.ChatType.PRIVATE
    query = " ".join(query.split())[:100]

    if len(query) < config.MIN_QUERY_LENGTH:
        if private:
            await message.reply(f"Please type at least {config.MIN_QUERY_LENGTH} characters.")
        return

    uid = message.from_user.id
    token = _remember(query)
    try:
        text, markup = await _build_page(uid, token, query, 0)
    except Exception as e:
        logger.error(f"Search error: {e}", exc_info=True)
        if private:
            await message.reply("⚠️ Search failed, please try again later.")
        return

    if not text:
        if private:
            await message.reply(
                f"❌ No files found for <code>{html.escape(query)}</code>.\n"
                "Check the spelling or try fewer words."
            )
        return
    await message.reply(text, reply_markup=markup)


SEARCH_FILTER = (
    (filters.private | filters.group)
    & filters.text
    & filters.incoming
    & ~filters.forwarded
    & ~filters.bot
    & ~filters.regex(r"^/")
    & ~(filters.user(config.ADMINS) & filters.regex(LINK_RE.pattern))
)


@Client.on_message(SEARCH_FILTER)
async def search_text(client, message):
    await do_search(client, message, message.text)


@Client.on_message(filters.command("search") & (filters.private | filters.group))
async def search_command(client, message):
    if len(message.command) < 2:
        return await message.reply("Usage: <code>/search movie name</code>")
    await do_search(client, message, " ".join(message.command[1:]))


@Client.on_callback_query(filters.regex(r"^noop$"))
async def noop(client, query):
    await query.answer()


@Client.on_callback_query(filters.regex(r"^pg#"))
async def paginate(client, query):
    try:
        _, uid, token, offset = query.data.split("#")
    except ValueError:
        return await query.answer()
    if query.from_user.id != int(uid):
        return await query.answer("This is not your search.", show_alert=True)
    text_q = _QUERIES.get(token)
    if not text_q:
        return await query.answer("Search expired, please search again.", show_alert=True)

    text, markup = await _build_page(int(uid), token, text_q, int(offset))
    if not text:
        return await query.answer("No more results.", show_alert=True)
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep(e.value)
    await query.answer()


async def _delete_later(message, seconds):
    await asyncio.sleep(seconds)
    try:
        await message.delete()
    except Exception:
        pass


@Client.on_callback_query(filters.regex(r"^f#"))
async def send_file(client, query):
    try:
        _, uid, file_id = query.data.split("#", 2)
    except ValueError:
        return await query.answer()
    if query.from_user.id != int(uid):
        return await query.answer("This is not your search.", show_alert=True)

    doc = await filedb.get_file(file_id)
    if not doc:
        return await query.answer("File not found in the database.", show_alert=True)

    await query.answer("📤 Sending file...")

    name = doc.get("file_name") or "File"
    caption = doc.get("caption") or f"<b>{html.escape(name)}</b>"
    if len(caption) > 1000:
        caption = f"<b>{html.escape(name)}</b>"

    chat_id = query.message.chat.id
    sent = None
    for cap in (caption, f"<b>{html.escape(name)}</b>"):
        try:
            sent = await client.send_cached_media(
                chat_id=chat_id,
                file_id=file_id,
                caption=cap,
                protect_content=config.PROTECT_CONTENT,
            )
            break
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception as e:
            logger.error(f"send_cached_media failed: {e}")

    if sent is None:
        return await client.send_message(chat_id, "❌ Could not send this file. It may no longer be available.")
    if config.AUTO_DELETE_SECONDS:
        asyncio.create_task(_delete_later(sent, config.AUTO_DELETE_SECONDS))
