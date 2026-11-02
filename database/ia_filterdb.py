import asyncio
import base64
import logging
import re
import time
from datetime import datetime
from struct import pack

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure
from pyrogram.file_id import FileId

import config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SAVED = 1
DUPLICATE = 0
ERROR = 2

RECONNECT_DELAY = 30
_NAME_CLEAN = re.compile(r"[\[\]\(\)@_\-\.\+]")
_WORD_SPLIT = re.compile(r"[\s\[\]\(\)@_\-\.\+]+")


class Shard:

    def __init__(self, number: int, uri: str):
        self.number = number
        self.uri = uri
        self.client = None
        self.col = None
        self.online = False
        self.full = False
        self.size = 0
        self.checked_at = 0.0
        self.retry_at = 0.0


class FileDB:
    def __init__(self):
        self.shards = []
        self.limit = int(config.DB_SIZE_LIMIT_MB * 1024 * 1024)
        self._ready = False
        self._refresh_lock = None
        self._save_sem = None
        self._inflight = set()

    async def init(self) -> bool:
        if self._ready:
            return any(s.online for s in self.shards)

        self._refresh_lock = asyncio.Lock()
        self._save_sem = asyncio.Semaphore(10)

        uris = [u.strip() for u in config.FILE_URIS if u and u.strip()]
        self.shards = [Shard(i, u) for i, u in enumerate(uris, 1)]
        if not self.shards:
            logger.error("No MongoDB URI configured in config.FILE_URIS")

        await asyncio.gather(*(self._connect(s) for s in self.shards))
        await asyncio.gather(*(self._refresh(s) for s in self.shards if s.online))

        self._ready = True
        online = [s.number for s in self.shards if s.online]
        logger.info(f"MongoDB URIs online: {online} / {len(self.shards)}")
        return bool(online)

    async def close(self):
        for s in self.shards:
            if s.client is not None:
                s.client.close()

    async def _connect(self, shard: Shard):
        shard.retry_at = time.time() + RECONNECT_DELAY
        try:
            if shard.client is None:
                shard.client = AsyncIOMotorClient(
                    shard.uri, serverSelectionTimeoutMS=8000, maxPoolSize=20
                )
            await shard.client.admin.command("ping")
            shard.col = shard.client[config.DATABASE_NAME][config.COLLECTION_NAME]
            shard.online = True
        except Exception as e:
            shard.online = False
            logger.error(f"DB {shard.number}: connection failed: {e}")
            return

        try:
            await shard.col.create_index("file_id", unique=True)
            await shard.col.create_index([("created_at", DESCENDING)])
        except Exception as e:
            logger.warning(f"DB {shard.number}: index creation skipped: {e}")

    async def _refresh(self, shard: Shard, force: bool = False):
        now = time.time()
        if not shard.online:
            if force or now >= shard.retry_at:
                await self._connect(shard)
            if not shard.online:
                return
        try:
            stats = await shard.col.database.command("dbstats")
            shard.size = int(stats.get("dataSize", 0)) + int(stats.get("indexSize", 0))
            shard.full = shard.size >= self.limit
            shard.checked_at = time.time()
        except Exception as e:
            logger.error(f"DB {shard.number}: dbstats failed: {e}")
            shard.online = False
            shard.retry_at = time.time() + RECONNECT_DELAY

    def _needs_refresh(self, shard: Shard, now: float) -> bool:
        if shard.online:
            return now - shard.checked_at >= config.SIZE_CHECK_INTERVAL
        return now >= shard.retry_at

    async def _usable(self):
        """Online, not-full shards in fill order. Re-checks sizes when stale."""
        now = time.time()
        if any(self._needs_refresh(s, now) for s in self.shards):
            async with self._refresh_lock:
                now = time.time()
                stale = [s for s in self.shards if self._needs_refresh(s, now)]
                if stale:
                    await asyncio.gather(*(self._refresh(s) for s in stale))
        return [s for s in self.shards if s.online and not s.full]

    @staticmethod
    def _is_quota_error(e: Exception) -> bool:
        return getattr(e, "code", None) == 8000 or "quota" in str(e).lower()

    async def save_file(self, media):
        if not self._ready:
            await self.init()
        async with self._save_sem:
            return await self._save(media)

    async def _save(self, media):
        raw_id = getattr(media, "file_id", None)
        if not raw_id:
            logger.warning("Media without file_id skipped")
            return False, ERROR
        try:
            file_id, _ = unpack_new_file_id(raw_id)
        except Exception as e:
            logger.warning(f"Could not decode file_id: {e}")
            return False, ERROR

        if file_id in self._inflight:
            return False, DUPLICATE
        self._inflight.add(file_id)
        try:
            caption = getattr(media, "caption", None)
            caption_html = None
            if caption:
                caption_html = getattr(caption, "html", None) or str(caption)

            raw_name = getattr(media, "file_name", None) or ""
            if not raw_name and caption:  # videos often have no file name
                raw_name = str(caption).strip().split("\n")[0][:200]
            file_name = " ".join(_NAME_CLEAN.sub(" ", str(raw_name)).split())

            doc = {
                "file_id": file_id,
                "file_name": file_name,
                "file_size": getattr(media, "file_size", 0) or 0,
                "caption": caption_html,
                "created_at": datetime.now(),
                "backup": False,
            }

            online = [s for s in self.shards if s.online]
            if not online:
                logger.error("All databases are offline, cannot save")
                return False, ERROR

            found = await asyncio.gather(*(self._exists(s, file_id) for s in online))
            if any(found):
                return False, DUPLICATE

            for shard in await self._usable():
                try:
                    await shard.col.insert_one(doc)
                    return True, SAVED
                except DuplicateKeyError:
                    return False, DUPLICATE
                except OperationFailure as e:
                    if self._is_quota_error(e):
                        logger.warning(f"DB {shard.number} is out of space, switching to next")
                        shard.full = True
                        shard.checked_at = time.time()
                    else:
                        logger.error(f"DB {shard.number}: insert failed: {e}")
                except Exception as e:
                    logger.error(f"DB {shard.number}: insert failed: {e}")

            logger.error("No database available to store the file (all full/offline)")
            return False, ERROR
        finally:
            self._inflight.discard(file_id)

    @staticmethod
    async def _exists(shard: Shard, file_id: str) -> bool:
        try:
            return await shard.col.find_one({"file_id": file_id}, {"_id": 1}) is not None
        except Exception:
            return False

    @staticmethod
    def _build_filter(query: str):
        words = [w for w in _WORD_SPLIT.split(query.lower()) if w]
        if not words:
            return None
        return {
            "$and": [
                {
                    "$or": [
                        {"file_name": {"$regex": re.escape(w), "$options": "i"}},
                        {"caption": {"$regex": re.escape(w), "$options": "i"}},
                    ]
                }
                for w in words
            ]
        }

    async def search_files(self, query: str, offset: int = 0, limit: int = 10):
        if not self._ready:
            await self.init()
        flt = self._build_filter(query)
        online = [s for s in self.shards if s.online]
        if flt is None or not online:
            return [], 0

        max_results = config.MAX_RESULTS
        need = min(offset + limit, max_results)

        async def one(shard):
            try:
                cursor = shard.col.find(flt, {"_id": 0}).sort("created_at", -1).limit(need)
                docs = await cursor.to_list(length=need)
                count = await shard.col.count_documents(flt, limit=max_results)
                return docs, count
            except Exception as e:
                logger.error(f"DB {shard.number}: search failed: {e}")
                return [], 0

        results = await asyncio.gather(*(one(s) for s in online))

        merged = [d for docs, _ in results for d in docs]
        merged.sort(key=lambda d: d.get("created_at") or datetime.min, reverse=True)
        total = min(sum(c for _, c in results), max_results)
        return merged[offset:offset + limit], total

    async def get_file(self, file_id: str):
        online = [s for s in self.shards if s.online]

        async def one(shard):
            try:
                return await shard.col.find_one({"file_id": file_id}, {"_id": 0})
            except Exception:
                return None

        for doc in await asyncio.gather(*(one(s) for s in online)):
            if doc:
                return doc
        return None

    async def count_files(self) -> int:
        if not self._ready:
            await self.init()

        async def count(shard):
            if not shard.online:
                return 0
            try:
                return await shard.col.estimated_document_count()
            except Exception:
                return 0

        return sum(await asyncio.gather(*(count(s) for s in self.shards)))

    async def next_batch(self, shard_number: int, after_id=None, limit: int = 50):
        shard = next((s for s in self.shards if s.number == shard_number), None)
        if shard is None:
            return []
        if not shard.online:
            await self._refresh(shard, force=True)
        if not shard.online:
            raise ConnectionError(f"DB {shard.number} is offline")

        flt = {"_id": {"$gt": after_id}} if after_id is not None else {}
        projection = {"file_id": 1, "file_name": 1, "caption": 1}
        cursor = shard.col.find(flt, projection).sort("_id", 1).limit(limit)
        return await cursor.to_list(length=limit)

    async def skip_cursor(self, skip: int):
        if not self._ready:
            await self.init()
        if not self.shards:
            raise ValueError("No MongoDB URI configured")

        remaining = skip
        for shard in self.shards:
            if not shard.online:
                raise ConnectionError(f"DB {shard.number} is offline")
            count = await shard.col.count_documents({})
            if remaining >= count:
                remaining -= count
                continue
            if remaining == 0:
                return shard.number, None
            doc = await shard.col.find({}, {"_id": 1}).sort("_id", 1).skip(remaining - 1).limit(1).to_list(1)
            return shard.number, doc[0]["_id"]
        return self.shards[-1].number + 1, None

    async def get_stats(self):
        if not self._ready:
            await self.init()
        await asyncio.gather(*(self._refresh(s, force=True) for s in self.shards))

        active = next((s for s in self.shards if s.online and not s.full), None)

        async def count(shard):
            if not shard.online:
                return 0
            try:
                return await shard.col.estimated_document_count()
            except Exception:
                return 0

        counts = await asyncio.gather(*(count(s) for s in self.shards))

        rows = []
        for shard, files in zip(self.shards, counts):
            if not shard.online:
                status = "offline"
            elif shard.full:
                status = "full"
            elif shard is active:
                status = "active"
            else:
                status = "standby"
            rows.append({
                "number": shard.number,
                "status": status,
                "files": files,
                "size": shard.size,
                "limit": self.limit,
                "percent": (shard.size / self.limit * 100) if self.limit else 0,
            })
        return rows


filedb = FileDB()


async def save_file(media):
    return await filedb.save_file(media)


def encode_file_id(s: bytes) -> str:
    r = b""
    n = 0
    for i in s + bytes([22]) + bytes([4]):
        if i == 0:
            n += 1
        else:
            if n:
                r += b"\x00" + bytes([n])
                n = 0
            r += bytes([i])
    return base64.urlsafe_b64encode(r).decode().rstrip("=")


def encode_file_ref(file_ref: bytes) -> str:
    return base64.urlsafe_b64encode(file_ref).decode().rstrip("=")


def unpack_new_file_id(new_file_id):
    decoded = FileId.decode(new_file_id)
    file_id = encode_file_id(
        pack("<iiqq", int(decoded.file_type), decoded.dc_id, decoded.media_id, decoded.access_hash)
    )
    file_ref = encode_file_ref(decoded.file_reference)
    return file_id, file_ref
