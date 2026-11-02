# 📁 FileSaverBot

A Telegram bot by **PiRO** that indexes files from your channels into MongoDB, lets users search them, and can forward the whole collection to any channels you choose.

[![Updates](https://img.shields.io/badge/Updates-@piroxbots-blue?logo=telegram)](https://t.me/piroxbots)
[![Support](https://img.shields.io/badge/Support-@piroxsupportbot-blue?logo=telegram)](https://t.me/piroxsupportbot)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## ✨ Features

- **Live and manual indexing** of channel files (video type and size filters included)
- **Multi-database storage**: add as many MongoDB URIs as you need; the bot fills one, then moves to the next
- **Fast search** across every database, with pagination
- **Smart forwarding**: send all saved files to any channels, with an automatic rate limit per channel, resume support and a live progress message
- **Admin tools**: stats, restart and self-update from Telegram

## 🤖 Commands

| Command | Who | What |
|---|---|---|
| `/start`, `/help` | everyone | Intro and usage |
| text or `/search name` | everyone | Search files |
| `/stats` | admin | Files, size and status of each database |
| `/index <link>` | admin | Index a channel up to a message (or paste a link / forward a post in PM) |
| `/setskip <n>` | admin | Start indexing after message `n` |
| `/status` | admin | Indexing status |
| `/forward [skip] id1 id2 ...` | admin | Forward saved files to the given channels |
| `/forward` | admin | Resume the last forwarding job |
| `/forwardstop` | admin | Stop forwarding safely |
| `/forwardstatus` | admin | Forwarding progress |
| `/update` | admin | `git pull`, install requirements and restart |
| `/restart` | admin | Restart the bot |

## 🚀 Setup

**Requirements:** Python 3.10+ (3.11 recommended) and at least one MongoDB URI (a free Atlas cluster works).

```bash
git clone https://github.com/<your-username>/FileSaverBot
cd FileSaverBot
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

Fill in `.env` before starting:

| Variable | Required | Description |
|---|---|---|
| `API_ID`, `API_HASH` | yes | From [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | yes | From [@BotFather](https://t.me/BotFather) |
| `ADMINS` | yes | Admin user IDs, separated by commas |
| `FILE_URIS` | yes | MongoDB URIs for file storage, separated by spaces or commas |
| `CHANNELS` | no | Channel IDs to index live. Add the bot as admin in each one |
| `PROGRESS_URI` | no | A separate MongoDB URI that stores forwarding progress. Needed only for `/forward` |

Never commit your `.env` file. It is already in `.gitignore`.

### Docker

```bash
docker build -t filesaverbot .
docker run -d --env-file .env filesaverbot
```

### Heroku and similar platforms

A `Procfile` is included. Set the variables above in your platform's config and deploy.

## 🗄 Multi-database storage

`FILE_URIS` is a list. Files go to the first URI. Once it reaches `DB_SIZE_LIMIT_MB` (see `config.py`), the bot moves to the second one, and so on. Search and duplicate checks cover every URI. If a cluster rejects a write because it is out of quota, the bot marks it full and retries on the next one.

## 📤 Forwarding

```
/forward 0 -1001111111111 -1002222222222 -1003333333333
```

- The first number is how many files to skip. It is optional.
- The bot must be an admin with posting rights in every target channel. Channels that fail the check are skipped.
- Each channel receives 20 files per minute. The delay between sends is `3 seconds ÷ number of channels`, so three channels get 60 files per minute in total.
- Files are spread evenly across the channels. A channel that starts failing is removed and the others carry on.
- Progress is saved to `PROGRESS_URI` every 20 files. Run `/forward` with no arguments to resume after a stop or restart.
- To skip unwanted names, create a `blacklist.txt` file with one word per line.

## 💬 Support

- Updates and news: [@piroxbots](https://t.me/piroxbots)
- Help and questions: [@piroxsupportbot](https://t.me/piroxsupportbot)

## ⭐ Support the project

If this bot helps you, please **star** ⭐ and **fork** 🍴 the repository. It helps others find it and keeps the project going.

## 📜 License

Released under the [MIT License](LICENSE). Made with ❤️ by **PiRO**.
