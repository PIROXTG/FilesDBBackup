import asyncio
import html
import logging
import os
import re
import time

from pyrogram import Client, enums, filters
from pyrogram.errors import (
    ChannelInvalid,
    ChannelPrivate,
    ChatAdminRequired,
    ChatWriteForbidden,
    FloodWait,
    MessageNotModified,
    PeerIdInvalid,
    UserBannedInChannel,
)

import config
from database.forward_db import forward_db
from database.ia_filterdb import filedb
from utils import fmt_time

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BATCH_SIZE = 50
FETCH_RETRIES = 5
FLOOD_RETRIES = 5
CAPTION_LIMIT = 1000

OK, BAD_CHANNEL, BAD_FILE = range(3)
CHANNEL_ERRORS = (
    ChannelInvalid,
    ChannelPrivate,
    ChatAdminRequired,
    ChatWriteForbidden,
    PeerIdInvalid,
    UserBannedInChannel,
)

USAGE = (
    "<b>Usage</b>\n"
    "<code>/forward [skip] channel_id1 channel_id2 ...</code>\n"
    "<code>/forward</code> - resume the saved job"
)


class Job:
    def __init__(self, data: dict):
        self.channels = list(data["channels"])
        self.shard = data["shard"]
        self.last_id = data["last_id"]
        self.skip = data["skip"]
        self.sent = data["sent"]
        self.errors = data["errors"]
        self.skipped = data["skipped"]
        self.total = data.get("total", 0)
        self.dropped = list(data.get("dropped", []))
        self.status = data.get("status", "stopped")
        self.error = data.get("error")
        self.start_shard = data.get("start_shard", data["shard"])
        self.start_id = data.get("start_id")
        self.ends = dict(data.get("ends", {}))
        self.failed = list(data.get("failed", []))
        self.deleted = data.get("deleted", 0)
        self.cleanup_error = None
        self.stop = False
        self.turn = 0
        self.pending = 0
        self.task = None
        self.message = None
        self.started = time.time()
        self.sent0 = self.sent
        self.done0 = self.done

    @property
    def done(self) -> int:
        return self.sent + self.errors + self.skipped

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def to_doc(self, status: str) -> dict:
        return {
            "channels": self.channels,
            "shard": self.shard,
            "last_id": self.last_id,
            "skip": self.skip,
            "sent": self.sent,
            "errors": self.errors,
            "skipped": self.skipped,
            "total": self.total,
            "dropped": self.dropped,
            "status": status,
            "error": self.error,
            "start_shard": self.start_shard,
            "start_id": self.start_id,
            "ends": self.ends,
            "failed": self.failed,
            "deleted": self.deleted,
        }


current = None


def _blacklist_words() -> list:
    words = list(config.BLACKLIST_KEYWORDS)
    if os.path.exists(config.BLACKLIST_FILE):
        with open(config.BLACKLIST_FILE, "r", encoding="utf-8") as f:
            words += [line.strip() for line in f if line.strip()]
    unique = {}
    for word in words:
        unique.setdefault(word.lower(), word)
    return sorted(unique.values(), key=len, reverse=True)


def _load_blacklist():
    words = _blacklist_words()
    if not words:
        return None
    pattern = "|".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!\w)(?:{pattern})(?!\w)", re.IGNORECASE)


def _status_text(job: Job, label: str, live: bool = False) -> str:
    position = job.skip + job.done
    percent = min(position / job.total * 100, 100) if job.total else 0
    lines = [
        f"<b>📤 Forwarding</b> — {label}\n",
        f"├ Channels: <code>{len(job.channels)}</code>",
        f"├ Sent: <code>{job.sent}</code>",
        f"├ Skipped: <code>{job.skipped}</code>",
        f"├ Errors: <code>{job.errors}</code>",
        f"{'├' if live else '└'} Progress: <code>{position}/{job.total}</code> ({percent:.1f}%)",
    ]
    if live:
        elapsed = max(time.time() - job.started, 1)
        rate = (job.done - job.done0) / elapsed
        per_minute = (job.sent - job.sent0) / elapsed * 60
        remaining = max(job.total - position, 0)
        eta = fmt_time(remaining / rate) if rate > 0 else "Calculating..."
        lines += [
            f"├ Speed: <code>{per_minute:.1f}/min</code>",
            f"├ Elapsed: <code>{fmt_time(elapsed)}</code>",
            f"└ ETA: <code>{eta}</code>",
        ]
    if job.deleted:
        lines.append(f"\n🧹 Removed from database: <code>{job.deleted}</code>")
    if job.cleanup_error:
        lines.append(f"\n⚠️ Cleanup problem: {html.escape(job.cleanup_error)}")
    if job.dropped:
        ids = ", ".join(f"<code>{c}</code>" for c in job.dropped)
        lines.append(f"\n⚠️ Removed channels: {ids}")
    if job.error:
        lines.append(f"\n❌ {html.escape(str(job.error))}")
    return "\n".join(lines)


async def _save(job: Job, status: str):
    try:
        await forward_db.save(job.to_doc(status))
    except Exception as e:
        logger.error(f"Could not save forward progress: {e}")


async def _fetch(job: Job):
    for _ in range(FETCH_RETRIES):
        try:
            return await filedb.next_batch(job.shard, job.last_id, BATCH_SIZE)
        except Exception as e:
            logger.error(f"Fetch failed: {e}")
            await asyncio.sleep(10)
    raise ConnectionError("File database is unreachable")


async def _try_send(client, channel: int, file_id: str, caption: str) -> int:
    for _ in range(FLOOD_RETRIES):
        try:
            await client.send_cached_media(
                chat_id=channel,
                file_id=file_id,
                caption=caption,
                protect_content=config.PROTECT_CONTENT,
            )
            return OK
        except FloodWait as e:
            logger.warning(f"FloodWait {e.value}s on {channel}")
            await asyncio.sleep(e.value + 1)
        except CHANNEL_ERRORS as e:
            logger.error(f"Channel {channel} unusable: {type(e).__name__}")
            return BAD_CHANNEL
        except Exception as e:
            logger.error(f"Send failed on {channel}: {e}")
            return BAD_FILE
    return BAD_FILE


def _captions(doc: dict) -> list:
    short = f"<b>{html.escape(doc.get('file_name') or 'File')}</b>"
    caption = doc.get("caption") or short
    if len(caption) > CAPTION_LIMIT:
        caption = short
    return [caption] if caption == short else [caption, short]


async def _send(client, job: Job, doc: dict) -> bool:
    captions = _captions(doc)
    while job.channels:
        channel = job.channels[job.turn % len(job.channels)]
        result = BAD_FILE
        for caption in captions:
            result = await _try_send(client, channel, doc["file_id"], caption)
            if result != BAD_FILE:
                break
        if result == OK:
            job.turn += 1
            job.sent += 1
            return True
        if result == BAD_CHANNEL:
            job.channels.remove(channel)
            job.dropped.append(channel)
            continue
        job.errors += 1
        job.failed.append(doc["_id"])
        job.turn += 1
        return True
    return False


async def _process(client, job: Job):
    blacklist = _load_blacklist()
    numbers = [s.number for s in filedb.shards]
    next_at = time.monotonic()

    while not job.stop and job.channels:
        batch = await _fetch(job)
        if not batch:
            following = next((n for n in numbers if n > job.shard), None)
            if following is None:
                return
            job.shard, job.last_id = following, None
            continue

        for doc in batch:
            if job.stop or not job.channels:
                return

            if blacklist and blacklist.search(doc.get("file_name") or ""):
                job.skipped += 1
            else:
                slot = max(next_at, time.monotonic())
                await asyncio.sleep(max(slot - time.monotonic(), 0))
                if not await _send(client, job, doc):
                    return
                next_at = slot + config.FORWARD_INTERVAL / max(len(job.channels), 1)

            job.last_id = doc["_id"]
            job.ends[str(job.shard)] = job.last_id
            job.pending += 1
            if job.pending >= config.FORWARD_SAVE_EVERY:
                job.pending = 0
                await _save(job, "running")


async def _cleanup(job: Job):
    removed, problems = 0, []
    for shard in filedb.shards:
        end = job.ends.get(str(shard.number))
        if shard.number < job.start_shard or end is None:
            continue
        span = {"$lte": end}
        if shard.number == job.start_shard and job.start_id is not None:
            span["$gt"] = job.start_id
        if job.failed:
            span["$nin"] = job.failed
        try:
            result = await shard.col.delete_many({"_id": span})
            removed += result.deleted_count
            await filedb._refresh(shard, force=True)
        except Exception as e:
            logger.error(f"DB {shard.number}: cleanup failed: {e}")
            problems.append(f"DB {shard.number}: {type(e).__name__}")
    job.deleted += removed
    if problems:
        job.cleanup_error = ", ".join(problems)
    logger.info(f"Cleanup finished, {removed} files removed")


async def _status_loop(job: Job):
    while True:
        await asyncio.sleep(config.FORWARD_STATUS_INTERVAL)
        try:
            await job.message.edit_text(_status_text(job, "🟢 Running", live=True))
        except MessageNotModified:
            pass
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception as e:
            logger.warning(f"Status update failed: {e}")


async def _run(client, job: Job):
    ticker = asyncio.create_task(_status_loop(job))
    status = "done"
    try:
        await _process(client, job)
        if job.stop:
            status = "stopped"
        elif not job.channels:
            status, job.error = "failed", "No usable channels left"
    except Exception as e:
        logger.exception("Forward job failed")
        status, job.error = "failed", str(e)
    finally:
        ticker.cancel()

    if status == "done":
        await _cleanup(job)

    await _save(job, status)
    label = {"done": "✅ Completed", "stopped": "🛑 Stopped", "failed": "❌ Failed"}[status]
    text = _status_text(job, label)
    try:
        await job.message.edit_text(text)
    except Exception:
        pass
    await job.message.reply(text)


async def _validate(client, channels: list):
    valid, invalid = [], []
    for channel in channels:
        try:
            member = await client.get_chat_member(channel, "me")
        except Exception as e:
            invalid.append((channel, type(e).__name__))
            continue
        admin = member.status in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER)
        privileges = getattr(member, "privileges", None)
        if not admin or (privileges is not None and privileges.can_post_messages is False):
            invalid.append((channel, "bot needs admin rights to post"))
            continue
        valid.append(channel)
    return valid, invalid


async def _new_job_data(args: list) -> dict:
    skip = int(args.pop(0)) if args and args[0].isdigit() else 0
    channels = []
    for arg in args:
        try:
            channel = int(arg)
        except ValueError:
            raise ValueError(f"Invalid channel id: {arg}")
        if channel >= 0:
            raise ValueError(f"Invalid channel id: {arg}")
        if channel not in channels:
            channels.append(channel)
    if not channels:
        raise ValueError("Give at least one channel id.")

    shard, last_id = await filedb.skip_cursor(skip)
    return {
        "channels": channels,
        "shard": shard,
        "last_id": last_id,
        "start_shard": shard,
        "start_id": last_id,
        "skip": skip,
        "sent": 0,
        "errors": 0,
        "skipped": 0,
        "dropped": [],
    }


async def _resume_job_data() -> dict:
    data = await forward_db.get()
    if not data or data.get("status") == "done" or not data.get("channels"):
        raise ValueError("No saved job to resume.")
    if "start_shard" not in data:
        data["start_shard"], data["start_id"] = await filedb.skip_cursor(data.get("skip", 0))
    return data


@Client.on_message(filters.command("forward") & filters.user(config.ADMINS))
async def forward_command(client, message):
    global current
    if current and current.running:
        return await message.reply("⚠️ A forwarding job is already running. Use /forwardstop first.")

    if not config.PROGRESS_URI:
        return await message.reply("❌ PROGRESS_URI is not configured, so forwarding is disabled.")

    args = message.command[1:]
    try:
        data = await _new_job_data(args) if args else await _resume_job_data()
    except ValueError as e:
        return await message.reply(f"❌ {e}\n\n{USAGE}")
    except Exception as e:
        logger.exception("Could not prepare forward job")
        return await message.reply(f"❌ {html.escape(str(e))}")

    valid, invalid = await _validate(client, data["channels"])
    if invalid:
        rows = "\n".join(f"• <code>{c}</code> — {html.escape(r)}" for c, r in invalid)
        await message.reply(f"⚠️ Skipping invalid channels:\n{rows}")
    if not valid:
        return await message.reply("❌ No usable channels.")
    data["channels"] = valid

    job = Job(data)
    job.total = await filedb.count_files()
    job.message = await message.reply(_status_text(job, "🟢 Running", live=True))
    await _save(job, "running")

    current = job
    job.task = asyncio.create_task(_run(client, job))


@Client.on_message(filters.command("forwardstop") & filters.user(config.ADMINS))
async def forward_stop_command(client, message):
    if not current or not current.running:
        return await message.reply("No forwarding job is running.")
    current.stop = True
    await message.reply("🛑 Stopping after the current file...")


@Client.on_message(filters.command("forwardstatus") & filters.user(config.ADMINS))
async def forward_status_command(client, message):
    if current and current.running:
        return await message.reply(_status_text(current, "🟢 Running", live=True))
    try:
        data = await forward_db.get()
    except Exception as e:
        return await message.reply(f"❌ {html.escape(str(e))}")
    if not data:
        return await message.reply("No forwarding job found.")
    labels = {"done": "✅ Completed", "stopped": "🛑 Stopped", "failed": "❌ Failed", "running": "⚪ Interrupted"}
    await message.reply(_status_text(Job(data), labels.get(data.get("status"), "⚪ Idle")))