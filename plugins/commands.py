import asyncio
import html
import json
import logging
import os
import sys
import time

from pyrogram import Client, enums, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup

import config
from Script import script
from database.ia_filterdb import filedb
from utils import RESTART_FILE, fmt_time, get_size, temp

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

STATUS_LABEL = {
    "active": "🟢 Active",
    "standby": "🟡 Standby",
    "full": "🔴 Full",
    "offline": "⚫ Offline",
}


def _help_text(user_id):
    text = script.HELP_TXT
    if user_id in config.ADMINS:
        text += script.ADMIN_HELP_TXT
    return text


def _start_markup():
    rows = [[Btn("📚 Help", callback_data="help_cb")]]
    links = []
    if config.UPDATES_LINK:
        links.append(Btn("📢 Updates", url=config.UPDATES_LINK))
    if config.SUPPORT_LINK:
        links.append(Btn("👤 Support", url=config.SUPPORT_LINK))
    if links:
        rows.append(links)
    if config.REPO_LINK:
        rows.append([Btn("⭐ Source Code", url=config.REPO_LINK)])
    return Markup(rows)


@Client.on_message(filters.command("start") & filters.incoming)
async def start_command(client, message):
    if message.chat.type != enums.ChatType.PRIVATE:
        return await message.reply(script.GROUP_START_TXT)
    user = message.from_user.mention if message.from_user else "there"
    await message.reply(script.START_TXT.format(user=user), reply_markup=_start_markup())


@Client.on_message(filters.command("help") & filters.incoming)
async def help_command(client, message):
    uid = message.from_user.id if message.from_user else 0
    await message.reply(_help_text(uid))


@Client.on_callback_query(filters.regex(r"^help_cb$"))
async def help_cb(client, query):
    try:
        await query.message.edit_text(
            _help_text(query.from_user.id),
            reply_markup=Markup([[Btn("⬅️ Back", callback_data="back_start")]]),
        )
    except MessageNotModified:
        pass
    await query.answer()


@Client.on_callback_query(filters.regex(r"^back_start$"))
async def back_start(client, query):
    try:
        await query.message.edit_text(
            script.START_TXT.format(user=query.from_user.mention), reply_markup=_start_markup()
        )
    except MessageNotModified:
        pass
    await query.answer()


@Client.on_message(filters.command("stats") & filters.user(config.ADMINS))
async def stats_command(client, message):
    msg = await message.reply("📊 Collecting stats...")
    rows = await filedb.get_stats()

    lines = ["<b>📊 Database Stats</b>\n"]
    total = 0
    for r in rows:
        total += r["files"]
        lines.append(
            f"<b>DB {r['number']}</b> — {STATUS_LABEL[r['status']]}\n"
            f"├ Files: <code>{r['files']}</code>\n"
            f"└ Used: <code>{get_size(r['size'])} / {get_size(r['limit'])}</code> ({r['percent']:.1f}%)\n"
        )
    if not rows:
        lines.append("No MongoDB URI configured.\n")

    indexing = f"Running (#{temp.CURRENT})" if temp.INDEXING else "Idle"
    lines.append(f"<b>📁 Total files:</b> <code>{total}</code>")
    lines.append(f"<b>🗄 Databases:</b> {len(rows)}")
    lines.append(f"<b>⚙️ Indexing:</b> {indexing}")
    lines.append(f"<b>⏱ Uptime:</b> {fmt_time(time.time() - temp.START_TIME)}")
    await msg.edit_text("\n".join(lines))


def _restart_process():
    os.execl(sys.executable, sys.executable, *sys.argv)


async def _run(*cmd):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="ignore").strip()


def _save_restart_marker(msg):
    try:
        with open(RESTART_FILE, "w") as f:
            json.dump({"chat_id": msg.chat.id, "message_id": msg.id}, f)
    except Exception as e:
        logger.warning(f"Could not write restart marker: {e}")


@Client.on_message(filters.command("restart") & filters.user(config.ADMINS))
async def restart_command(client, message):
    msg = await message.reply("🔄 Restarting...")
    _save_restart_marker(msg)
    _restart_process()


@Client.on_message(filters.command("update") & filters.user(config.ADMINS))
async def update_command(client, message):
    msg = await message.reply("🔍 Checking for updates...")
    code, out = await _run("git", "pull")
    if code != 0:
        return await msg.edit_text(f"❌ Update failed:\n<code>{html.escape(out[-800:])}</code>")
    if "Already up to date" in out or "Already up-to-date" in out:
        return await msg.edit_text("✅ Already up to date.")

    await msg.edit_text("📦 Updated. Installing requirements...")
    code, pip_out = await _run(sys.executable, "-m", "pip", "install", "-r", "requirements.txt")
    if code != 0:
        return await msg.edit_text(f"⚠️ Pip install failed:\n<code>{html.escape(pip_out[-800:])}</code>")

    await msg.edit_text(f"✅ Updated:\n<code>{html.escape(out[-500:])}</code>\n\n🔄 Restarting...")
    _save_restart_marker(msg)
    _restart_process()