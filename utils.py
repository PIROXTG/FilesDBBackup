import os
import re
import time

from pyrogram.types import BotCommand, BotCommandScopeChat

import config

RESTART_FILE = "restart.json"

LINK_RE = re.compile(
    r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$"
)


class temp:
    CURRENT = 0
    CANCEL = False
    INDEXING = False
    START_TIME = time.time()


def get_size(size) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size or 0)
    i = 0
    while size >= 1024.0 and i < len(units) - 1:
        i += 1
        size /= 1024.0
    return f"{size:.2f} {units[i]}"


def fmt_time(seconds) -> str:
    seconds = int(seconds)
    out = []
    for name, sec in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= sec or (name == "s" and not out):
            val, seconds = divmod(seconds, sec)
            out.append(f"{val}{name}")
    return " ".join(out)


def get_media(message):
    for kind in ("video", "audio", "document"):
        obj = getattr(message, kind, None)
        if obj:
            obj.message_type = kind
            return obj
    return None


def should_index(media) -> bool:
    name = str(getattr(media, "file_name", None) or "").strip().lower()
    ext = os.path.splitext(name)[1].lstrip(".")

    if ext in config.BLOCKED_EXTENSIONS:
        return False

    if config.ALLOWED_EXTENSIONS:
        if ext:
            if ext not in config.ALLOWED_EXTENSIONS:
                return False
        else:
            mime = (getattr(media, "mime_type", None) or "").lower()
            kind = getattr(media, "message_type", None)
            if not (mime.startswith("video/") or kind == "video"):
                return False

    min_size = config.MIN_FILE_SIZE_MB * 1024 * 1024
    size = getattr(media, "file_size", None)
    if min_size and size is not None and size < min_size:
        return False
    return True


async def set_commands(client):
    user_cmds = [
        BotCommand("start", "Start the bot"),
        BotCommand("help", "How to use the bot"),
        BotCommand("search", "Search files"),
    ]
    admin_cmds = user_cmds + [
        BotCommand("stats", "Database statistics"),
        BotCommand("index", "Index a channel"),
        BotCommand("setskip", "Set indexing skip position"),
        BotCommand("status", "Indexing status"),
        BotCommand("forward", "Forward files to channels"),
        BotCommand("forwardstop", "Stop forwarding"),
        BotCommand("forwardstatus", "Forwarding progress"),
        BotCommand("update", "Pull updates and restart"),
        BotCommand("restart", "Restart the bot"),
    ]
    try:
        await client.set_bot_commands(user_cmds)
    except Exception:
        pass
    for admin_id in config.ADMINS:
        try:
            await client.set_bot_commands(admin_cmds, scope=BotCommandScopeChat(admin_id))
        except Exception:
            pass  # admin hasn't started the bot yet
