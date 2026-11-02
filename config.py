import os
import re

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required setting: {name} (see .env.example)")
    return value


def _items(name: str, cast=str, required: bool = False) -> list:
    values = [cast(v) for v in re.split(r"[,\s]+", os.getenv(name, "")) if v]
    if required and not values:
        raise RuntimeError(f"Missing required setting: {name} (see .env.example)")
    return values


def _words(name: str) -> list:
    seen, words = set(), []
    for value in re.split(r"[,\n]", os.getenv(name, "")):
        word = value.strip()
        if word and word.lower() not in seen:
            seen.add(word.lower())
            words.append(word)
    return words


API_ID = int(_required("API_ID"))
API_HASH = _required("API_HASH")
BOT_TOKEN = _required("BOT_TOKEN")

ADMINS = _items("ADMINS", int, required=True)
CHANNELS = _items("CHANNELS", int)

FILE_URIS = _items("FILE_URIS", required=True)
PROGRESS_URI = os.getenv("PROGRESS_URI", "").strip()

DATABASE_NAME = "dmfilterindex"
COLLECTION_NAME = "dmfiltercollection"
PROGRESS_DB = "forwardprogress"
PROGRESS_COLLECTION = "jobs"

DB_SIZE_LIMIT_MB = 480
SIZE_CHECK_INTERVAL = 60

MIN_FILE_SIZE_MB = 10

ALLOWED_EXTENSIONS = {
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg",
    "ts", "m2ts", "3gp", "ogv", "vob", "divx", "rmvb",
}
BLOCKED_EXTENSIONS = {
    "aria2", "qb", "!qb", "html", "htm", "jpg", "jpeg", "png", "js", "txt",
    "srt", "nfo", "webp", "m4a", "exe", "iso", "rar", "zip", "inf", "mp3",
    "pdf", "url", "tiff", "tif", "dat", "bundle", "hash", "json", "xml",
    "bin", "ini", "dll", "config", "extension", "extenstion",
}

INDEX_BATCH_SIZE = 20
INDEX_DELAY = 2.0

RESULTS_PER_PAGE = 10
MAX_RESULTS = 200
MIN_QUERY_LENGTH = 2
PROTECT_CONTENT = False
AUTO_DELETE_SECONDS = 0

BLACKLIST_KEYWORDS = _words("BLACKLIST_KEYWORDS")
BLACKLIST_FILE = "blacklist.txt"

FORWARD_INTERVAL = 3.0
FORWARD_SAVE_EVERY = 20
FORWARD_STATUS_INTERVAL = 30

SUPPORT_LINK = "https://t.me/piroxsupportbot"
UPDATES_LINK = "https://t.me/piroxbots"
REPO_LINK = "https://github.com/PIROXTG/FilesDBBackup"