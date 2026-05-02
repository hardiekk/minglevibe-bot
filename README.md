# AnonMatch Telegram Bot

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set environment variables
Copy `.env.example` to `.env` and fill in your keys:
- `TELEGRAM_BOT_TOKEN` → Get from @BotFather on Telegram
- `GEMINI_API_KEY` → Get free from https://aistudio.google.com

### 3. Run the bot
```bash
TELEGRAM_BOT_TOKEN=xxx GEMINI_API_KEY=xxx python bot.py
```

---

## AI Girl — Priya

When no real users are in the queue, users are automatically matched
with **Priya** — an AI girl powered by **Gemini 1.5 Flash**.

Priya:
- Replies in Hinglish (Hindi + English)
- Sends realistic short messages like real texting
- Has typing delays (1.5–4 seconds) to feel human
- Occasionally sends follow-up messages
- Never admits to being a bot

This keeps users engaged even during low-traffic times (cold start problem solved).

---

## File Structure
```
anonmatch-bot/
├── bot.py              ← Main bot file
├── requirements.txt    ← Dependencies
├── .env.example        ← Environment variable template
└── README.md
```

---

## Deploy to Railway (Free)
1. Push to GitHub
2. Go to railway.app → New Project → Deploy from GitHub
3. Add TELEGRAM_BOT_TOKEN and GEMINI_API_KEY as env variables
4. Railway auto-deploys — done!
