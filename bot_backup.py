from typing import Optional
"""
MingleVibe Bot — Production Ready
==================================
Anonymous Telegram Matching + Auto Razorpay Payments
Author: MingleVibe Team
"""

import logging
import os
import random
import asyncio
import hmac
import hashlib

from datetime import date, datetime, timedelta
from collections import deque
from dotenv import load_dotenv

load_dotenv()

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("MingleVibe")

# ─────────────────────────────────────────────
# Config — fill these before running
# ─────────────────────────────────────────────
FREE_DAILY_LIMIT  = 3          # free matches per day
AUTO_BAN_REPORTS  = 3          # reports before auto-ban
COOLDOWN_SECONDS  = 10         # seconds between rapid match requests
REFERRAL_REWARD_H = 24         # hours of VIP per 3 referrals

ADMIN_IDS: set[int] = {123456789}   # replace with your Telegram user_id

# Loaded from .env
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
RAZORPAY_KEY_ID     = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")

# Razorpay plans — amount in paise (₹1 = 100 paise)
PLANS: dict[str, dict] = {
    "1d":  {"days": 1,  "amount": 2900,  "label": "1 Day",   "display": "₹29"},
    "7d":  {"days": 7,  "amount": 9900,  "label": "7 Days",  "display": "₹99"},
    "30d": {"days": 30, "amount": 29900, "label": "30 Days", "display": "₹299"},
}

ICEBREAKERS: list[str] = [
    "🎯 *Icebreaker:* Apna ek hidden talent batao!",
    "🎯 *Icebreaker:* Kaunsa superpower chahiye tumhe?",
    "🎯 *Icebreaker:* Last time kab hassi aayi aur kyu?",
    "🎯 *Icebreaker:* Chai ya coffee? Defend karo! ☕",
    "🎯 *Icebreaker:* Ek cheez jo log tumhare baare mein nahi jaante?",
    "🎯 *Icebreaker:* Ideal Saturday kaise spend karoge?",
    "🎯 *Icebreaker:* Kaunsa movie character tum ho actually?",
    "🎯 *Icebreaker:* 3 words mein apne aap ko describe karo!",
    "🎯 *Icebreaker:* Night owl ya morning person? 🌙☀️",
    "🎯 *Icebreaker:* Ek wish maango — kya mangoge?",
]

# ─────────────────────────────────────────────
# Razorpay client (lazy init to catch missing keys early)
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# In-Memory State
# ─────────────────────────────────────────────
waiting_queue:        deque = deque()
active_chats:         dict  = {}   # user_id -> partner_id
user_profiles:        dict  = {}   # user_id -> profile dict
reported_users:       dict  = {}   # user_id -> report count
banned_users:         set   = set()
chat_start_times:     dict  = {}   # user_id -> datetime
cooldowns:            dict  = {}   # user_id -> datetime
referral_map:         dict  = {}   # code -> user_id
user_referral_counts: dict  = {}   # user_id -> int
premium_expiry:       dict  = {}   # user_id -> datetime
pending_payments:     dict  = {}   # link_id -> PaymentOrder

_bot_username: str = ""            # cached at startup

# ─────────────────────────────────────────────
# Profile helpers
# ─────────────────────────────────────────────
def get_profile(user_id: int) -> dict:
    if user_id not in user_profiles:
        user_profiles[user_id] = {
            "user_id":        user_id,
            "age_confirmed":  False,
            "gender":         None,           # male / female / other
            "pref_gender":    "any",          # male / female / any
            "mood":           None,           # fun / dating / friendship / vent
            "is_premium":     False,
            "matches_today":  0,
            "last_match_date": None,
            "total_matches":  0,
            "referral_code":  f"MV{abs(user_id) % 99999:05d}",
            "referred_by":    None,
        }
    return user_profiles[user_id]

def reset_daily_count(profile: dict) -> None:
    if profile["last_match_date"] != date.today():
        profile["matches_today"]   = 0
        profile["last_match_date"] = date.today()

def is_premium(user_id: int) -> bool:
    p = get_profile(user_id)
    if p["is_premium"]:
        return True
    expiry = premium_expiry.get(user_id)
    return bool(expiry and datetime.now() < expiry)

def can_match(user_id: int) -> bool:
    p = get_profile(user_id)
    reset_daily_count(p)
    return is_premium(user_id) or p["matches_today"] < FREE_DAILY_LIMIT

def remaining_matches(user_id: int) -> str:
    if is_premium(user_id):
        return "Unlimited ♾️"
    p = get_profile(user_id)
    reset_daily_count(p)
    return f"{max(0, FREE_DAILY_LIMIT - p['matches_today'])} / {FREE_DAILY_LIMIT}"

def increment_match(user_id: int) -> None:
    p = get_profile(user_id)
    p["matches_today"]  += 1
    p["total_matches"]  += 1
    p["last_match_date"] = date.today()

def remove_from_queue(user_id: int) -> None:
    try:
        waiting_queue.remove(user_id)
    except ValueError:
        pass

def disconnect_user(user_id: int) -> Optional[int]:
    """Disconnect user from active chat. Returns partner_id or None."""
    partner_id = active_chats.pop(user_id, None)
    if partner_id:
        active_chats.pop(partner_id, None)
        chat_start_times.pop(partner_id, None)
    remove_from_queue(user_id)
    chat_start_times.pop(user_id, None)
    return partner_id

def get_chat_duration(user_id: int) -> str:
    start = chat_start_times.get(user_id)
    if not start:
        return "0s"
    delta = datetime.now() - start
    m, s  = divmod(int(delta.total_seconds()), 60)
    return f"{m}m {s}s" if m else f"{s}s"

def is_on_cooldown(user_id: int) -> bool:
    last = cooldowns.get(user_id)
    return bool(last and (datetime.now() - last).total_seconds() < COOLDOWN_SECONDS)

def set_cooldown(user_id: int) -> None:
    cooldowns[user_id] = datetime.now()

def find_best_match(user_id: int) -> Optional[int]:
    """Priority: gender+mood → gender only → anyone."""
    p    = get_profile(user_id)
    pref = p.get("pref_gender", "any")
    mood = p.get("mood")

    def gender_ok(cp: dict) -> bool:
        return pref == "any" or cp.get("gender") == pref

    def mood_ok(cp: dict) -> bool:
        return mood is None or cp.get("mood") == mood

    queue_list = [c for c in waiting_queue if c != user_id and c not in active_chats]

    for check in [
        lambda cp: gender_ok(cp) and mood_ok(cp),
        lambda cp: gender_ok(cp),
        lambda cp: True,
    ]:
        for candidate in queue_list:
            if check(get_profile(candidate)):
                remove_from_queue(candidate)
                return candidate

    return None

def verify_razorpay_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Verify Razorpay webhook/payment signature."""
    body    = f"{order_id}|{payment_id}"
    digest  = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        body.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, signature)

# ─────────────────────────────────────────────
# Keyboards
# ─────────────────────────────────────────────
def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton("🔍 Find Match"),    KeyboardButton("⚙️ Preferences")],
        [KeyboardButton("💎 Go Premium"),    KeyboardButton("📊 My Stats")],
        [KeyboardButton("🔗 Refer & Earn"),  KeyboardButton("ℹ️ Help")],
    ], resize_keyboard=True)

def kb_chat() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton("⏭ Next"),    KeyboardButton("🛑 Stop")],
        [KeyboardButton("🚨 Report"), KeyboardButton("⏱ Chat Time")],
    ], resize_keyboard=True)

def kb_age() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, I'm 18+", callback_data="age_yes")],
        [InlineKeyboardButton("❌ No",            callback_data="age_no")],
    ])

def kb_gender() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👦 Boy",          callback_data="gender_male"),
         InlineKeyboardButton("👧 Girl",         callback_data="gender_female")],
        [InlineKeyboardButton("🌈 Other / Skip", callback_data="gender_other")],
    ])

def kb_pref() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👦 Boys",   callback_data="pref_male"),
         InlineKeyboardButton("👧 Girls",  callback_data="pref_female")],
        [InlineKeyboardButton("🌍 Anyone", callback_data="pref_any")],
    ])

def kb_mood() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("😂 Fun / Timepass", callback_data="mood_fun"),
         InlineKeyboardButton("💘 Dating / Flirt", callback_data="mood_dating")],
        [InlineKeyboardButton("🤝 Friendship",     callback_data="mood_friendship"),
         InlineKeyboardButton("🫂 Vent / Talk",    callback_data="mood_vent")],
    ])

def kb_prefs_edit() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Change Gender",     callback_data="edit_gender")],
        [InlineKeyboardButton("🎯 Change Preference", callback_data="edit_pref")],
        [InlineKeyboardButton("😊 Change Mood",       callback_data="edit_mood")],
        [InlineKeyboardButton("❌ Close",              callback_data="close")],
    ])

def kb_plans() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ 1 Day  — ₹29",   callback_data="buy_1d")],
        [InlineKeyboardButton("🔥 7 Days — ₹99",   callback_data="buy_7d")],
        [InlineKeyboardButton("💎 30 Days — ₹299", callback_data="buy_30d")],
        [InlineKeyboardButton("❌ Cancel",           callback_data="close")],
    ])

# ─────────────────────────────────────────────
# Handlers — Onboarding
# ─────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _bot_username
    user_id = update.effective_user.id

    if user_id in banned_users:
        await update.message.reply_text("🚫 You are banned from MingleVibe.")
        return

    profile = get_profile(user_id)
    referral_map[profile["referral_code"]] = user_id

    # Cache bot username once
    if not _bot_username:
        _bot_username = (await context.bot.get_me()).username

    # Handle referral deep link
    if context.args and not profile["referred_by"]:
        referrer_id = referral_map.get(context.args[0])
        if referrer_id and referrer_id != user_id:
            profile["referred_by"] = referrer_id
            user_referral_counts[referrer_id] = user_referral_counts.get(referrer_id, 0) + 1
            count = user_referral_counts[referrer_id]
            if count % 3 == 0:
                premium_expiry[referrer_id] = datetime.now() + timedelta(hours=REFERRAL_REWARD_H)
                notif = (
                    f"🎉 *Referral Reward!*\n\n"
                    f"*{count}* dost join kar gaye!\n"
                    f"💎 *{REFERRAL_REWARD_H} Hours VIP* activate ho gaya! 🔥"
                )
            else:
                remaining = 3 - (count % 3)
                notif = (
                    f"✅ Ek aur dost join hua! *{count}/3*\n"
                    f"*{remaining}* aur invite karo → {REFERRAL_REWARD_H}hr VIP FREE! 🔥"
                )
            try:
                await context.bot.send_message(referrer_id, notif, parse_mode="Markdown")
            except Exception:
                pass

    if not profile["age_confirmed"]:
        await update.message.reply_text(
            "👋 Welcome to *MingleVibe!*\n\n"
            "Chat with strangers anonymously on Telegram.\n"
            "No profile. No number. 100% anonymous. ✨\n\n"
            "⚠️ *18+ only.* Are you 18 or older?",
            parse_mode="Markdown",
            reply_markup=kb_age(),
        )
        return

    await update.message.reply_text(
        "👋 Welcome back to *MingleVibe!* 🔥\n\nTap *Find Match* to connect!",
        parse_mode="Markdown",
        reply_markup=kb_main(),
    )

# ─────────────────────────────────────────────
# Handlers — Callbacks
# ─────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _bot_username
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data    = query.data
    profile = get_profile(user_id)

    # ── Age gate ──
    if data == "age_yes":
        profile["age_confirmed"] = True
        await query.edit_message_text(
            "✅ *Welcome to MingleVibe!*\n\n"
            "🎁 3 free matches every day\n"
            "💎 Upgrade anytime for unlimited\n\n"
            "Let's set up your profile 👇",
            parse_mode="Markdown",
        )
        await context.bot.send_message(user_id, "👤 What's your gender?", reply_markup=kb_gender())
        return

    if data == "age_no":
        await query.edit_message_text("❌ Sorry, MingleVibe is only for users 18 and above.")
        return

    # ── Profile setup ──
    if data.startswith("gender_"):
        profile["gender"] = data[7:]          # male / female / other
        await query.edit_message_text("✅ Gender saved!")
        await context.bot.send_message(user_id, "🎯 Who do you want to talk to?", reply_markup=kb_pref())
        return

    if data.startswith("pref_"):
        profile["pref_gender"] = data[5:]     # male / female / any
        await query.edit_message_text("✅ Preference saved!")
        await context.bot.send_message(user_id, "😊 What's your mood today?", reply_markup=kb_mood())
        return

    if data.startswith("mood_"):
        profile["mood"] = data[5:]            # fun / dating / friendship / vent
        await query.edit_message_text("✅ All done!")
        await context.bot.send_message(
            user_id,
            "🎉 *Profile set!* Tap Find Match to start! 🔥",
            parse_mode="Markdown",
            reply_markup=kb_main(),
        )
        return

    # ── Edit preferences ──
    if data == "edit_gender":
        await query.edit_message_text("👤 Select your gender:", reply_markup=kb_gender())
        return
    if data == "edit_pref":
        await query.edit_message_text("🎯 Who do you want to talk to?", reply_markup=kb_pref())
        return
    if data == "edit_mood":
        await query.edit_message_text("😊 What's your mood?", reply_markup=kb_mood())
        return
    if data == "close":
        await query.edit_message_text("✅ Closed.")
        return

    # ── Buy plan → create Razorpay Payment Link ──
    if data.startswith("buy_"):
        plan_key = data[4:]                   # 1d / 7d / 30d
        plan     = PLANS.get(plan_key)
        if not plan:
            await query.edit_message_text("❌ Invalid plan. Please try again.")
            return

        await query.edit_message_text("⏳ Generating secure payment link...")

        if not _bot_username:
            _bot_username = (await context.bot.get_me()).username

        try:
            rzp    = get_rzp()
            plink  = rzp.payment_link.create({
                "amount":          plan["amount"],
                "currency":        "INR",
                "description":     f"MingleVibe Premium — {plan['label']}",
                "notify":          {"sms": False, "email": False},
                "reminder_enable": False,
                "expire_by":       int((datetime.now() + timedelta(hours=1)).timestamp()),
                "notes": {
                    "user_id":    str(user_id),
                    "plan_key":   plan_key,
                    "plan_label": plan["label"],
                    "plan_days":  str(plan["days"]),
                },
                "callback_url":    f"https://t.me/{_bot_username}",
                "callback_method": "get",
            })

            link_id = plink["id"]
            pay_url = plink["short_url"]

            pending_payments[link_id] = {
                "user_id":    user_id,
                "plan_key":   plan_key,
                "plan_days":  plan["days"],
                "plan_label": plan["label"],
                "amount":     plan["amount"],
                "created_at": datetime.now(),
            }

            await query.edit_message_text(
                f"💳 *{plan['label']} — {plan['display']}*\n\n"
                f"1️⃣ Neeche *Pay Now* tap karo\n"
                f"2️⃣ GPay / PhonePe / UPI se pay karo\n"
                f"3️⃣ Wapas aao aur *Verify Payment* tap karo\n\n"
                f"⏰ Link 1 hour mein expire hoga.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"💳 Pay {plan['display']} Now", url=pay_url)],
                    [InlineKeyboardButton("✅ Verify Payment", callback_data=f"verify_{link_id}")],
                    [InlineKeyboardButton("❌ Cancel",          callback_data="close")],
                ]),
            )

        except EnvironmentError:
            await query.edit_message_text(
                "❌ Payments are temporarily unavailable.\n\nContact admin: @YourAdminUsername"
            )
        except Exception as e:
            logger.error(f"Razorpay payment link error: {e}")
            await query.edit_message_text(
                "❌ Payment link create nahi hua. Dobara try karo ya contact karo: @YourAdminUsername"
            )
        return

    # ── Verify payment ──
    if data.startswith("verify_"):
        link_id = data[7:]
        order   = pending_payments.get(link_id)

        if not order:
            await query.edit_message_text(
                "❌ Order not found.\n\nAgar payment ho gayi hai toh contact karo: @YourAdminUsername"
            )
            return

        await query.edit_message_text("🔄 Verifying payment with Razorpay...")

        try:
            rzp       = get_rzp()
            link_data = rzp.payment_link.fetch(link_id)
            status    = link_data.get("status", "")

            if status == "paid":
                target = order["user_id"]
                get_profile(target)["is_premium"] = True
                expires_at = datetime.now() + timedelta(days=order["plan_days"])
                premium_expiry[target] = expires_at
                pending_payments.pop(link_id, None)

                await query.edit_message_text(
                    f"🎉 *Payment Verified!*\n\n"
                    f"💎 *{order['plan_label']} Premium* activated!\n"
                    f"Expires: {expires_at.strftime('%d %b %Y at %H:%M')}\n\n"
                    f"Enjoy unlimited matches! 🔥",
                    parse_mode="Markdown",
                    reply_markup=kb_main(),
                )

                # Notify admins
                for admin_id in ADMIN_IDS:
                    try:
                        await context.bot.send_message(
                            admin_id,
                            f"💰 *Payment Received — Auto Activated*\n\n"
                            f"User: `{target}`\n"
                            f"Plan: {order['plan_label']}\n"
                            f"Amount: ₹{order['amount'] // 100}\n"
                            f"Expires: {expires_at.strftime('%d %b %Y %H:%M')}",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass

            elif status in ("created", "partially_paid"):
                await query.edit_message_text(
                    "⏳ *Payment not received yet.*\n\n"
                    "Pay karo phir dobara *Verify* tap karo.\n"
                    "Problem ho toh contact: @YourAdminUsername",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Check Again", callback_data=f"verify_{link_id}")],
                        [InlineKeyboardButton("❌ Cancel",       callback_data="close")],
                    ]),
                )

            elif status == "expired":
                pending_payments.pop(link_id, None)
                await query.edit_message_text(
                    "❌ Payment link expire ho gaya.\n\nDobara *Go Premium* tap karo.",
                    parse_mode="Markdown",
                    reply_markup=kb_main(),
                )

            else:
                await query.edit_message_text(
                    f"⚠️ Payment status: *{status}*\n\nContact: @YourAdminUsername",
                    parse_mode="Markdown",
                )

        except EnvironmentError:
            await query.edit_message_text("❌ Verification unavailable. Contact: @YourAdminUsername")
        except Exception as e:
            logger.error(f"Razorpay verify error: {e}")
            await query.edit_message_text(
                "❌ Verification error.\n\nManually contact: @YourAdminUsername"
            )
        return

# ─────────────────────────────────────────────
# Handlers — Matching
# ─────────────────────────────────────────────
async def cmd_find_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if user_id in banned_users:
        await update.message.reply_text("🚫 You are banned.")
        return

    profile = get_profile(user_id)

    if not profile["age_confirmed"]:
        await update.message.reply_text("Please use /start first to set up your profile.")
        return

    if user_id in active_chats:
        await update.message.reply_text("You're already in a chat! Use ⏭ Next to find someone new.")
        return

    if is_on_cooldown(user_id):
        await update.message.reply_text(f"⏳ Please wait {COOLDOWN_SECONDS}s between match requests.")
        return

    if not can_match(user_id):
        await update.message.reply_text(
            f"⚠️ *{FREE_DAILY_LIMIT} free matches used today!*\n\n"
            "💎 Upgrade to *Premium* for unlimited matches.\n"
            "Or refer 3 friends → 24hr VIP FREE! 🔥\n\n"
            "Tap *Go Premium* below 👇",
            parse_mode="Markdown",
            reply_markup=kb_main(),
        )
        return

    set_cooldown(user_id)
    candidate = find_best_match(user_id)

    if candidate:
        # Connect both users
        active_chats[user_id]   = candidate
        active_chats[candidate] = user_id
        increment_match(user_id)
        increment_match(candidate)
        now = datetime.now()
        chat_start_times[user_id]   = now
        chat_start_times[candidate] = now

        icebreaker = random.choice(ICEBREAKERS)
        match_msg  = (
            "🎉 *Match found!* You're chatting anonymously.\n\n"
            "🔒 Identity hidden — say hi! 👋\n"
            "⏭ Next  ·  🛑 Stop  ·  🚨 Report\n\n"
            f"{icebreaker}"
        )
        await context.bot.send_message(user_id,   match_msg, parse_mode="Markdown", reply_markup=kb_chat())
        await context.bot.send_message(candidate, match_msg, parse_mode="Markdown", reply_markup=kb_chat())
        return

    # No match found — add to queue
    if user_id not in waiting_queue:
        waiting_queue.append(user_id)

    pos = list(waiting_queue).index(user_id) + 1
    await update.message.reply_text(
        f"⏳ *Searching for a match...*\n\n"
        f"Queue position: *#{pos}*\n"
        f"Estimated wait: ~{pos * 15}s\n\n"
        f"Use /cancel to stop waiting.",
        parse_mode="Markdown",
    )

async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id    = update.effective_user.id
    duration   = get_chat_duration(user_id)
    partner_id = disconnect_user(user_id)

    if partner_id:
        await context.bot.send_message(
            partner_id,
            f"⏭ Your partner left after *{duration}*.\nTap *Find Match* for someone new!",
            parse_mode="Markdown",
            reply_markup=kb_main(),
        )

    await update.message.reply_text(
        f"⏭ Chat lasted *{duration}*. Finding next match...",
        parse_mode="Markdown",
        reply_markup=kb_main(),
    )
    await cmd_find_match(update, context)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id    = update.effective_user.id
    duration   = get_chat_duration(user_id)
    partner_id = disconnect_user(user_id)

    if partner_id:
        await context.bot.send_message(
            partner_id,
            f"🛑 Your partner ended the chat after *{duration}*.\nTap *Find Match* to meet someone new!",
            parse_mode="Markdown",
            reply_markup=kb_main(),
        )

    await update.message.reply_text(
        f"🛑 Chat ended after *{duration}*.\n\nTap *Find Match* whenever you're ready! 👇",
        parse_mode="Markdown",
        reply_markup=kb_main(),
    )

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id    = update.effective_user.id
    partner_id = active_chats.get(user_id)

    if not partner_id:
        await update.message.reply_text("You're not currently in a chat.")
        return

    reported_users[partner_id] = reported_users.get(partner_id, 0) + 1
    count = reported_users[partner_id]
    disconnect_user(user_id)
    logger.warning(f"User {partner_id} reported by {user_id}. Total: {count}")

    if count >= AUTO_BAN_REPORTS:
        banned_users.add(partner_id)
        logger.warning(f"User {partner_id} auto-banned after {count} reports.")
        try:
            await context.bot.send_message(
                partner_id, "🚫 You have been banned from MingleVibe due to multiple reports."
            )
        except Exception:
            pass
    else:
        try:
            await context.bot.send_message(
                partner_id,
                f"⚠️ You were reported and disconnected.\n"
                f"Warning {count}/{AUTO_BAN_REPORTS}. Repeated violations lead to a ban.",
            )
        except Exception:
            pass

    await update.message.reply_text(
        "🚨 *Report submitted.* User disconnected.\n\n"
        "Thank you for keeping MingleVibe safe! 🛡️",
        parse_mode="Markdown",
        reply_markup=kb_main(),
    )

async def cmd_chat_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in active_chats:
        await update.message.reply_text("You're not in a chat right now.")
        return
    await update.message.reply_text(
        f"⏱ Chat duration: *{get_chat_duration(user_id)}*",
        parse_mode="Markdown",
    )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    remove_from_queue(user_id)
    disconnect_user(user_id)
    await update.message.reply_text(
        "✅ Cancelled. Tap *Find Match* whenever you're ready!",
        parse_mode="Markdown",
        reply_markup=kb_main(),
    )

# ─────────────────────────────────────────────
# Handlers — User menu
# ─────────────────────────────────────────────
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    reset_daily_count(profile)

    prem   = is_premium(user_id)
    expiry = premium_expiry.get(user_id)
    expiry_str = (
        expiry.strftime("%d %b %Y %H:%M") if expiry and datetime.now() < expiry else "—"
    )

    g_map = {"male": "👦 Boy", "female": "👧 Girl", "other": "🌈 Other", None: "Not set"}
    p_map = {"male": "👦 Boys", "female": "👧 Girls", "any": "🌍 Anyone"}
    m_map = {"fun": "😂 Fun", "dating": "💘 Dating", "friendship": "🤝 Friendship", "vent": "🫂 Vent", None: "Not set"}

    await update.message.reply_text(
        f"📊 *Your MingleVibe Stats*\n\n"
        f"Plan: {'💎 Premium' if prem else '🆓 Free'}\n"
        f"VIP expires: {expiry_str}\n"
        f"Matches today: {profile['matches_today']}\n"
        f"Remaining today: {remaining_matches(user_id)}\n"
        f"Total matches: {profile['total_matches']}\n\n"
        f"👤 Gender: {g_map.get(profile.get('gender'))}\n"
        f"🎯 Preference: {p_map.get(profile.get('pref_gender', 'any'))}\n"
        f"😊 Mood: {m_map.get(profile.get('mood'))}\n\n"
        f"🔗 Referrals: {user_referral_counts.get(user_id, 0)} friends\n"
        f"Your code: `{profile['referral_code']}`",
        parse_mode="Markdown",
        reply_markup=kb_main(),
    )

async def cmd_refer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _bot_username
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    refs    = user_referral_counts.get(user_id, 0)

    if not _bot_username:
        _bot_username = (await context.bot.get_me()).username

    link        = f"https://t.me/{_bot_username}?start={profile['referral_code']}"
    next_reward = 3 - (refs % 3)

    await update.message.reply_text(
        f"🔗 *Refer & Earn Free VIP!*\n\n"
        f"Share your link:\n`{link}`\n\n"
        f"✅ *{refs}* friends joined so far\n"
        f"🎯 *{next_reward}* more → {REFERRAL_REWARD_H}hr VIP FREE!\n\n"
        f"Every 3 referrals = {REFERRAL_REWARD_H} hours of unlimited matches 💎",
        parse_mode="Markdown",
        reply_markup=kb_main(),
    )

async def cmd_preferences(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⚙️ *My Preferences*\n\nWhat do you want to change?",
        parse_mode="Markdown",
        reply_markup=kb_prefs_edit(),
    )

async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "💎 *MingleVibe Premium*\n\n"
        "✅ Unlimited matches per day\n"
        "✅ Priority smart matching\n"
        "✅ Gender & mood filters\n"
        "✅ No cooldown between matches\n\n"
        "💰 *Choose your plan:* 👇",
        parse_mode="Markdown",
        reply_markup=kb_plans(),
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ *MingleVibe Help*\n\n"
        "🔍 *Find Match* — Connect with a stranger\n"
        "⏭ *Next* — Skip to next person\n"
        "🛑 *Stop* — End current chat\n"
        "🚨 *Report* — Report bad behavior\n"
        "⏱ *Chat Time* — See chat duration\n"
        "⚙️ *Preferences* — Update gender/mood filters\n"
        "📊 *Stats* — Your match history\n"
        "🔗 *Refer & Earn* — Get free VIP\n"
        "💎 *Go Premium* — Unlimited matches\n\n"
        "📌 *Commands:*\n"
        "/start · /next · /stop · /cancel · /refer · /stats · /help\n\n"
        "🛡️ All chats are 100% anonymous. Report abuse anytime.",
        parse_mode="Markdown",
        reply_markup=kb_main(),
    )

# ─────────────────────────────────────────────
# Admin commands
# ─────────────────────────────────────────────
def admin_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            return
        await fn(update, context)
    return wrapper

@admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>"); return
    target = int(context.args[0])
    banned_users.add(target)
    disconnect_user(target)
    await update.message.reply_text(f"✅ User {target} banned.")
    try:
        await context.bot.send_message(target, "🚫 You have been banned from MingleVibe.")
    except Exception:
        pass

@admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>"); return
    target = int(context.args[0])
    banned_users.discard(target)
    await update.message.reply_text(f"✅ User {target} unbanned.")

@admin_only
async def cmd_adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    premium_count = sum(1 for uid in user_profiles if is_premium(uid))
    await update.message.reply_text(
        f"📊 *Admin Stats*\n\n"
        f"Total users: {len(user_profiles)}\n"
        f"Active chats: {len(active_chats) // 2}\n"
        f"Waiting queue: {len(waiting_queue)}\n"
        f"Premium users: {premium_count}\n"
        f"Banned users: {len(banned_users)}\n"
        f"Pending payments: {len(pending_payments)}\n"
        f"Total reports: {sum(reported_users.values())}",
        parse_mode="Markdown",
    )

@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>"); return
    msg  = " ".join(context.args)
    sent = 0
    for uid in list(user_profiles):
        try:
            await context.bot.send_message(
                uid, f"📢 *MingleVibe Announcement*\n\n{msg}", parse_mode="Markdown"
            )
            sent += 1
            await asyncio.sleep(0.05)   # Telegram rate limit
        except Exception:
            pass
    await update.message.reply_text(f"✅ Broadcast sent to {sent} users.")

@admin_only
async def cmd_givepremium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /givepremium <user_id> <days>"); return
    target = int(context.args[0])
    days   = int(context.args[1])
    get_profile(target)["is_premium"] = True
    expires_at = datetime.now() + timedelta(days=days)
    premium_expiry[target] = expires_at
    await update.message.reply_text(
        f"✅ {days}-day Premium given to user {target}.\nExpires: {expires_at.strftime('%d %b %Y %H:%M')}"
    )
    try:
        await context.bot.send_message(
            target,
            f"🎉 *{days} Days Premium activated!*\n\n"
            f"Expires: {expires_at.strftime('%d %b %Y %H:%M')}\n"
            f"Enjoy unlimited matches! 💎",
            parse_mode="Markdown",
        )
    except Exception:
        pass

# ─────────────────────────────────────────────
# Message relay
# ─────────────────────────────────────────────
BUTTON_ROUTES = {
    "🔍 Find Match":    cmd_find_match,
    "⏭ Next":           cmd_next,
    "🛑 Stop":           cmd_stop,
    "🚨 Report":         cmd_report,
    "⏱ Chat Time":      cmd_chat_time,
    "⚙️ Preferences":   cmd_preferences,
    "💎 Go Premium":     cmd_premium,
    "📊 My Stats":       cmd_stats,
    "🔗 Refer & Earn":   cmd_refer,
    "ℹ️ Help":           cmd_help,
}

async def relay_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if user_id in banned_users:
        await update.message.reply_text("🚫 You are banned from MingleVibe.")
        return

    text = update.message.text or ""

    # Keyboard button routing
    if text in BUTTON_ROUTES:
        await BUTTON_ROUTES[text](update, context)
        return

    partner_id = active_chats.get(user_id)
    if not partner_id:
        await update.message.reply_text(
            "You're not in a chat. Tap *Find Match* to connect! 👇",
            parse_mode="Markdown",
            reply_markup=kb_main(),
        )
        return

    # Relay all supported media types
    msg = update.message
    try:
        if msg.text:
            await context.bot.send_message(partner_id, msg.text)
        elif msg.photo:
            await context.bot.send_photo(partner_id, msg.photo[-1].file_id, caption=msg.caption or "")
        elif msg.sticker:
            await context.bot.send_sticker(partner_id, msg.sticker.file_id)
        elif msg.voice:
            await context.bot.send_voice(partner_id, msg.voice.file_id)
        elif msg.video:
            await context.bot.send_video(partner_id, msg.video.file_id, caption=msg.caption or "")
        elif msg.video_note:
            await context.bot.send_video_note(partner_id, msg.video_note.file_id)
        elif msg.document:
            await context.bot.send_document(partner_id, msg.document.file_id, caption=msg.caption or "")
        elif msg.animation:
            await context.bot.send_animation(partner_id, msg.animation.file_id, caption=msg.caption or "")
        elif msg.audio:
            await context.bot.send_audio(partner_id, msg.audio.file_id, caption=msg.caption or "")
        elif msg.location:
            await context.bot.send_location(partner_id, msg.location.latitude, msg.location.longitude)
        else:
            await update.message.reply_text("⚠️ This media type is not supported yet.")
    except Exception as e:
        logger.error(f"Relay error {user_id} → {partner_id}: {e}")
        await update.message.reply_text(
            "⚠️ Message could not be delivered. Your partner may have left."
        )

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set in .env file!")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    # User commands
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("next",   cmd_next))
    app.add_handler(CommandHandler("stop",   cmd_stop))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("refer",  cmd_refer))
    app.add_handler(CommandHandler("help",   cmd_help))

    # Admin commands
    app.add_handler(CommandHandler("ban",          cmd_ban))
    app.add_handler(CommandHandler("unban",        cmd_unban))
    app.add_handler(CommandHandler("adminstats",   cmd_adminstats))
    app.add_handler(CommandHandler("broadcast",    cmd_broadcast))
    app.add_handler(CommandHandler("givepremium",  cmd_givepremium))

    # Callbacks and messages
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, relay_message))

    logger.info("🤖 MingleVibe Bot is LIVE with Auto Razorpay Payments!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()