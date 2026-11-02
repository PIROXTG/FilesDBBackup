import logging
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorClient

import config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

JOB_ID = "job"


class ForwardDB:
    def __init__(self):
        self.client = None
        self.col = None

    def _collection(self):
        if self.col is None:
            self.client = AsyncIOMotorClient(
                config.PROGRESS_URI, serverSelectionTimeoutMS=8000, maxPoolSize=5
            )
            self.col = self.client[config.PROGRESS_DB][config.PROGRESS_COLLECTION]
        return self.col

    async def init(self) -> bool:
        try:
            col = self._collection()
            await col.database.client.admin.command("ping")
            return True
        except Exception as e:
            logger.error(f"Progress database is not reachable: {e}")
            return False

    async def get(self):
        return await self._collection().find_one({"_id": JOB_ID})

    async def save(self, data: dict):
        data = {**data, "updated_at": datetime.now()}
        await self._collection().update_one({"_id": JOB_ID}, {"$set": data}, upsert=True)

    async def close(self):
        if self.client is not None:
            self.client.close()


forward_db = ForwardDB()
