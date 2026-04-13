#!/usr/bin/env python3
"""
🎬 Premium Video Bot — v4 (Scanner Fixed)
- Channel ID correctly uses -100 prefix
- Scanner uses forward_message to admin DM (most reliable probe)
- Falls back to copy_message if forward fails
- Sends ANY media type: video, photo, document, audio, voice, etc.
"""

import json
import logging
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError, BadRequest, Forbidden

# ══════════════════════════════════════════════════════
#  CONFIG  — edit these
# ══════════════════════════════════════════════════════
BOT_TOKEN      = "8793052185:AAERJUsr3tmlEKIE7gjm-Yw3guaNZCHVbTs"

# From your link https://t.me/c/3917377610/14 → channel id = 3917377610
# Telegram API requires the -100 prefix for channels/supergroups
CHANNEL_ID     = -1003917377610

ADMIN_PASSWORD = "void#123"
DATA_FILE      = "bot_data.json"
SCAN_DEPTH     = 1000                    # how many message IDs to scan back

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
#  DATA MANAGER
# ══════════════════════════════════════════════════════
class DataManager:
    def __init__(self):
        self.data = self._load()

    def _load(self):
        if Path(DATA_FILE).exists():
            try:
                with open(DATA_FILE) as f:
                    d = json.load(f)
                # Migrations / defaults
                d.setdefault("support_messages", [])
                d.setdefault("stats", {})
                d["stats"].setdefault("total_videos_sent", 0)
                d["stats"].setdefault("total_revenue", 0)
                d["stats"].setdefault("payments_pending", [])
                s = d.setdefault("settings", {})
                s.setdefault("channel_messages", [])
                s.setdefault("support_username", None)
                s.setdefault("free_limit", 5)
                s.setdefault("payment_qr", None)
                s.setdefault("upi_id", "payment@upi")
                s.setdefault("welcome_msg",
                    "😈 Welcome, {name}!\n\nGet up to {limit} FREE demo videos now.\n"
                    "All videos are protected and cannot be forwarded or saved.")
                s.setdefault("plans", {
                    "gold":    {"name": "🥇 Gold",    "days": 7,  "price": 50},
                    "silver":  {"name": "🥈 Silver",  "days": 15, "price": 89},
                    "diamond": {"name": "💎 Diamond", "days": 30, "price": 120},
                })
                d.setdefault("admins", [])
                return d
            except Exception as e:
                logger.error(f"Data load error: {e}")
        return self._default()

    def _default(self):
        return {
            "users": {},
            "settings": {
                "free_limit": 5,
                "plans": {
                    "gold":    {"name": "🥇 Gold",    "days": 7,  "price": 50},
                    "silver":  {"name": "🥈 Silver",  "days": 15, "price": 89},
                    "diamond": {"name": "💎 Diamond", "days": 30, "price": 120},
                },
                "payment_qr": None,
                "upi_id": "payment@upi",
                "welcome_msg": (
                    "😈 Welcome, {name}!\n\n"
                    "Get up to {limit} FREE demo videos now.\n"
                    "All videos are protected and cannot be forwarded or saved."
                ),
                "channel_messages": [],
                "support_username": None,
            },
            "admins": [],
            "support_messages": [],
            "stats": {
                "total_videos_sent": 0,
                "total_revenue": 0,
                "payments_pending": [],
            },
        }

    def save(self):
        with open(DATA_FILE, "w") as f:
            json.dump(self.data, f, indent=2, default=str)

    # ── Users ──────────────────────────────────────────
    def get_user(self, uid: int) -> dict:
        key = str(uid)
        if key not in self.data["users"]:
            self.data["users"][key] = {
                "id": uid, "name": "User", "username": None,
                "free_used": 0, "premium": False, "premium_plan": None,
                "premium_expiry": None, "total_videos": 0,
                "current_msg_id": None, "video_index": 0,
                "joined": datetime.now().isoformat(),
                "last_active": datetime.now().isoformat(),
            }
            self.save()
        return self.data["users"][key]

    def update_user(self, uid: int, **kw):
        u = self.get_user(uid)
        u.update(kw)
        u["last_active"] = datetime.now().isoformat()
        self.save()

    def is_premium(self, uid: int) -> bool:
        u = self.get_user(uid)
        if not u.get("premium"):
            return False
        expiry = u.get("premium_expiry")
        if expiry and datetime.now() > datetime.fromisoformat(str(expiry)):
            self.update_user(uid, premium=False, premium_plan=None, premium_expiry=None)
            return False
        return True

    def free_left(self, uid: int) -> int:
        return max(0, self.data["settings"]["free_limit"] - self.get_user(uid)["free_used"])

    def give_premium(self, uid: int, plan_key: str) -> bool:
        plan = self.data["settings"]["plans"].get(plan_key)
        if not plan:
            return False
        expiry = datetime.now() + timedelta(days=plan["days"])
        self.update_user(uid, premium=True, premium_plan=plan_key,
                         premium_expiry=expiry.isoformat())
        self.data["stats"]["total_revenue"] += plan["price"]
        self.save()
        return True

    def get_stats(self) -> dict:
        users = self.data["users"]
        today = datetime.now().date()
        prem  = sum(1 for u in users.values() if u.get("premium"))
        act   = sum(1 for u in users.values()
                    if u.get("last_active") and
                    datetime.fromisoformat(str(u["last_active"])).date() == today)
        return {
            "total_users": len(users),
            "premium_users": prem,
            "free_users": len(users) - prem,
            "active_today": act,
            "videos_sent": self.data["stats"]["total_videos_sent"],
            "revenue": self.data["stats"]["total_revenue"],
            "pending_count": len(self.data["stats"]["payments_pending"]),
            "support_unread": sum(1 for m in self.data["support_messages"] if not m.get("read")),
            "video_count": len(self.data["settings"]["channel_messages"]),
        }

db = DataManager()

# ══════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════
def kb_main():
    return ReplyKeyboardMarkup(
        [["🎬 Get Videos", "⭐ Buy Premium"],
         ["👤 Profile",    "🆘 Support"]],
        resize_keyboard=True)

def kb_video():
    return ReplyKeyboardMarkup(
        [["⏭️ Next Video", "⏮️ Previous Video"],
         ["📋 Premium Plans", "🏠 Main Menu"]],
        resize_keyboard=True)

def inline_plans():
    rows = []
    for k, p in db.data["settings"]["plans"].items():
        rows.append([InlineKeyboardButton(
            f"{p['name']} — ₹{p['price']} · {p['days']} days",
            callback_data=f"buy_{k}"
        )])
    rows.append([InlineKeyboardButton("🏠 Back to Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)

def inline_payment(plan_key: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I Have Paid", callback_data=f"paid_{plan_key}")],
        [InlineKeyboardButton("🔙 Back to Plans", callback_data="premium_plans")],
    ])

# ── Admin Panel Inline Keyboards ──────────────────────
def kb_admin_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats",           callback_data="adm_stats"),
         InlineKeyboardButton("🎥 Videos",          callback_data="adm_videos")],
        [InlineKeyboardButton("💰 Pending Payments",callback_data="adm_pending"),
         InlineKeyboardButton("🆘 Support Inbox",   callback_data="adm_inbox")],
        [InlineKeyboardButton("👥 User Lookup",     callback_data="adm_userlookup"),
         InlineKeyboardButton("📢 Broadcast",       callback_data="adm_broadcast")],
        [InlineKeyboardButton("⚙️ Settings",        callback_data="adm_settings")],
        [InlineKeyboardButton("🚪 Logout",          callback_data="adm_logout")],
    ])

def kb_admin_videos():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Scan New Videos",  callback_data="adm_scanvideos"),
         InlineKeyboardButton("🔄 Full Rescan",      callback_data="adm_fullscan")],
        [InlineKeyboardButton("➕ Add Video IDs",    callback_data="adm_addvideos"),
         InlineKeyboardButton("➖ Remove Video IDs", callback_data="adm_removevideos")],
        [InlineKeyboardButton("📋 List All IDs",    callback_data="adm_listvideos")],
        [InlineKeyboardButton("🔙 Back",             callback_data="adm_panel")],
    ])

def kb_admin_settings():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Set Price",        callback_data="adm_setprice"),
         InlineKeyboardButton("🆓 Free Limit",       callback_data="adm_setlimit")],
        [InlineKeyboardButton("💳 Set UPI ID",       callback_data="adm_setupi"),
         InlineKeyboardButton("🖼️ Set QR Photo",     callback_data="adm_setqr")],
        [InlineKeyboardButton("🧑‍💼 Set Support User", callback_data="adm_setsupport")],
        [InlineKeyboardButton("🔙 Back",              callback_data="adm_panel")],
    ])

def kb_admin_back():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="adm_panel")]
    ])

# ══════════════════════════════════════════════════════
#  VIDEO SCANNER  — v4 (Robust)
#
#  HOW IT WORKS:
#  1. Send a temp message to the channel to get the latest message_id
#     (bot must be an admin with "Post Messages" permission)
#  2. Walk backwards from latest_id by SCAN_DEPTH
#  3. For each ID, try forward_message to admin DM — if it succeeds,
#     the message exists and has content → record it, delete the forward
#  4. If forwarding is disabled on the channel, fall back to copy_message
#
#  BOT REQUIREMENTS:
#  - Bot must be an ADMIN in the private channel
#  - Bot needs: "Post Messages" + "Delete Messages" permissions
# ══════════════════════════════════════════════════════

async def _get_latest_msg_id(bot) -> int:
    """Send + immediately delete a temp message to learn the latest message_id."""
    try:
        m = await bot.send_message(CHANNEL_ID, "​")   # zero-width space, invisible
        mid = m.message_id
        await bot.delete_message(CHANNEL_ID, mid)
        logger.info(f"[Scan] Latest channel message_id = {mid}")
        return mid
    except TelegramError as e:
        logger.error(
            f"[Scan] Cannot send probe to channel: {e}\n"
            f"Make sure bot is admin with 'Post Messages' + 'Delete Messages' rights."
        )
        return 0


async def _probe_message(bot, admin_chat_id: int, msg_id: int) -> bool:
    """
    Returns True if msg_id exists in the channel and contains sendable content.
    Strategy: forward first (preserves original sender info), fall back to copy.
    Either way delete the forwarded/copied message immediately.
    """
    # ── Try forward_message ──────────────────────────
    try:
        fwd = await bot.forward_message(
            chat_id=admin_chat_id,
            from_chat_id=CHANNEL_ID,
            message_id=msg_id,
        )
        await bot.delete_message(admin_chat_id, fwd.message_id)
        return True
    except BadRequest as e:
        err = str(e).lower()
        if "message to forward not found" in err or "message not found" in err:
            return False          # ID doesn't exist at all
        if "forward" in err or "restricted" in err or "protected" in err:
            pass                  # forwarding disabled → try copy below
        else:
            return False
    except TelegramError as e:
        err = str(e).lower()
        if "flood" in err or "too many" in err:
            await asyncio.sleep(5)
        # fall through to copy attempt

    # ── Fall back to copy_message ────────────────────
    try:
        cpy = await bot.copy_message(
            chat_id=admin_chat_id,
            from_chat_id=CHANNEL_ID,
            message_id=msg_id,
            protect_content=False,
        )
        await bot.delete_message(admin_chat_id, cpy.message_id)
        return True
    except BadRequest as e:
        err = str(e).lower()
        if "message to copy not found" in err or "message not found" in err:
            return False
        logger.debug(f"[Scan] copy fallback ID {msg_id}: {e}")
        return False
    except TelegramError as e:
        err = str(e).lower()
        if "flood" in err or "too many" in err:
            await asyncio.sleep(5)
        logger.debug(f"[Scan] TelegramError ID {msg_id}: {e}")
        return False


async def scan_channel_videos(
    bot,
    admin_chat_id: int,
    merge: bool = True,
    status_msg=None,
) -> int:
    """
    Scan the channel for media messages.
    merge=True  → only look for IDs not already in the library
    merge=False → check every ID in the scan window (still keeps existing)
    """
    existing = set(db.data["settings"]["channel_messages"])

    # ── Step 1: find latest message ID ──────────────
    if status_msg:
        try: await status_msg.edit_text("🔍 Step 1/2 — Finding latest channel message…")
        except Exception: pass

    latest_id = await _get_latest_msg_id(bot)
    if latest_id <= 0:
        if status_msg:
            try:
                await status_msg.edit_text(
                    "❌ *Scan failed!*\n\n"
                    "Bot cannot post to the channel.\n\n"
                    "✅ Fix: Go to channel → Admins → Bot → enable:\n"
                    "• Post Messages\n• Delete Messages",
                    parse_mode=ParseMode.MARKDOWN)
            except Exception: pass
        return 0

    scan_start = latest_id - 1          # the probe message itself is not a video
    scan_end   = max(1, scan_start - SCAN_DEPTH)
    scan_range = range(scan_start, scan_end, -1)

    logger.info(f"[Scan] Scanning IDs {scan_start} → {scan_end} (depth={SCAN_DEPTH})")

    if status_msg:
        try:
            await status_msg.edit_text(
                f"🔍 Step 2/2 — Scanning {len(scan_range)} message slots…\n"
                f"_(from ID {scan_start} down to {scan_end})_\n\n"
                f"This may take 1–3 minutes ☕",
                parse_mode=ParseMode.MARKDOWN)
        except Exception: pass

    found = []
    checked = 0

    for msg_id in scan_range:
        # In merge mode, skip IDs we already know about
        if merge and msg_id in existing:
            continue

        if await _probe_message(bot, admin_chat_id, msg_id):
            found.append(msg_id)
            logger.info(f"[Scan] ✅ Found: {msg_id}  (total so far: {len(found)})")

        await asyncio.sleep(0.08)       # ~12 probes/sec, safe rate limit

        checked += 1
        if status_msg and checked % 80 == 0:
            pct = int(checked / len(scan_range) * 100)
            try:
                await status_msg.edit_text(
                    f"🔍 Scanning… {pct}% complete\n"
                    f"✅ Found {len(found)} new videos so far…\n"
                    f"_(checked {checked}/{len(scan_range)} slots)_",
                    parse_mode=ParseMode.MARKDOWN)
            except Exception: pass

    # ── Merge: always keep existing + new ───────────
    combined = sorted(existing | set(found))
    db.data["settings"]["channel_messages"] = combined
    db.save()
    logger.info(f"[Scan] Done. new={len(found)}, total={len(combined)}")
    return len(found)


# ══════════════════════════════════════════════════════
#  SEND MEDIA — delivers ANY content type from the channel
#  (video, photo, document, audio, voice, animation, etc.)
#  Uses copy_message which works for all media types.
#  protect_content=True prevents forwarding/saving by user.
# ══════════════════════════════════════════════════════
def _remove_dead_id(msg_id: int):
    """Remove a no-longer-valid message ID from the library."""
    vl = db.data["settings"]["channel_messages"]
    if msg_id in vl:
        vl.remove(msg_id)
        db.save()
        logger.info(f"[Library] Removed dead ID {msg_id}. Remaining: {len(vl)}")


async def send_video(bot, chat_id: int, msg_id: int, user_id: int):
    """
    Copies the channel message to the user's chat.
    Works for ALL media types (video, photo, doc, audio, voice…).
    Returns the sent Message object, or None on failure.
    """
    u = db.get_user(user_id)

    # Delete previous message to keep the chat clean
    prev = u.get("current_msg_id")
    if prev:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=prev)
        except Exception:
            pass  # already gone, ignore

    try:
        sent = await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=CHANNEL_ID,
            message_id=msg_id,
            protect_content=True,       # 🔒 no forwarding / saving
        )
        db.update_user(user_id,
                       current_msg_id=sent.message_id,
                       total_videos=u.get("total_videos", 0) + 1)
        db.data["stats"]["total_videos_sent"] += 1
        db.save()
        logger.info(f"[Send] ✅ Sent channel msg {msg_id} → user {user_id}")
        return sent

    except BadRequest as e:
        err = str(e).lower()
        if "message to copy not found" in err or "message not found" in err:
            logger.warning(f"[Send] Dead ID {msg_id} — removing from library.")
            _remove_dead_id(msg_id)
        elif "chat not found" in err:
            logger.error(f"[Send] Channel not found! Check CHANNEL_ID={CHANNEL_ID}")
        else:
            logger.error(f"[Send] BadRequest id={msg_id}: {e}")
        return None

    except Forbidden as e:
        logger.error(f"[Send] Bot is not in channel or lacks permission: {e}")
        return None

    except TelegramError as e:
        logger.error(f"[Send] TelegramError id={msg_id}: {e}")
        return None


# ══════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.update_user(user.id, name=user.first_name, username=user.username)
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    s = db.data["settings"]
    welcome = s["welcome_msg"].format(
        name=user.first_name,
        limit=s["free_limit"]
    )

    # Send welcome — no fragile GIF dependency
    await update.message.reply_text(
        f"🎬 {welcome}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main()
    )


# ══════════════════════════════════════════════════════
#  MESSAGE ROUTER
# ══════════════════════════════════════════════════════
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    uid  = update.effective_user.id
    db.update_user(uid, name=update.effective_user.first_name,
                   username=update.effective_user.username)

    # ── Awaiting states ──────────────────────────────
    state = ctx.user_data.get("state")

    if state == "awaiting_admin_pass":
        await _admin_password(update, ctx); return

    if state == "awaiting_support_reply_to":
        await _admin_support_reply(update, ctx); return

    if state == "awaiting_support_msg":
        await _user_support_msg(update, ctx); return

    if state and state.startswith("adm_input_"):
        await _handle_admin_input(update, ctx, state); return

    # ── Button routes ────────────────────────────────
    routes = {
        "🎬 Get Videos":     h_get_video,
        "⏭️ Next Video":     h_next_video,
        "⏮️ Previous Video": h_prev_video,
        "⭐ Buy Premium":    h_buy_premium,
        "📋 Premium Plans":  h_buy_premium,
        "👤 Profile":        h_profile,
        "🆘 Support":        h_support,
        "🏠 Main Menu":      h_main_menu,
    }
    if text in routes:
        await routes[text](update, ctx)
    else:
        await update.message.reply_text(
            "❓ Use the buttons below to navigate.",
            reply_markup=kb_main()
        )


async def h_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏠 *Main Menu*", parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=kb_main())

# ══════════════════════════════════════════════════════
#  VIDEO HANDLERS — FIXED
# ══════════════════════════════════════════════════════
async def _deliver_video(bot, chat_id: int, uid: int, idx: int,
                         update: Update, label: str):
    """Core video delivery used by Get/Next/Prev."""
    videos = db.data["settings"]["channel_messages"]
    if not videos:
        await bot.send_message(chat_id,
            "😔 No videos available yet. Admin needs to scan/add videos.",
            reply_markup=kb_main())
        return

    # Clamp index
    idx = idx % len(videos)
    msg_id = videos[idx]

    await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
    sent = await send_video(bot, chat_id, msg_id, uid)

    # Re-fetch videos list (may have changed if a dead ID was removed)
    videos = db.data["settings"]["channel_messages"]

    if sent:
        is_prem = db.is_premium(uid)
        u = db.get_user(uid)
        if not is_prem:
            left = db.free_left(uid)
            note = f"{label} · 🆓 Free left: *{left}/{db.data['settings']['free_limit']}*"
            if left == 0:
                note += "\n\n⚠️ That was your last free video!\n👇 Upgrade to Premium for unlimited access."
        else:
            note = f"{label} · ⭐ *Premium Active*"
        await bot.send_message(chat_id, note,
                               parse_mode=ParseMode.MARKDOWN,
                               reply_markup=kb_video())
    else:
        # Dead ID was already removed inside send_video — try next
        if videos:
            new_idx = idx % len(videos)
            db.update_user(uid, video_index=new_idx)
            await bot.send_message(chat_id,
                "⚠️ That video was removed. Fetching next one…",
                reply_markup=kb_video())
            # Retry once with the next valid index
            await _deliver_video(bot, chat_id, uid, new_idx, update, label)
        else:
            await bot.send_message(chat_id,
                "😔 All videos removed. Admin needs to add new ones.",
                reply_markup=kb_main())


async def h_get_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    chat_id = update.effective_chat.id
    is_prem = db.is_premium(uid)

    if not is_prem and db.free_left(uid) <= 0:
        await update.message.reply_text(
            "🚫 *No free videos left!*\n\nUpgrade to Premium for unlimited access 👇",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=inline_plans())
        return

    videos = db.data["settings"]["channel_messages"]
    if not videos:
        m = await update.message.reply_text("🔍 No videos found locally, scanning channel…")
        await scan_channel_videos(ctx.bot, admin_chat_id=uid, merge=False, status_msg=m)
        videos = db.data["settings"]["channel_messages"]
        if not videos:
            try: await m.delete()
            except: pass
            await update.message.reply_text(
                "😔 No videos available. Admin needs to add videos to the channel.",
                reply_markup=kb_main())
            return
        try: await m.delete()
        except: pass

    u   = db.get_user(uid)
    idx = u.get("video_index", 0)

    # Consume a free credit before sending
    if not is_prem:
        db.update_user(uid, free_used=u["free_used"] + 1)

    await _deliver_video(ctx.bot, chat_id, uid, idx, update, "🎬 Enjoy!")


async def h_next_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    chat_id = update.effective_chat.id
    is_prem = db.is_premium(uid)
    videos  = db.data["settings"]["channel_messages"]

    if not videos:
        await update.message.reply_text("😔 No videos available.", reply_markup=kb_video())
        return

    if not is_prem and db.free_left(uid) <= 0:
        await update.message.reply_text(
            "🚫 *No free videos left!* Upgrade to continue 👇",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=inline_plans())
        return

    u       = db.get_user(uid)
    new_idx = (u.get("video_index", 0) + 1) % len(videos)
    db.update_user(uid, video_index=new_idx)

    if not is_prem:
        u = db.get_user(uid)
        db.update_user(uid, free_used=u["free_used"] + 1)

    await _deliver_video(ctx.bot, chat_id, uid, new_idx, update, "⏭️ Next")


async def h_prev_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    if not db.is_premium(uid):
        await update.message.reply_text(
            "🔒 *Previous Video* is for Premium members only!\n\nUpgrade 👇",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=inline_plans())
        return

    videos  = db.data["settings"]["channel_messages"]
    u       = db.get_user(uid)
    new_idx = (u.get("video_index", 0) - 1) % len(videos)
    db.update_user(uid, video_index=new_idx)

    await _deliver_video(ctx.bot, update.effective_chat.id, uid, new_idx, update, "⏮️ Previous")


# ══════════════════════════════════════════════════════
#  PROFILE & BUY PREMIUM
# ══════════════════════════════════════════════════════
async def h_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u   = db.get_user(uid)
    s   = db.data["settings"]

    if db.is_premium(uid):
        exp    = datetime.fromisoformat(str(u["premium_expiry"]))
        days   = max(0, (exp - datetime.now()).days)
        hours  = max(0, int((exp - datetime.now()).seconds / 3600))
        plan_n = s["plans"].get(u["premium_plan"], {}).get("name", "?")
        status = f"⭐ {plan_n}"
        time_left = f"{days}d {hours}h remaining"
        vids   = "♾️ Unlimited"
    else:
        status    = "❌ Free Tier"
        time_left = "—"
        vids      = f"🆓 {db.free_left(uid)}/{s['free_limit']} remaining"

    await update.message.reply_text(
        f"👤 *Your Profile*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: `{uid}`\n"
        f"👋 Name: {u['name']}\n"
        f"⭐ Status: {status}\n"
        f"⏳ Time Left: {time_left}\n"
        f"🎬 Videos: {vids}\n"
        f"📊 Watched: {u.get('total_videos', 0)}\n"
        f"📅 Joined: {u['joined'][:10]}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main()
    )


async def h_buy_premium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⭐ *Premium Plans*\n\n"
        "Unlock unlimited videos with no restrictions!\n\n"
        "Pick a plan below 👇",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=inline_plans()
    )


# ══════════════════════════════════════════════════════
#  SUPPORT
# ══════════════════════════════════════════════════════
async def h_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    su    = db.data["settings"].get("support_username")
    extra = f"\n📩 Direct contact: @{su}" if su else ""
    await update.message.reply_text(
        f"🆘 *Support*\n\n"
        f"Need help? Send your message below and the admin will reply.{extra}\n\n"
        f"✏️ *Type your message now:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)
    )
    ctx.user_data["state"] = "awaiting_support_msg"


async def _user_support_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    uid  = update.effective_user.id
    u    = db.get_user(uid)

    if text == "❌ Cancel":
        ctx.user_data["state"] = None
        await update.message.reply_text("❌ Cancelled.", reply_markup=kb_main())
        return

    rec = {
        "id": len(db.data["support_messages"]) + 1,
        "uid": uid, "name": u["name"],
        "username": u.get("username"),
        "text": text,
        "time": datetime.now().isoformat(),
        "read": False, "reply": None,
    }
    db.data["support_messages"].append(rec)
    db.save()
    ctx.user_data["state"] = None

    await update.message.reply_text(
        "✅ *Message sent!*\n\nAdmin will reply soon. You'll get a notification here.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main()
    )

    # Notify all admins
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Reply", callback_data=f"sreply_{rec['id']}_{uid}")
    ]])
    notif = (
        f"🆘 *New Support Message #{rec['id']}*\n"
        f"👤 {u['name']} (`{uid}`)"
        + (f" @{u['username']}" if u.get("username") else "")
        + f"\n📩 {text}\n🕐 {rec['time'][:16]}"
    )
    for aid in db.data.get("admins", []):
        try:
            await ctx.bot.send_message(aid, notif,
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            pass


async def _admin_support_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    info   = ctx.user_data.get("reply_info", {})
    msg_id = info.get("msg_id")
    t_uid  = info.get("uid")
    text   = update.message.text or ""

    if text == "❌ Cancel":
        ctx.user_data["state"] = None
        await update.message.reply_text("❌ Cancelled.", reply_markup=kb_main())
        return

    for m in db.data["support_messages"]:
        if m["id"] == msg_id:
            m["reply"] = text
            m["read"]  = True
            break
    db.save()
    ctx.user_data["state"] = None

    await update.message.reply_text("✅ Reply sent!", reply_markup=kb_main())
    try:
        await ctx.bot.send_message(
            t_uid,
            f"📩 *Admin replied to your support message:*\n\n{text}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_main()
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Could not deliver reply: {e}")


# ══════════════════════════════════════════════════════
#  ADMIN PANEL — INLINE KEYBOARD DRIVEN
# ══════════════════════════════════════════════════════
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("admin_authenticated"):
        await _show_panel(update.message, ctx)
        return
    ctx.user_data["state"] = "awaiting_admin_pass"
    await update.message.reply_text(
        "🔐 *Admin Panel*\n\nEnter your password:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove()
    )


async def _admin_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == ADMIN_PASSWORD:
        ctx.user_data["state"] = None
        ctx.user_data["admin_authenticated"] = True
        uid = update.effective_user.id
        if uid not in db.data["admins"]:
            db.data["admins"].append(uid)
            db.save()
        try: await update.message.delete()
        except: pass
        await _show_panel(update.message, ctx)
    else:
        ctx.user_data["state"] = None
        await update.message.reply_text("❌ Wrong password!", reply_markup=kb_main())


async def _show_panel(message, ctx: ContextTypes.DEFAULT_TYPE):
    st = db.get_stats()
    text = (
        f"🛡️ *Admin Panel*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 *Quick Stats*\n"
        f"👥 Users: `{st['total_users']}`  ⭐ Premium: `{st['premium_users']}`\n"
        f"🆓 Free: `{st['free_users']}`  🟢 Active Today: `{st['active_today']}`\n"
        f"🎬 Videos Sent: `{st['videos_sent']}`  🎥 In Library: `{st['video_count']}`\n"
        f"💰 Revenue: `₹{st['revenue']}`\n"
        f"⏳ Pending: `{st['pending_count']}`  "
        f"🆘 Unread: `{st['support_unread']}`\n\n"
        f"👇 *Select an action:*"
    )
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                              reply_markup=kb_admin_main())


def _admin_only(func):
    """Decorator: blocks non-authenticated admins from command handlers."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.user_data.get("admin_authenticated"):
            await update.message.reply_text(
                "❌ Not authorized. Use /admin to log in first.")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


# ── Admin Input Handler (for inline-panel-triggered inputs) ──
async def _handle_admin_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE, state: str):
    text = update.message.text or ""
    if text == "❌ Cancel":
        ctx.user_data["state"] = None
        await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
        await _show_panel(update.message, ctx)
        return

    # adm_input_<action>
    action = state.replace("adm_input_", "")

    if action == "give_uid":
        ctx.user_data["give_uid"] = text.strip()
        ctx.user_data["state"] = "adm_input_give_plan"
        await update.message.reply_text(
            "📋 Enter plan (gold / silver / diamond):",
            reply_markup=ReplyKeyboardMarkup(
                [["gold", "silver", "diamond"], ["❌ Cancel"]],
                resize_keyboard=True))

    elif action == "give_plan":
        uid_str = ctx.user_data.get("give_uid", "")
        plan    = text.strip().lower()
        ctx.user_data["state"] = None
        try:
            uid = int(uid_str)
            if db.give_premium(uid, plan):
                p = db.data["settings"]["plans"][plan]
                await update.message.reply_text(
                    f"✅ {p['name']} granted to `{uid}`",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=ReplyKeyboardRemove())
                try:
                    await ctx.bot.send_message(
                        uid,
                        f"🎉 *Premium Activated!*\n⭐ {p['name']} — {p['days']} days\n\nEnjoy unlimited videos! 🎬",
                        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
                except Exception: pass
            else:
                await update.message.reply_text("❌ Invalid plan name.",
                                                reply_markup=ReplyKeyboardRemove())
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.",
                                            reply_markup=ReplyKeyboardRemove())
        await _show_panel(update.message, ctx)

    elif action == "revoke_uid":
        ctx.user_data["state"] = None
        try:
            uid = int(text.strip())
            db.update_user(uid, premium=False, premium_plan=None, premium_expiry=None)
            await update.message.reply_text(f"✅ Premium revoked from `{uid}`",
                parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
        except ValueError:
            await update.message.reply_text("❌ Invalid ID.", reply_markup=ReplyKeyboardRemove())
        await _show_panel(update.message, ctx)

    elif action == "setlimit":
        ctx.user_data["state"] = None
        try:
            limit = int(text.strip())
            db.data["settings"]["free_limit"] = limit
            db.save()
            await update.message.reply_text(f"✅ Free limit set to `{limit}`",
                parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
        except ValueError:
            await update.message.reply_text("❌ Enter a number.", reply_markup=ReplyKeyboardRemove())
        await _show_panel(update.message, ctx)

    elif action == "setupi":
        ctx.user_data["state"] = None
        upi = text.strip()
        db.data["settings"]["upi_id"] = upi
        db.save()
        await update.message.reply_text(f"✅ UPI set to `{upi}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
        await _show_panel(update.message, ctx)

    elif action == "setsupport":
        ctx.user_data["state"] = None
        u = text.strip().lstrip("@")
        db.data["settings"]["support_username"] = u
        db.save()
        await update.message.reply_text(f"✅ Support contact set to @{u}",
            reply_markup=ReplyKeyboardRemove())
        await _show_panel(update.message, ctx)

    elif action == "setprice_plan":
        ctx.user_data["price_plan"] = text.strip().lower()
        ctx.user_data["state"] = "adm_input_setprice_amount"
        await update.message.reply_text("💵 Enter new price (₹):",
            reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))

    elif action == "setprice_amount":
        ctx.user_data["state"] = None
        plan  = ctx.user_data.get("price_plan", "")
        try:
            price = int(text.strip())
            if plan in db.data["settings"]["plans"]:
                db.data["settings"]["plans"][plan]["price"] = price
                db.save()
                await update.message.reply_text(f"✅ {plan.title()} price → ₹{price}",
                    reply_markup=ReplyKeyboardRemove())
            else:
                await update.message.reply_text("❌ Invalid plan.", reply_markup=ReplyKeyboardRemove())
        except ValueError:
            await update.message.reply_text("❌ Enter a valid number.", reply_markup=ReplyKeyboardRemove())
        await _show_panel(update.message, ctx)

    elif action == "broadcast":
        ctx.user_data["state"] = None
        msg = text.strip()
        sent = 0
        for uid_str in db.data["users"]:
            try:
                await ctx.bot.send_message(
                    int(uid_str),
                    f"📢 *Announcement*\n\n{msg}",
                    parse_mode=ParseMode.MARKDOWN)
                sent += 1
                await asyncio.sleep(0.06)
            except Exception:
                pass
        await update.message.reply_text(f"✅ Broadcast sent to {sent} users.",
            reply_markup=ReplyKeyboardRemove())
        await _show_panel(update.message, ctx)

    elif action == "addvideos":
        ctx.user_data["state"] = None
        parts = text.strip().split()
        existing = db.data["settings"]["channel_messages"]
        added = 0
        for p in parts:
            try:
                mid = int(p)
                if mid not in existing:
                    existing.append(mid)
                    added += 1
            except ValueError:
                pass
        existing.sort()
        db.data["settings"]["channel_messages"] = existing
        db.save()
        await update.message.reply_text(
            f"✅ Added {added} video IDs. Total: {len(existing)}",
            reply_markup=ReplyKeyboardRemove())
        await _show_panel(update.message, ctx)

    elif action == "removevideos":
        ctx.user_data["state"] = None
        rm = {int(p) for p in text.strip().split() if p.isdigit()}
        before = len(db.data["settings"]["channel_messages"])
        db.data["settings"]["channel_messages"] = [
            x for x in db.data["settings"]["channel_messages"] if x not in rm]
        db.save()
        removed = before - len(db.data["settings"]["channel_messages"])
        await update.message.reply_text(
            f"✅ Removed {removed}. Total: {len(db.data['settings']['channel_messages'])}",
            reply_markup=ReplyKeyboardRemove())
        await _show_panel(update.message, ctx)

    elif action == "userinfo":
        ctx.user_data["state"] = None
        try:
            uid = int(text.strip())
            u   = db.get_user(uid)
            prem = db.is_premium(uid)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ Gold",    callback_data=f"approve_{uid}_gold"),
                 InlineKeyboardButton("⭐ Silver",  callback_data=f"approve_{uid}_silver")],
                [InlineKeyboardButton("💎 Diamond", callback_data=f"approve_{uid}_diamond"),
                 InlineKeyboardButton("❌ Revoke",  callback_data=f"reject_{uid}")],
                [InlineKeyboardButton("🔙 Back to Panel", callback_data="adm_panel")]
            ])
            await update.message.reply_text(
                f"👤 *User Info*\n"
                f"🆔 ID: `{uid}`\n"
                f"👋 Name: {u['name']}"
                + (f"  @{u['username']}" if u.get("username") else "")
                + f"\n⭐ Premium: {'✅ Yes' if prem else '❌ No'}\n"
                f"📋 Plan: {u.get('premium_plan') or '—'}\n"
                f"📅 Expiry: {str(u.get('premium_expiry') or '—')[:16]}\n"
                f"🆓 Free Used: {u['free_used']}\n"
                f"🎬 Watched: {u.get('total_videos', 0)}\n"
                f"📅 Joined: {u['joined'][:10]}\n"
                f"🕐 Last Active: {u['last_active'][:16]}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb)
        except (ValueError, TypeError):
            await update.message.reply_text("❌ Invalid user ID.")
            await _show_panel(update.message, ctx)


# ══════════════════════════════════════════════════════
#  CALLBACK QUERY HANDLER
# ══════════════════════════════════════════════════════
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    uid  = q.from_user.id
    await q.answer()

    # ── User-facing callbacks ────────────────────────
    if data.startswith("buy_"):
        plan_key = data[4:]
        plan = db.data["settings"]["plans"].get(plan_key)
        if not plan:
            await q.answer("Invalid plan!", show_alert=True)
            return
        s    = db.data["settings"]
        text = (
            f"💰 *Payment Details*\n\n"
            f"🎯 Plan: {plan['name']}\n"
            f"💵 Amount: ₹{plan['price']}\n"
            f"⏳ Duration: {plan['days']} days\n\n"
            f"📱 UPI ID: `{s.get('upi_id', 'N/A')}`\n\n"
            f"Scan the QR / pay via UPI, then tap *I Have Paid* ✅"
        )
        qr = s.get("payment_qr")
        try:
            if qr:
                await ctx.bot.send_photo(
                    q.message.chat_id, qr,
                    caption=text, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=inline_payment(plan_key))
            else:
                await q.edit_message_text(
                    text, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=inline_payment(plan_key))
        except Exception:
            await q.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=inline_payment(plan_key))

    elif data.startswith("paid_"):
        plan_key = data[5:]
        u    = db.get_user(uid)
        plan = db.data["settings"]["plans"].get(plan_key, {})
        db.data["stats"]["payments_pending"].append({
            "uid": uid, "name": u["name"],
            "username": u.get("username"),
            "plan": plan_key, "plan_name": plan.get("name", plan_key),
            "amount": plan.get("price", 0),
            "time": datetime.now().isoformat(),
        })
        db.save()
        confirm = (
            f"✅ *Payment Submitted!*\n\n"
            f"Plan: {plan.get('name')}\n"
            f"Amount: ₹{plan.get('price')}\n\n"
            f"⏳ Admin will verify within 1 hour."
        )
        try:
            if q.message.caption:
                await q.edit_message_caption(confirm, parse_mode=ParseMode.MARKDOWN)
            else:
                await q.edit_message_text(confirm, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass
        # Notify admins
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{uid}_{plan_key}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{uid}")
        ]])
        note = (
            f"💰 *New Payment Claim*\n"
            f"👤 {u['name']} (`{uid}`)"
            + (f" @{u['username']}" if u.get("username") else "")
            + f"\n📋 {plan.get('name')} — ₹{plan.get('price')}"
            + f"\n🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        for aid in db.data.get("admins", []):
            try:
                await ctx.bot.send_message(aid, note,
                    parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            except Exception:
                pass

    elif data == "premium_plans":
        try:
            await q.edit_message_text(
                "⭐ *Premium Plans*\n\nChoose a plan 👇",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=inline_plans())
        except Exception:
            pass

    elif data == "main_menu":
        try: await q.delete_message()
        except Exception: pass
        await ctx.bot.send_message(uid, "🏠 *Main Menu*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())

    # ── Approve / Reject ─────────────────────────────
    elif data.startswith("approve_"):
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        parts = data.split("_")
        t_uid = int(parts[1])
        pk    = parts[2]
        db.give_premium(t_uid, pk)
        plan = db.data["settings"]["plans"].get(pk, {})
        db.data["stats"]["payments_pending"] = [
            p for p in db.data["stats"]["payments_pending"] if p["uid"] != t_uid]
        db.save()
        try:
            await q.edit_message_text(
                f"✅ {plan.get('name')} granted to `{t_uid}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_admin_back())
        except Exception: pass
        try:
            await ctx.bot.send_message(
                t_uid,
                f"🎉 *Payment Approved!*\n"
                f"⭐ {plan.get('name')} activated!\n"
                f"📅 {plan.get('days')} days of unlimited access!\n\n"
                f"Enjoy! 🎬",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_main())
        except Exception: pass

    elif data.startswith("reject_"):
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        t_uid = int(data[7:])
        db.data["stats"]["payments_pending"] = [
            p for p in db.data["stats"]["payments_pending"] if p["uid"] != t_uid]
        db.save()
        try:
            await q.edit_message_text(
                f"❌ Payment rejected for `{t_uid}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_admin_back())
        except Exception: pass
        try:
            await ctx.bot.send_message(
                t_uid,
                "❌ *Payment Rejected*\n\n"
                "We couldn't verify your payment. Please try again or tap 🆘 Support.",
                parse_mode=ParseMode.MARKDOWN)
        except Exception: pass

    # ── Support reply ────────────────────────────────
    elif data.startswith("sreply_"):
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        parts  = data.split("_")
        msg_id = int(parts[1])
        t_uid  = int(parts[2])
        ctx.user_data["state"]      = "awaiting_support_reply_to"
        ctx.user_data["reply_info"] = {"msg_id": msg_id, "uid": t_uid}
        await ctx.bot.send_message(
            uid,
            f"💬 *Replying to Support #{msg_id}*\n\nType your reply message:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))

    elif data.startswith("markread_"):
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        mid = int(data[9:])
        for m in db.data["support_messages"]:
            if m["id"] == mid:
                m["read"] = True
        db.save()
        try:
            await q.edit_message_text("✅ Marked as read.", reply_markup=kb_admin_back())
        except Exception: pass

    # ── Admin Panel Navigation ───────────────────────
    elif data == "adm_panel":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        st = db.get_stats()
        text = (
            f"🛡️ *Admin Panel*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊 *Quick Stats*\n"
            f"👥 Users: `{st['total_users']}`  ⭐ Premium: `{st['premium_users']}`\n"
            f"🆓 Free: `{st['free_users']}`  🟢 Active Today: `{st['active_today']}`\n"
            f"🎬 Videos Sent: `{st['videos_sent']}`  🎥 In Library: `{st['video_count']}`\n"
            f"💰 Revenue: `₹{st['revenue']}`\n"
            f"⏳ Pending: `{st['pending_count']}`  🆘 Unread: `{st['support_unread']}`\n\n"
            f"👇 *Select an action:*"
        )
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=kb_admin_main())
        except Exception:
            await ctx.bot.send_message(uid, text, parse_mode=ParseMode.MARKDOWN,
                                       reply_markup=kb_admin_main())

    elif data == "adm_stats":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        st = db.get_stats()
        s  = db.data["settings"]
        plans_txt = "\n".join(
            f"  • {p['name']}: ₹{p['price']} / {p['days']} days"
            for p in s["plans"].values())
        text = (
            f"📊 *Detailed Stats*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total Users: `{st['total_users']}`\n"
            f"⭐ Premium: `{st['premium_users']}`\n"
            f"🆓 Free Tier: `{st['free_users']}`\n"
            f"🟢 Active Today: `{st['active_today']}`\n"
            f"🎬 Videos Sent: `{st['videos_sent']}`\n"
            f"🎥 Library Size: `{st['video_count']}`\n"
            f"💰 Revenue: `₹{st['revenue']}`\n"
            f"⏳ Pending Payments: `{st['pending_count']}`\n"
            f"🆘 Unread Support: `{st['support_unread']}`\n\n"
            f"🆓 Free Limit: `{s['free_limit']}` videos\n"
            f"💳 UPI: `{s.get('upi_id', '—')}`\n\n"
            f"📋 *Plans*\n{plans_txt}"
        )
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=kb_admin_back())
        except Exception: pass

    elif data == "adm_videos":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        count = len(db.data["settings"]["channel_messages"])
        try:
            await q.edit_message_text(
                f"🎥 *Video Management*\n\n"
                f"📦 Library: `{count}` videos\n\n"
                f"Choose an action 👇",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_admin_videos())
        except Exception: pass

    elif data == "adm_scanvideos":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        try:
            await q.edit_message_text(
                "🔍 *Scanning for new videos…*\n\nThis will take a minute.",
                parse_mode=ParseMode.MARKDOWN)
        except Exception: pass
        added = await scan_channel_videos(ctx.bot, admin_chat_id=uid, merge=True)
        total = len(db.data["settings"]["channel_messages"])
        try:
            await q.edit_message_text(
                f"✅ *Scan Complete!*\n\n"
                f"➕ New videos found: `{added}`\n"
                f"🎥 Total in library: `{total}`\n\n"
                f"_(Existing IDs preserved)_",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_admin_videos())
        except Exception: pass

    elif data == "adm_fullscan":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        try:
            await q.edit_message_text(
                "🔄 *Full Rescan in progress…*\n\nChecking all IDs, please wait.",
                parse_mode=ParseMode.MARKDOWN)
        except Exception: pass
        added = await scan_channel_videos(ctx.bot, admin_chat_id=uid, merge=False)
        total = len(db.data["settings"]["channel_messages"])
        try:
            await q.edit_message_text(
                f"✅ *Full Scan Done!*\n\n"
                f"➕ New: `{added}`\n"
                f"🎥 Total: `{total}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_admin_videos())
        except Exception: pass

    elif data == "adm_listvideos":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        ids = db.data["settings"]["channel_messages"]
        if not ids:
            try:
                await q.edit_message_text("📭 No video IDs stored.",
                                          reply_markup=kb_admin_videos())
            except Exception: pass
            return
        # Send as chunks
        try: await q.edit_message_text(
            f"🎥 *Video IDs ({len(ids)} total)*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_admin_videos())
        except Exception: pass
        for i, chunk in enumerate([ids[j:j+50] for j in range(0, len(ids), 50)]):
            await ctx.bot.send_message(
                uid,
                f"`Part {i+1}:` " + " ".join(str(x) for x in chunk),
                parse_mode=ParseMode.MARKDOWN)

    elif data == "adm_addvideos":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        ctx.user_data["state"] = "adm_input_addvideos"
        await ctx.bot.send_message(
            uid,
            "➕ *Add Video IDs*\n\nSend the message IDs separated by spaces:\n"
            "Example: `123 456 789`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))

    elif data == "adm_removevideos":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        ctx.user_data["state"] = "adm_input_removevideos"
        await ctx.bot.send_message(
            uid,
            "➖ *Remove Video IDs*\n\nSend the IDs to remove separated by spaces:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))

    elif data == "adm_pending":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        pending = db.data["stats"]["payments_pending"]
        try: await q.edit_message_text(
            f"⏳ *Pending Payments* ({len(pending)})",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_admin_back())
        except Exception: pass
        if not pending:
            await ctx.bot.send_message(uid, "✅ No pending payments!")
            return
        for p in pending:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{p['uid']}_{p['plan']}"),
                InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{p['uid']}")]])
            await ctx.bot.send_message(
                uid,
                f"💰 *Payment Claim*\n"
                f"👤 {p['name']} (`{p['uid']}`)"
                + (f" @{p['username']}" if p.get("username") else "")
                + f"\n📋 {p['plan_name']} — ₹{p['amount']}\n🕐 {p['time'][:16]}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb)

    elif data == "adm_inbox":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        msgs = db.data["support_messages"]
        unread = [m for m in msgs if not m.get("read")]
        read   = [m for m in msgs if m.get("read")]
        try: await q.edit_message_text(
            f"🆘 *Support Inbox*\n"
            f"🔴 Unread: {len(unread)}  ✅ Read: {len(read)}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_admin_back())
        except Exception: pass
        if not msgs:
            await ctx.bot.send_message(uid, "📭 No support messages yet.")
            return
        for m in (unread + read)[:10]:
            status = "🔴 Unread" if not m.get("read") else "✅ Read"
            reply_txt = f"\n\n💬 *Admin reply:* {m['reply']}" if m.get("reply") else ""
            rows = [[InlineKeyboardButton("💬 Reply", callback_data=f"sreply_{m['id']}_{m['uid']}")]]
            if not m.get("read"):
                rows[0].append(
                    InlineKeyboardButton("✅ Mark Read", callback_data=f"markread_{m['id']}"))
            await ctx.bot.send_message(
                uid,
                f"🆘 *Support #{m['id']}* — {status}\n"
                f"👤 {m['name']} (`{m['uid']}`)"
                + (f" @{m['username']}" if m.get("username") else "")
                + f"\n🕐 {m['time'][:16]}\n\n📩 {m['text']}{reply_txt}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(rows))

    elif data == "adm_broadcast":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        ctx.user_data["state"] = "adm_input_broadcast"
        await ctx.bot.send_message(
            uid,
            "📢 *Broadcast*\n\nType the message to send to ALL users:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))

    elif data == "adm_settings":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        s = db.data["settings"]
        try:
            await q.edit_message_text(
                f"⚙️ *Settings*\n\n"
                f"🆓 Free Limit: `{s['free_limit']}`\n"
                f"💳 UPI ID: `{s.get('upi_id', '—')}`\n"
                f"🧑‍💼 Support: `@{s.get('support_username') or '—'}`\n"
                f"🖼️ QR Photo: {'Set ✅' if s.get('payment_qr') else 'Not set ❌'}\n\n"
                f"Select what to change 👇",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_admin_settings())
        except Exception: pass

    elif data == "adm_setlimit":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        ctx.user_data["state"] = "adm_input_setlimit"
        await ctx.bot.send_message(
            uid,
            f"🆓 *Set Free Video Limit*\n\nCurrent: `{db.data['settings']['free_limit']}`\n\nEnter new limit:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup([["3", "5", "10"], ["❌ Cancel"]],
                                             resize_keyboard=True))

    elif data == "adm_setupi":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        ctx.user_data["state"] = "adm_input_setupi"
        await ctx.bot.send_message(
            uid,
            "💳 *Set UPI ID*\n\nEnter your UPI ID (e.g. name@upi):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))

    elif data == "adm_setsupport":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        ctx.user_data["state"] = "adm_input_setsupport"
        await ctx.bot.send_message(
            uid,
            "🧑‍💼 *Set Support Contact*\n\nEnter the @username for support:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))

    elif data == "adm_setprice":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        ctx.user_data["state"] = "adm_input_setprice_plan"
        await ctx.bot.send_message(
            uid,
            "💵 *Set Plan Price*\n\nChoose the plan:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup(
                [["gold", "silver", "diamond"], ["❌ Cancel"]],
                resize_keyboard=True))

    elif data == "adm_setqr":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        await ctx.bot.send_message(
            uid,
            "🖼️ *Set QR Code*\n\nSend a photo of your payment QR code now:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))
        ctx.user_data["state"] = "adm_awaiting_qr"

    elif data == "adm_userlookup":
        if not ctx.user_data.get("admin_authenticated"):
            await q.answer("❌ Not authorized!", show_alert=True); return
        ctx.user_data["state"] = "adm_input_userinfo"
        await ctx.bot.send_message(
            uid,
            "👤 *User Lookup*\n\nEnter the User ID to look up:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))

    elif data == "adm_logout":
        ctx.user_data["admin_authenticated"] = False
        ctx.user_data["state"] = None
        try:
            await q.edit_message_text("👋 Logged out of Admin Panel.")
        except Exception: pass
        await ctx.bot.send_message(uid, "👋 Admin session ended.", reply_markup=kb_main())


# ── Handle QR photo upload ───────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if ctx.user_data.get("state") == "adm_awaiting_qr":
        if not ctx.user_data.get("admin_authenticated"):
            return
        file_id = update.message.photo[-1].file_id
        db.data["settings"]["payment_qr"] = file_id
        db.save()
        ctx.user_data["state"] = None
        await update.message.reply_text("✅ QR photo saved!", reply_markup=ReplyKeyboardRemove())
        await _show_panel(update.message, ctx)


# ══════════════════════════════════════════════════════
#  LEGACY COMMAND HANDLERS (still work as shortcuts)
# ══════════════════════════════════════════════════════
@_admin_only
async def cmd_give(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /give <uid> <gold|silver|diamond>"); return
    uid, plan = int(args[0]), args[1].lower()
    if db.give_premium(uid, plan):
        p = db.data["settings"]["plans"][plan]
        await update.message.reply_text(f"✅ {p['name']} → `{uid}`", parse_mode=ParseMode.MARKDOWN)
        try:
            await ctx.bot.send_message(uid,
                f"🎉 *Premium!*\n⭐ {p['name']} — {p['days']} days",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
        except Exception: pass
    else:
        await update.message.reply_text("❌ Invalid plan.")

@_admin_only
async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /revoke <uid>"); return
    uid = int(ctx.args[0])
    db.update_user(uid, premium=False, premium_plan=None, premium_expiry=None)
    await update.message.reply_text(f"✅ Revoked `{uid}`", parse_mode=ParseMode.MARKDOWN)

@_admin_only
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = " ".join(ctx.args)
    if not msg:
        await update.message.reply_text("Usage: /broadcast <message>"); return
    sent = 0
    for uid_str in db.data["users"]:
        try:
            await ctx.bot.send_message(int(uid_str), f"📢 *Announcement*\n\n{msg}",
                parse_mode=ParseMode.MARKDOWN)
            sent += 1; await asyncio.sleep(0.06)
        except Exception: pass
    await update.message.reply_text(f"✅ Sent to {sent} users.")

async def cmd_logout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["admin_authenticated"] = False
    ctx.user_data["state"] = None
    await update.message.reply_text("👋 Logged out.", reply_markup=kb_main())


# ══════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    for cmd, fn in [
        ("start",     cmd_start),
        ("admin",     cmd_admin),
        ("give",      cmd_give),
        ("revoke",    cmd_revoke),
        ("broadcast", cmd_broadcast),
        ("logout",    cmd_logout),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    # Message handlers
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Callback handler
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("🤖 Bot v3 running…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
