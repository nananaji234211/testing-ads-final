# -*- coding: utf-8 -*-
import os
import sys
import random
import asyncio
import psycopg2
import json
import pytz
from datetime import datetime, time as dtime, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl.functions.account import UpdateProfileRequest
import requests
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL     = os.getenv('DATABASE_URL')
BOT_API_ID       = int(os.getenv('API_ID'))
BOT_API_HASH     = os.getenv('API_HASH')
FREE_BOT_TOKEN   = os.getenv('FREE_BOT_TOKEN')
LOGGER_BOT_TOKEN = os.getenv('LOGGER_BOT_TOKEN')
ENV_ADMINS       = [int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip()]
OWNER_ID         = ENV_ADMINS[0] if ENV_ADMINS else None  # super-admin, can manage other admins

# These five are now editable from inside the bot (Admin Panel → Settings).
# The values below are only the first-run defaults, seeded into bot_settings
# the first time init_db() runs. After that the DB is the source of truth —
# refresh_settings() reloads these module-level names whenever they change.
CHANNEL_LINK      = "https://t.me/Uzeron_AdsBot"
COMMUNITY_LINK    = "https://t.me/UzeronCommunity"
HOW_TO_USE_LINK   = "https://t.me/Uzeron_Ads"
SUPPORT_LINK      = "https://t.me/Uzeron_Ads_support"
CONTACT_USERNAME  = "@Pandaysubscription"
PREMIUM_BOT       = "@Uzeron_AdsBot"

# Runtime admin list = ENV_ADMINS + admins added via the bot's Admin Panel.
# Populated at startup and refreshed after every add/remove in the panel.
ADMINS = list(ENV_ADMINS)

IST = pytz.timezone('Asia/Kolkata')

FREE_MAX_GROUPS          = 100
FREE_CYCLE_DELAY         = 600
FREE_MSG_DELAY           = 60
FREE_MAX_RUNTIME         = 8 * 3600
FREE_BRANDING_TAG        = "• via @Uzeron_AdsBot"
FREE_BRANDING_LASTNAME   = FREE_BRANDING_TAG
FREE_BRANDING_BIO        = "Free Automated ads • via @Uzeron_AdsBot"
FREE_WARNINGS_BEFORE_BAN = 3

# Premium defaults
DEFAULT_SCHEDULE_START = "07:00"
DEFAULT_SCHEDULE_END   = "23:50"
MAX_PROMO_MESSAGES     = 4

# ═══════════════════════════════════════════════════════════════════════════
# BOT API HELPERS
# ═══════════════════════════════════════════════════════════════════════════
def _bot(method, data=None, token=None):
    t   = token or FREE_BOT_TOKEN
    url = f"https://api.telegram.org/bot{t}/{method}"
    try:
        processed = {k: json.dumps(v) if isinstance(v, (dict, list)) else v
                     for k, v in (data or {}).items()}
        r = requests.post(url, data=processed, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Bot API [{method}]: {e}")
        return {}

def send_msg(chat_id, text, keyboard=None):
    d = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard: d["reply_markup"] = json.dumps(keyboard)
    return _bot("sendMessage", d)

def edit_msg(chat_id, msg_id, text, keyboard=None):
    d = {"chat_id": chat_id, "message_id": msg_id,
         "text": text, "parse_mode": "HTML"}
    if keyboard: d["reply_markup"] = json.dumps(keyboard)
    _bot("editMessageText", d)

def kb(buttons):
    return {"inline_keyboard": buttons}

# ── Send log to user via LOGGER bot ────────────────────────────────────────
def user_log(user_id, text):
    """Send a log message to the user via the LOGGER bot token.
    User must have started the logger bot first (/start on it).
    Errors are silently ignored — logging should never break the main flow."""
    try:
        _bot("sendMessage", {
            "chat_id": user_id,
            "text": text,
            "parse_mode": "HTML"
        }, token=LOGGER_BOT_TOKEN)
    except Exception as e:
        print(f"user_log uid={user_id}: {e}")

# ═══════════════════════════════════════════════════════════════════════════
# MEMBERSHIP CHECK
# ═══════════════════════════════════════════════════════════════════════════
def check_member(user_id, chat_username):
    r = _bot("getChatMember", {
        "chat_id": f"@{chat_username}",
        "user_id": user_id
    })
    print(f"getChatMember @{chat_username} uid={user_id}: {r}")
    if not r.get("ok"):
        err = r.get("description", "unknown error")
        return False, err
    status = r.get("result", {}).get("status", "left")
    is_member = status in ("member", "administrator", "creator", "restricted")
    return is_member, None

def refresh_settings(db):
    """Reload the editable settings/admin globals from the DB. Call this once
    at startup and again after any Admin Panel edit."""
    global CHANNEL_LINK, COMMUNITY_LINK, HOW_TO_USE_LINK, SUPPORT_LINK, CONTACT_USERNAME, PREMIUM_BOT, ADMINS
    s = db.get_all_settings()
    CHANNEL_LINK     = s.get('channel_link', CHANNEL_LINK)
    COMMUNITY_LINK   = s.get('community_link', COMMUNITY_LINK)
    HOW_TO_USE_LINK  = s.get('how_to_use_link', HOW_TO_USE_LINK)
    SUPPORT_LINK     = s.get('support_link', SUPPORT_LINK)
    CONTACT_USERNAME = s.get('contact_username', CONTACT_USERNAME)
    PREMIUM_BOT      = s.get('premium_bot', PREMIUM_BOT)
    ADMINS = list(dict.fromkeys(ENV_ADMINS + db.get_bot_admin_ids()))  # de-duped, env admins first

# ═══════════════════════════════════════════════════════════════════════════
# SMALL TIME HELPERS (for schedule windows)
# ═══════════════════════════════════════════════════════════════════════════
def _parse_hhmm(s, fallback):
    try:
        h, m = s.strip().split(':')
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return dtime(hour=h, minute=m)
    except Exception:
        pass
    fh, fm = fallback.split(':')
    return dtime(hour=int(fh), minute=int(fm))

def is_within_schedule(start_str, end_str):
    """True if current IST time falls inside [start, end]. Handles overnight
    windows (e.g. 22:00 -> 06:00) as well as same-day windows."""
    now_t   = datetime.now(IST).time()
    start_t = _parse_hhmm(start_str or DEFAULT_SCHEDULE_START, DEFAULT_SCHEDULE_START)
    end_t   = _parse_hhmm(end_str or DEFAULT_SCHEDULE_END, DEFAULT_SCHEDULE_END)
    if start_t <= end_t:
        return start_t <= now_t <= end_t
    # overnight window
    return now_t >= start_t or now_t <= end_t

def valid_hhmm(s):
    try:
        h, m = s.strip().split(':')
        h, m = int(h), int(m)
        return 0 <= h <= 23 and 0 <= m <= 59
    except Exception:
        return False

# ═══════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════════
def force_join_keyboard(channels=None):
    """channels: list of (id, username, title, invite_link, is_active) rows
    from Database.get_force_join_channels(active_only=True). Falls back to
    the two legacy hardcoded links if the DB list is empty for any reason."""
    rows = []
    if channels:
        for _id, username, title, invite_link, _active in channels:
            link = invite_link or f"https://t.me/{username}"
            rows.append([{"text": f"📢 Join {title or username}", "url": link}])
    else:
        rows.append([{"text": "📢 Join Updates Channel", "url": CHANNEL_LINK}])
        rows.append([{"text": "👥 Join Community",        "url": COMMUNITY_LINK}])
    rows.append([{"text": "✅ I've Joined — Continue", "callback_data": "check_join"}])
    return kb(rows)

def welcome_keyboard():
    return kb([
        [{"text": "🚀 Open Dashboard",        "callback_data": "dashboard"}],
        [{"text": "📢 Updates",  "url": CHANNEL_LINK},
         {"text": "🆘 Support",  "url": SUPPORT_LINK}],
        [{"text": "📖 How To Use", "url": HOW_TO_USE_LINK}],
        [{"text": "🎁 Redeem Code", "callback_data": "redeem"},
         {"text": "💎 Go Premium",  "callback_data": "upgrade"}],
    ])

def dashboard_keyboard(is_premium=False):
    rows = [
        [{"text": "👤 My Account",     "callback_data": "account"},
         {"text": "📊 Status",         "callback_data": "status"}],
        [{"text": "💬 Set Messages",   "callback_data": "setmessages"},
         {"text": "⏰ Schedule",       "callback_data": "schedule"}],
        [{"text": "🚀 Start Campaign", "callback_data": "startcampaign"},
         {"text": "🛑 Stop Campaign",  "callback_data": "stopcampaign"}],
        [{"text": "🔑 Login",          "callback_data": "login"},
         {"text": "🎁 Redeem Code",    "callback_data": "redeem"}],
    ]
    if not is_premium:
        rows.append([{"text": "💎 Upgrade", "callback_data": "upgrade"}])
    rows.append([{"text": "📢 Updates",    "url": CHANNEL_LINK},
                 {"text": "📖 How To Use", "url": HOW_TO_USE_LINK}])
    rows.append([{"text": "🚪 Logout", "callback_data": "logout"}])
    return kb(rows)

def upgrade_keyboard():
    return kb([
        [{"text": f"💎 Upgrade → {CONTACT_USERNAME}", "url": "https://t.me/Pandaysubscription"}],
        [{"text": "🎁 Have a code? Redeem", "callback_data": "redeem"}],
        [{"text": "📢 Updates", "url": CHANNEL_LINK},
         {"text": "🆘 Support", "url": SUPPORT_LINK}],
        [{"text": "🔙 Back", "callback_data": "dashboard"}],
    ])

def back_keyboard():
    return kb([[{"text": "🏠 Dashboard", "callback_data": "dashboard"}]])

def numpad_keyboard(prefix, entered=""):
    display = entered or "—"
    rows = [
        [{"text": f"📟  {display}  ", "callback_data": f"{prefix}_display"}],
        [{"text":"1","callback_data":f"{prefix}_1"},
         {"text":"2","callback_data":f"{prefix}_2"},
         {"text":"3","callback_data":f"{prefix}_3"}],
        [{"text":"4","callback_data":f"{prefix}_4"},
         {"text":"5","callback_data":f"{prefix}_5"},
         {"text":"6","callback_data":f"{prefix}_6"}],
        [{"text":"7","callback_data":f"{prefix}_7"},
         {"text":"8","callback_data":f"{prefix}_8"},
         {"text":"9","callback_data":f"{prefix}_9"}],
        [{"text":"⌫ Del",     "callback_data":f"{prefix}_del"},
         {"text":"0",         "callback_data":f"{prefix}_0"},
         {"text":"✅ Submit", "callback_data":f"{prefix}_submit"}],
        [{"text":"❌ Cancel Login","callback_data":"cancel_login"}],
    ]
    return kb(rows)

def messages_keyboard(messages, is_premium):
    rows = []
    for i, m in enumerate(messages):
        preview = (m[:28] + '…') if len(m) > 28 else m
        row = [{"text": f"📝 {i+1}. {preview}", "callback_data": f"msgview_{i}"}]
        row.append({"text": "🗑️", "callback_data": f"msgdel_{i}"})
        rows.append(row)
    limit = MAX_PROMO_MESSAGES if is_premium else 1
    if len(messages) < limit:
        rows.append([{"text": "➕ Add Message", "callback_data": "msgadd"}])
    elif not is_premium and len(messages) >= 1:
        rows.append([{"text": "💎 Upgrade for 4 messages", "callback_data": "upgrade"}])
    rows.append([{"text": "🏠 Dashboard", "callback_data": "dashboard"}])
    return kb(rows)

def admin_panel_keyboard():
    return kb([
        [{"text": "👑 Admins",    "callback_data": "adm_panel"},
         {"text": "📢 Channels",  "callback_data": "ch_panel"}],
        [{"text": "📣 Broadcast", "callback_data": "adm_broadcast_hint"},
         {"text": "⚙️ Settings",  "callback_data": "setk_panel"}],
    ])

def admin_panel_text(owner_id, bot_admin_ids):
    return (
        "💎 <b>Welcome, Admin</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👤 <b>Bot Status:</b> 🟢 On\n"
        f"🔥 <b>Owner ID:</b> <code>{owner_id}</code>\n"
        f"👑 <b>Extra Admins:</b> {len(bot_admin_ids)}\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

def admins_keyboard(owner_id, bot_admin_ids, is_owner):
    rows = []
    rows.append([{"text": f"🔥 Owner: {owner_id}", "callback_data": "adm_noop"}])
    for aid in bot_admin_ids:
        row = [{"text": str(aid), "callback_data": "adm_noop"}]
        if is_owner:
            row.append({"text": "❌ Remove", "callback_data": f"adm_rm_{aid}"})
        rows.append(row)
    if is_owner:
        rows.append([{"text": "➕ Add Admin", "callback_data": "adm_add"}])
    rows.append([{"text": "🔙 Back to Admin", "callback_data": "adm_home"}])
    return kb(rows)

def channels_keyboard(channels):
    rows = []
    for _id, username, title, invite_link, is_active in channels:
        status = "✅" if is_active else "⚪"
        rows.append([
            {"text": f"{status} {title or username}", "callback_data": f"ch_toggle_{_id}"},
            {"text": "👀", "callback_data": f"ch_view_{_id}"},
            {"text": "▲", "callback_data": f"ch_up_{_id}"},
            {"text": "▼", "callback_data": f"ch_down_{_id}"},
            {"text": "❌", "callback_data": f"ch_del_{_id}"},
        ])
    rows.append([{"text": "➕ Add Channel", "callback_data": "ch_add"}])
    rows.append([{"text": "🔙 Back to Admin", "callback_data": "adm_home"}])
    return kb(rows)

def channels_panel_text():
    return (
        "📋 <b>Manage Force-Join Channels</b>\n\n"
        "✅ = active (required to join)   ⚪ = inactive\n\n"
        "Tap the name to toggle active/inactive.\n"
        "👀 view info   ▲▼ reorder   ❌ delete\n\n"
        "Users must join every <b>active</b> channel before using the bot."
    )

def settings_keyboard():
    return kb([
        [{"text": "📢 Channel Link",   "callback_data": "setk_edit_channel_link"}],
        [{"text": "👥 Community Link", "callback_data": "setk_edit_community_link"}],
        [{"text": "📖 How-To-Use Link","callback_data": "setk_edit_how_to_use_link"}],
        [{"text": "🆘 Support Link",   "callback_data": "setk_edit_support_link"}],
        [{"text": "👤 Contact Username","callback_data": "setk_edit_contact_username"}],
        [{"text": "🤖 Premium Bot",    "callback_data": "setk_edit_premium_bot"}],
        [{"text": "🔙 Back to Admin",  "callback_data": "adm_home"}],
    ])

def settings_panel_text(s):
    return (
        "⚙️ <b>Bot Settings</b>\n\n"
        f"📢 Channel Link: <code>{s.get('channel_link','—')}</code>\n"
        f"👥 Community Link: <code>{s.get('community_link','—')}</code>\n"
        f"📖 How-To-Use Link: <code>{s.get('how_to_use_link','—')}</code>\n"
        f"🆘 Support Link: <code>{s.get('support_link','—')}</code>\n"
        f"👤 Contact Username: <code>{s.get('contact_username','—')}</code>\n"
        f"🤖 Premium Bot: <code>{s.get('premium_bot','—')}</code>\n\n"
        "Tap a field below to change it."
    )

def schedule_keyboard(is_premium):
    if not is_premium:
        return kb([
            [{"text": "💎 Upgrade to unlock Schedule", "callback_data": "upgrade"}],
            [{"text": "🏠 Dashboard", "callback_data": "dashboard"}],
        ])
    return kb([
        [{"text": "✏️ Change Start Time", "callback_data": "sched_start"}],
        [{"text": "✏️ Change End Time",   "callback_data": "sched_end"}],
        [{"text": "↩️ Reset to Default (7:00 AM - 11:50 PM)", "callback_data": "sched_reset"}],
        [{"text": "🏠 Dashboard", "callback_data": "dashboard"}],
    ])

# ═══════════════════════════════════════════════════════════════════════════
# TEXTS
# ═══════════════════════════════════════════════════════════════════════════
def force_join_text(missing=None):
    base = (
        "👋 <b>Welcome to Uzeron AdsBot!</b>\n\n"
        "To unlock the bot, please join all of the channels/groups below.\n\n"
        "<i>After joining, tap the button below.</i>"
    )
    if missing:
        base += "\n\n❌ <b>Still not joined:</b>\n" + "\n".join(f"• {m}" for m in missing)
    return base

def welcome_text():
    return (
        "⚡ <b>Welcome to Uzeron AdsBot — Free Plan</b>\n\n"
        "╔══════════════════════╗\n"
        "║  📢 100 Groups Max\n"
        "║  ⏱️ 60s Message Delay\n"
        "║  🔄 10min Cycle Delay\n"
        "║  ⏳ 8 Hours Daily Runtime\n"
        "║  🏷️ Account Branding Required\n"
        "╚══════════════════════╝\n\n"
        "💎 <b>Upgrade to Premium for:</b>\n"
        "• Unlimited groups & runtime\n"
        "• Custom delays • No branding\n"
        "• Message rotation & auto schedule\n\n"
        f"👥 Community: {COMMUNITY_LINK}\n"
        f"🆘 Support: {SUPPORT_LINK}"
    )

def dashboard_text(user, runtime, is_premium=False, expiry=None, msg_count=0):
    phone   = user[1] if user and user[1] else "Not connected"
    msg_st  = f"✅ {msg_count} set" if msg_count else "❌ Not set"
    camp_st = "🟢 Live"  if user and user[6] else "🔴 Stopped"

    if is_premium:
        plan_line   = f"👑 <b>PREMIUM</b> — expires {expiry}"
        groups_line = "📢 <b>Groups:</b> Unlimited"
        delay_line  = "⏱️ <b>Delay:</b> 60s  |  <b>Cycle:</b> 10m"
        runtime_line = "⏳ <b>Runtime:</b> Unlimited"
    else:
        plan_line   = "🆓 <b>FREE PLAN</b>"
        groups_line = f"📢 <b>Groups:</b> up to {FREE_MAX_GROUPS}"
        delay_line  = "⏱️ <b>Delay:</b> 60s  |  <b>Cycle:</b> 10m"
        hu = runtime / 3600
        hl = max(0, 8 - hu)
        runtime_line = f"⏳ <b>Runtime Today:</b> {hu:.1f}h / 8h  ({hl:.1f}h left)"

    return (
        "⚡ <b>UZERON ADSBOT — Dashboard</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{plan_line}\n"
        f"📱 <b>Account:</b> <code>{phone}</code>\n"
        f"💬 <b>Ad Messages:</b> {msg_st}\n"
        f"{groups_line}\n"
        f"{delay_line}\n"
        f"{runtime_line}\n"
        f"📡 <b>Campaign:</b> {camp_st}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        + ("👑 <i>Premium: no group cap, no runtime cap</i>"
           if is_premium else
           "⚠️ <i>Free plan: 100 groups, 8hr/day limit</i>")
    )

def upgrade_text():
    return (
        "💎 <b>UPGRADE TO PREMIUM</b>\n\n"
        "╔══════════════════════╗\n"
        "║  🚀 Unlimited Groups\n"
        "║  ⏳ Unlimited Runtime\n"
        "║  🔀 Up to 4 Rotating Messages\n"
        "║  ⏰ Auto Schedule (custom hours)\n"
        "║  🏷️ No Branding\n"
        "║  24/7 Priority Support\n"
        "╚══════════════════════╝\n\n"
        f"👤 Contact: <b>{CONTACT_USERNAME}</b>\n"
        f"🤖 Premium Bot: <b>{PREMIUM_BOT}</b>\n\n"
        "🎁 Already have a redeem code? Tap below."
    )

# ═══════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════
class Database:
    def get_conn(self):
        return psycopg2.connect(DATABASE_URL, sslmode='require')

    def init_db(self):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS free_users (
            user_id        BIGINT PRIMARY KEY,
            username       TEXT,
            phone          TEXT,
            api_id         INTEGER,
            api_hash       TEXT,
            session_string TEXT,
            promo_message  TEXT,
            is_active      INTEGER DEFAULT 0,
            runtime_today  INTEGER DEFAULT 0,
            last_reset     TEXT,
            warning_count  INTEGER DEFAULT 0,
            is_banned      INTEGER DEFAULT 0,
            branding_set   INTEGER DEFAULT 0,
            created_at     TEXT
        )''')

        # ── Premium / schedule columns (safe to run repeatedly) ──────────
        c.execute("ALTER TABLE free_users ADD COLUMN IF NOT EXISTS is_premium INTEGER DEFAULT 0")
        c.execute("ALTER TABLE free_users ADD COLUMN IF NOT EXISTS premium_expiry TEXT")
        c.execute(f"ALTER TABLE free_users ADD COLUMN IF NOT EXISTS schedule_start TEXT DEFAULT '{DEFAULT_SCHEDULE_START}'")
        c.execute(f"ALTER TABLE free_users ADD COLUMN IF NOT EXISTS schedule_end TEXT DEFAULT '{DEFAULT_SCHEDULE_END}'")

        # ── Redeem codes ───────────────────────────────────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS redeem_codes (
            code         TEXT PRIMARY KEY,
            duration_days INTEGER NOT NULL,
            created_by   BIGINT,
            created_at   TEXT,
            is_used      INTEGER DEFAULT 0,
            used_by      BIGINT,
            used_at      TEXT
        )''')

        # ── Multiple promo messages ─────────────────────────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS promo_messages (
            id           SERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL,
            message_text TEXT NOT NULL,
            created_at   TEXT
        )''')

        # ── Bot-managed admins (in addition to ADMIN_IDS env var) ───────────
        c.execute('''CREATE TABLE IF NOT EXISTS bot_admins (
            user_id    BIGINT PRIMARY KEY,
            added_by   BIGINT,
            added_at   TEXT
        )''')

        # ── Editable settings (links, contact handles, etc.) ────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS bot_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )''')

        # ── Force-join channels (replaces hardcoded 2-channel gate) ─────────
        c.execute('''CREATE TABLE IF NOT EXISTS force_join_channels (
            id          SERIAL PRIMARY KEY,
            username    TEXT NOT NULL,
            title       TEXT,
            invite_link TEXT,
            is_active   INTEGER DEFAULT 1,
            sort_order  INTEGER DEFAULT 0,
            added_at    TEXT
        )''')

        conn.commit()

        # ── Seed defaults on first run only (won't overwrite later edits) ───
        c.execute('SELECT COUNT(*) FROM bot_settings')
        if c.fetchone()[0] == 0:
            defaults = {
                'channel_link':     "https://t.me/Uzeron_AdsBot",
                'community_link':   "https://t.me/UzeronCommunity",
                'how_to_use_link':  "https://t.me/Uzeron_Ads",
                'support_link':     "https://t.me/Uzeron_Ads_support",
                'contact_username': "@Pandaysubscription",
                'premium_bot':      "@Uzeron_AdsBot",
            }
            for k, v in defaults.items():
                c.execute('INSERT INTO bot_settings(key,value) VALUES(%s,%s) ON CONFLICT (key) DO NOTHING', (k, v))
            conn.commit()

        c.execute('SELECT COUNT(*) FROM force_join_channels')
        if c.fetchone()[0] == 0:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute('''INSERT INTO force_join_channels(username,title,invite_link,is_active,sort_order,added_at)
                         VALUES(%s,%s,%s,1,0,%s)''',
                      ("Uzeron_AdsBot", "Updates Channel", "https://t.me/Uzeron_AdsBot", now))
            c.execute('''INSERT INTO force_join_channels(username,title,invite_link,is_active,sort_order,added_at)
                         VALUES(%s,%s,%s,1,1,%s)''',
                      ("UzeronCommunity", "Community Group", "https://t.me/UzeronCommunity", now))
            conn.commit()

        # ── One-time migration: move existing single promo_message into
        #    promo_messages table if user has none there yet ────────────────
        c.execute('''SELECT user_id, promo_message FROM free_users
                     WHERE promo_message IS NOT NULL AND promo_message != ''
                     AND user_id NOT IN (SELECT DISTINCT user_id FROM promo_messages)''')
        rows = c.fetchall()
        for uid, msg in rows:
            c.execute('INSERT INTO promo_messages(user_id, message_text, created_at) VALUES(%s,%s,%s)',
                      (uid, msg, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()

    def get_user(self, user_id):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('''SELECT user_id,phone,api_id,api_hash,session_string,
                     promo_message,is_active,runtime_today,last_reset,
                     warning_count,is_banned,branding_set,is_premium,
                     premium_expiry,schedule_start,schedule_end
                     FROM free_users WHERE user_id=%s''', (user_id,))
        r = c.fetchone(); conn.close(); return r

    def register_user(self, user_id, username):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT user_id FROM free_users WHERE user_id=%s',(user_id,))
        if not c.fetchone():
            c.execute('''INSERT INTO free_users(user_id,username,created_at,schedule_start,schedule_end)
                         VALUES(%s,%s,%s,%s,%s)''',
                      (user_id, username, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                       DEFAULT_SCHEDULE_START, DEFAULT_SCHEDULE_END))
            conn.commit()
        conn.close()

    def is_banned(self, user_id):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT is_banned FROM free_users WHERE user_id=%s',(user_id,))
        r = c.fetchone(); conn.close()
        return r and r[0] == 1

    def save_session(self, user_id, phone, api_id, api_hash, session_string):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('UPDATE free_users SET phone=%s,api_id=%s,api_hash=%s,session_string=%s WHERE user_id=%s',
                  (phone, api_id, api_hash, session_string, user_id))
        conn.commit(); conn.close()

    def set_promo_message(self, user_id, msg):
        """Legacy single-message setter — kept for compatibility, but the bot
        now stores messages in promo_messages. Still mirrors to the column."""
        conn = self.get_conn(); c = conn.cursor()
        c.execute('UPDATE free_users SET promo_message=%s WHERE user_id=%s',(msg,user_id))
        conn.commit(); conn.close()

    def set_campaign_status(self, user_id, status):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('UPDATE free_users SET is_active=%s WHERE user_id=%s',(status,user_id))
        conn.commit(); conn.close()

    def get_runtime_today(self, user_id):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT runtime_today,last_reset FROM free_users WHERE user_id=%s',(user_id,))
        r = c.fetchone(); conn.close()
        if not r: return 0
        runtime, last_reset = r
        today = datetime.now(IST).strftime('%Y-%m-%d')
        if last_reset != today:
            self._reset_runtime(user_id, today); return 0
        return runtime or 0

    def _reset_runtime(self, user_id, today):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('UPDATE free_users SET runtime_today=0,last_reset=%s WHERE user_id=%s',(today,user_id))
        conn.commit(); conn.close()

    def add_runtime(self, user_id, seconds):
        conn = self.get_conn(); c = conn.cursor()
        today = datetime.now(IST).strftime('%Y-%m-%d')
        c.execute('UPDATE free_users SET runtime_today=COALESCE(runtime_today,0)+%s,last_reset=%s WHERE user_id=%s',
                  (seconds,today,user_id))
        conn.commit(); conn.close()

    def set_branding(self, user_id, status):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('UPDATE free_users SET branding_set=%s WHERE user_id=%s',(status,user_id))
        conn.commit(); conn.close()

    def add_warning(self, user_id):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('UPDATE free_users SET warning_count=COALESCE(warning_count,0)+1 WHERE user_id=%s',(user_id,))
        c.execute('SELECT warning_count FROM free_users WHERE user_id=%s',(user_id,))
        count = c.fetchone()[0]
        if count >= FREE_WARNINGS_BEFORE_BAN:
            c.execute('UPDATE free_users SET is_banned=1 WHERE user_id=%s',(user_id,))
        conn.commit(); conn.close(); return count

    def ban_user(self, user_id):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('UPDATE free_users SET is_banned=1 WHERE user_id=%s',(user_id,))
        conn.commit(); conn.close()

    def logout_user(self, user_id):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('''UPDATE free_users SET phone=NULL,api_id=NULL,api_hash=NULL,
                     session_string=NULL,is_active=0,branding_set=0 WHERE user_id=%s''',(user_id,))
        conn.commit(); conn.close()

    def get_all_active_with_branding(self):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT user_id,username FROM free_users WHERE is_banned=0 AND branding_set=1')
        r = c.fetchall(); conn.close(); return r

    def get_all_users(self):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT user_id,username FROM free_users WHERE is_banned=0')
        r = c.fetchall(); conn.close(); return r

    # ── PREMIUM ──────────────────────────────────────────────────────────
    def is_premium_active(self, user_id):
        """Returns True/False and auto-expires premium if the date has passed."""
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT is_premium, premium_expiry FROM free_users WHERE user_id=%s', (user_id,))
        r = c.fetchone()
        if not r or not r[0]:
            conn.close(); return False
        is_prem, expiry = r
        if not expiry:
            conn.close(); return bool(is_prem)
        try:
            exp_date = datetime.strptime(expiry, '%Y-%m-%d %H:%M:%S')
        except Exception:
            conn.close(); return bool(is_prem)
        if datetime.now() > exp_date:
            c.execute('UPDATE free_users SET is_premium=0 WHERE user_id=%s', (user_id,))
            conn.commit(); conn.close(); return False
        conn.close(); return True

    def get_premium_expiry(self, user_id):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT premium_expiry FROM free_users WHERE user_id=%s', (user_id,))
        r = c.fetchone(); conn.close()
        return r[0] if r else None

    def grant_premium(self, user_id, duration_days):
        """Extends from current expiry if still active, else from now."""
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT is_premium, premium_expiry FROM free_users WHERE user_id=%s', (user_id,))
        r = c.fetchone()
        base = datetime.now()
        if r and r[0] and r[1]:
            try:
                cur_expiry = datetime.strptime(r[1], '%Y-%m-%d %H:%M:%S')
                if cur_expiry > base:
                    base = cur_expiry
            except Exception:
                pass
        new_expiry = base + timedelta(days=duration_days)
        new_expiry_str = new_expiry.strftime('%Y-%m-%d %H:%M:%S')
        c.execute('UPDATE free_users SET is_premium=1, premium_expiry=%s WHERE user_id=%s',
                  (new_expiry_str, user_id))
        conn.commit(); conn.close()
        return new_expiry_str

    # ── REDEEM CODES ─────────────────────────────────────────────────────
    def create_code(self, code, duration_days, admin_id):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT code FROM redeem_codes WHERE code=%s', (code,))
        if c.fetchone():
            conn.close(); return False
        c.execute('''INSERT INTO redeem_codes(code,duration_days,created_by,created_at,is_used)
                     VALUES(%s,%s,%s,%s,0)''',
                  (code, duration_days, admin_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit(); conn.close(); return True

    def redeem_code(self, code, user_id):
        """Returns (success: bool, message: str, duration_days: int|None)."""
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT code,duration_days,is_used FROM redeem_codes WHERE code=%s', (code,))
        r = c.fetchone()
        if not r:
            conn.close(); return False, "❌ Invalid code.", None
        _, duration_days, is_used = r
        if is_used:
            conn.close(); return False, "❌ This code has already been used.", None
        c.execute('UPDATE redeem_codes SET is_used=1, used_by=%s, used_at=%s WHERE code=%s',
                  (user_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), code))
        conn.commit(); conn.close()
        return True, "✅ Code redeemed!", duration_days

    # ── SCHEDULE ─────────────────────────────────────────────────────────
    def set_schedule(self, user_id, start=None, end=None):
        conn = self.get_conn(); c = conn.cursor()
        if start is not None:
            c.execute('UPDATE free_users SET schedule_start=%s WHERE user_id=%s', (start, user_id))
        if end is not None:
            c.execute('UPDATE free_users SET schedule_end=%s WHERE user_id=%s', (end, user_id))
        conn.commit(); conn.close()

    def reset_schedule(self, user_id):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('UPDATE free_users SET schedule_start=%s, schedule_end=%s WHERE user_id=%s',
                  (DEFAULT_SCHEDULE_START, DEFAULT_SCHEDULE_END, user_id))
        conn.commit(); conn.close()

    def get_schedule(self, user_id):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT schedule_start, schedule_end FROM free_users WHERE user_id=%s', (user_id,))
        r = c.fetchone(); conn.close()
        if not r:
            return DEFAULT_SCHEDULE_START, DEFAULT_SCHEDULE_END
        return r[0] or DEFAULT_SCHEDULE_START, r[1] or DEFAULT_SCHEDULE_END

    # ── MULTIPLE PROMO MESSAGES ──────────────────────────────────────────
    def get_promo_messages(self, user_id):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT id, message_text FROM promo_messages WHERE user_id=%s ORDER BY id', (user_id,))
        r = c.fetchall(); conn.close(); return r

    def add_promo_message(self, user_id, text):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('INSERT INTO promo_messages(user_id,message_text,created_at) VALUES(%s,%s,%s)',
                  (user_id, text, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        # keep legacy column in sync with the most recent message for
        # any code path that still reads the single column
        c.execute('UPDATE free_users SET promo_message=%s WHERE user_id=%s', (text, user_id))
        conn.commit(); conn.close()

    def delete_promo_message_by_index(self, user_id, index):
        msgs = self.get_promo_messages(user_id)
        if index < 0 or index >= len(msgs):
            return False
        msg_id = msgs[index][0]
        conn = self.get_conn(); c = conn.cursor()
        c.execute('DELETE FROM promo_messages WHERE id=%s AND user_id=%s', (msg_id, user_id))
        conn.commit(); conn.close()
        return True

    def trim_promo_messages(self, user_id, keep_max):
        """Used when premium expires — trims stored messages down to `keep_max`."""
        msgs = self.get_promo_messages(user_id)
        if len(msgs) <= keep_max:
            return
        conn = self.get_conn(); c = conn.cursor()
        for msg_id, _ in msgs[keep_max:]:
            c.execute('DELETE FROM promo_messages WHERE id=%s', (msg_id,))
        conn.commit(); conn.close()

    # ── BOT-MANAGED ADMINS ───────────────────────────────────────────────
    def get_bot_admin_ids(self):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT user_id FROM bot_admins')
        r = [row[0] for row in c.fetchall()]; conn.close(); return r

    def add_bot_admin(self, user_id, added_by):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT user_id FROM bot_admins WHERE user_id=%s', (user_id,))
        if c.fetchone():
            conn.close(); return False
        c.execute('INSERT INTO bot_admins(user_id,added_by,added_at) VALUES(%s,%s,%s)',
                  (user_id, added_by, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit(); conn.close(); return True

    def remove_bot_admin(self, user_id):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('DELETE FROM bot_admins WHERE user_id=%s', (user_id,))
        deleted = c.rowcount > 0
        conn.commit(); conn.close(); return deleted

    # ── SETTINGS (links / handles) ───────────────────────────────────────
    def get_all_settings(self):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT key, value FROM bot_settings')
        r = dict(c.fetchall()); conn.close(); return r

    def set_setting(self, key, value):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('''INSERT INTO bot_settings(key,value) VALUES(%s,%s)
                     ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value''', (key, value))
        conn.commit(); conn.close()

    # ── FORCE-JOIN CHANNELS ──────────────────────────────────────────────
    def get_force_join_channels(self, active_only=False):
        conn = self.get_conn(); c = conn.cursor()
        q = 'SELECT id, username, title, invite_link, is_active FROM force_join_channels'
        if active_only:
            q += ' WHERE is_active=1'
        q += ' ORDER BY sort_order, id'
        c.execute(q)
        r = c.fetchall(); conn.close(); return r

    def add_force_join_channel(self, username, title, invite_link):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT COALESCE(MAX(sort_order),-1)+1 FROM force_join_channels')
        next_order = c.fetchone()[0]
        c.execute('''INSERT INTO force_join_channels(username,title,invite_link,is_active,sort_order,added_at)
                     VALUES(%s,%s,%s,1,%s,%s)''',
                  (username.lstrip('@'), title, invite_link, next_order,
                   datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit(); conn.close()

    def delete_force_join_channel(self, channel_id):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('DELETE FROM force_join_channels WHERE id=%s', (channel_id,))
        conn.commit(); conn.close()

    def toggle_force_join_channel(self, channel_id):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT is_active FROM force_join_channels WHERE id=%s', (channel_id,))
        r = c.fetchone()
        if not r:
            conn.close(); return
        new_val = 0 if r[0] else 1
        c.execute('UPDATE force_join_channels SET is_active=%s WHERE id=%s', (new_val, channel_id))
        conn.commit(); conn.close()

    def reorder_force_join_channel(self, channel_id, direction):
        """direction: -1 to move up, +1 to move down."""
        chans = self.get_force_join_channels()
        ids = [row[0] for row in chans]
        if channel_id not in ids:
            return
        idx = ids.index(channel_id)
        swap_idx = idx + direction
        if swap_idx < 0 or swap_idx >= len(ids):
            return
        conn = self.get_conn(); c = conn.cursor()
        c.execute('SELECT sort_order FROM force_join_channels WHERE id=%s', (ids[idx],))
        order_a = c.fetchone()[0]
        c.execute('SELECT sort_order FROM force_join_channels WHERE id=%s', (ids[swap_idx],))
        order_b = c.fetchone()[0]
        c.execute('UPDATE force_join_channels SET sort_order=%s WHERE id=%s', (order_b, ids[idx]))
        c.execute('UPDATE force_join_channels SET sort_order=%s WHERE id=%s', (order_a, ids[swap_idx]))
        conn.commit(); conn.close()

    def set_channel_invite_link(self, channel_id, invite_link):
        conn = self.get_conn(); c = conn.cursor()
        c.execute('UPDATE force_join_channels SET invite_link=%s WHERE id=%s', (invite_link, channel_id))
        conn.commit(); conn.close()

# ═══════════════════════════════════════════════════════════════════════════
# LOGGER
# ═══════════════════════════════════════════════════════════════════════════
class Logger:
    def __init__(self, token): self.token = token
    def log(self, chat_id, text):
        try:
            _bot("sendMessage",{"chat_id":chat_id,"text":text,"parse_mode":"HTML"},token=self.token)
        except: pass

# ═══════════════════════════════════════════════════════════════════════════
# MAIN BOT CLASS
# ═══════════════════════════════════════════════════════════════════════════
class UzeronFreeBot:
    def __init__(self):
        bot_session           = os.getenv('BOT_SESSION_STRING', '')
        self.bot              = TelegramClient(StringSession(bot_session), BOT_API_ID, BOT_API_HASH)
        self.db               = Database()
        self.db.init_db()
        refresh_settings(self.db)
        self.logger           = Logger(LOGGER_BOT_TOKEN)
        self.tasks            = {}
        self.campaign_start_times = {}
        self.pending_message      = {}   # legacy single-message flag (unused by new UI, kept safe)
        self.pending_new_message  = {}   # uid -> True while waiting for a new message slot's text
        self.pending_redeem       = {}   # uid -> True while waiting for a redeem code
        self.pending_schedule     = {}   # uid -> 'start' | 'end' while waiting for HH:MM text
        self.login_states         = {}
        self.broadcast_state      = {}   # uid -> {'step': ...} while composing an admin broadcast
        self.pending_admin_add    = {}   # uid -> True while waiting for a new admin's user ID
        self.pending_channel_add  = {}   # uid -> True while waiting for a new force-join channel
        self.pending_channel_link = {}   # uid -> channel_id while waiting for a new invite link
        self.pending_setting_edit = {}   # uid -> setting key while waiting for a new value
        self._join_cache          = {}

    async def start(self):
        await self.bot.start(bot_token=FREE_BOT_TOKEN)
        session_str = self.bot.session.save()
        if not os.getenv('BOT_SESSION_STRING'):
            print("="*60)
            print("Add to Railway env → BOT_SESSION_STRING:")
            print(session_str)
            print("="*60)
        self.register_handlers()
        asyncio.create_task(self.branding_checker())
        print("✓ Uzeron Free Bot live!")
        await self.bot.run_until_disconnected()

    # ─── FORCE-JOIN ───────────────────────────────────────────────────────
    def _check_join(self, uid):
        if uid in ADMINS:
            return True, [], False
        if self._join_cache.get(uid):
            return True, [], False

        channels = self.db.get_force_join_channels(active_only=True)
        if not channels:
            # Nothing configured to force-join — don't block anyone.
            self._join_cache[uid] = True
            return True, [], False

        missing = []
        errors  = 0
        for _id, username, title, invite_link, _active in channels:
            ok, err = check_member(uid, username)
            if err:
                errors += 1
                print(f"WARNING: Channel check error uid={uid} @{username}: {err!r} — skipping")
                continue
            if not ok:
                missing.append(f"📢 {title or username}")

        if errors == len(channels):
            # Every single check failed (e.g. bot not admin anywhere) — fail
            # open so a config problem doesn't lock out every user.
            print(f"WARNING: All membership checks failed uid={uid}. Failing open.")
            self._join_cache[uid] = True
            return True, [], True

        all_joined = len(missing) == 0
        if all_joined:
            self._join_cache[uid] = True
        return all_joined, missing, False

    # ─── BRANDING ─────────────────────────────────────────────────────────
    async def apply_branding_on_live_client(self, uid, live_client):
        try:
            me       = await live_client.get_me()
            cur_last = (me.last_name or "").strip()
            if FREE_BRANDING_TAG in cur_last:
                cur_last = cur_last.replace(FREE_BRANDING_TAG, "").strip()
            new_last = f"{cur_last} {FREE_BRANDING_TAG}".strip() if cur_last else FREE_BRANDING_TAG
            print(f"Branding uid={uid}: '{me.last_name}' → '{new_last}'")
            await live_client(UpdateProfileRequest(last_name=new_last, about=FREE_BRANDING_BIO))
            self.db.set_branding(uid, 1)
            print(f"✅ Branding OK uid={uid}")
            return True
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"apply_branding uid={uid} ERROR: {err}")
            try:
                if ADMINS: self.logger.log(ADMINS[0], f"⚠️ Branding failed uid={uid}\n<code>{err}</code>")
            except: pass
            return False

    async def set_branding(self, uid, session_str, api_id, api_hash):
        c = None
        try:
            c = TelegramClient(StringSession(session_str), api_id, api_hash)
            await c.connect()
            if not await c.is_user_authorized():
                await c.disconnect(); return False
            ok = await self.apply_branding_on_live_client(uid, c)
            await c.disconnect(); return ok
        except Exception as e:
            print(f"set_branding uid={uid}: {e}")
            try:
                if c: await c.disconnect()
            except: pass
            return False

    async def branding_checker(self):
        while True:
            await asyncio.sleep(1800)
            for uid, _ in self.db.get_all_active_with_branding():
                user = self.db.get_user(uid)
                if user and user[4] and not self.db.is_premium_active(uid):
                    # Premium users are exempt from the branding requirement.
                    asyncio.create_task(self._verify_branding(uid, user))

    async def _verify_branding(self, uid, user):
        try:
            c = TelegramClient(StringSession(user[4]), user[2], user[3])
            await c.connect()
            if not await c.is_user_authorized():
                await c.disconnect(); return
            me = await c.get_me()
            await c.disconnect()
            if FREE_BRANDING_TAG not in (me.last_name or ""):
                count = self.db.add_warning(uid)
                left  = FREE_WARNINGS_BEFORE_BAN - count
                if count >= FREE_WARNINGS_BEFORE_BAN:
                    if uid in self.tasks: self.tasks[uid].cancel(); del self.tasks[uid]
                    self.db.set_campaign_status(uid, 0)
                    send_msg(uid, "🚫 <b>Banned!</b> Branding removed 3× — upgrade to continue.", upgrade_keyboard())
                    user_log(uid, "🚫 <b>Account banned</b> — branding removed 3 times.")
                else:
                    u = self.db.get_user(uid)
                    await self.set_branding(uid, u[4], u[2], u[3])
                    send_msg(uid,
                        f"⚠️ <b>Warning {count}/3</b> — branding removed & re-applied.\n"
                        f"{left} warning(s) left before ban.", upgrade_keyboard())
                    user_log(uid, f"⚠️ Branding warning {count}/3 — re-applied automatically.")
        except Exception as e:
            print(f"verify_branding uid={uid}: {e}")

    # ─── BROADCAST ────────────────────────────────────────────────────────
    async def do_broadcast(self, admin_uid, message_text=None, from_chat_id=None, from_msg_id=None):
        """Normal style: message_text is sent fresh to every user (no origin shown).
        Forward style: from_chat_id/from_msg_id are forwarded as-is, so Telegram
        shows the 'Forwarded from' tag on the receiving end."""
        users   = self.db.get_all_users()
        total   = len(users)
        success = 0; failed = 0
        send_msg(admin_uid, f"📣 Broadcasting to <b>{total}</b> users…")
        for uid, _ in users:
            try:
                if from_chat_id is not None:
                    _bot("forwardMessage", {
                        "chat_id": uid, "from_chat_id": from_chat_id, "message_id": from_msg_id
                    })
                else:
                    send_msg(uid, message_text)
                success += 1
                await asyncio.sleep(0.05)
            except:
                failed += 1
        send_msg(admin_uid,
            f"✅ <b>Broadcast done!</b>\n\n👥 Total: {total}\n✅ Sent: {success}\n❌ Failed: {failed}")

    # ─── ADMIN PANEL (Admins / Channels / Settings) ────────────────────────
    async def _handle_admin_panel(self, event, uid, mid, data):
        """Returns True if the callback was handled here."""
        if uid not in ADMINS:
            await event.answer("🚫 Admins only.", alert=True); return True
        is_owner = (uid == OWNER_ID)

        # ── Admin home ──────────────────────────────────────────────────
        if data == 'adm_home':
            await event.answer()
            bot_admin_ids = self.db.get_bot_admin_ids()
            edit_msg(uid, mid, admin_panel_text(OWNER_ID, bot_admin_ids), admin_panel_keyboard())
            return True

        if data == 'adm_broadcast_hint':
            await event.answer("Use /broadcast to send a message to all users.", alert=True)
            return True

        if data == 'adm_noop':
            await event.answer(); return True

        # ── Admins panel ────────────────────────────────────────────────
        if data == 'adm_panel':
            await event.answer()
            bot_admin_ids = self.db.get_bot_admin_ids()
            edit_msg(uid, mid,
                "👑 <b>Here You Can Manage Your Admins</b>",
                admins_keyboard(OWNER_ID, bot_admin_ids, is_owner))
            return True

        if data == 'adm_add':
            await event.answer()
            if not is_owner:
                await event.answer("🚫 Only the owner can add admins.", alert=True); return True
            self.pending_admin_add[uid] = True
            edit_msg(uid, mid,
                "➕ <b>Add Admin</b>\n\nSend the Telegram user ID of the new admin.\n<i>/cancel to go back</i>",
                kb([[{"text": "❌ Cancel", "callback_data": "adm_panel"}]]))
            return True

        if data.startswith('adm_rm_'):
            await event.answer()
            if not is_owner:
                await event.answer("🚫 Only the owner can remove admins.", alert=True); return True
            target = int(data.split('_')[-1])
            self.db.remove_bot_admin(target)
            refresh_settings(self.db)
            bot_admin_ids = self.db.get_bot_admin_ids()
            edit_msg(uid, mid,
                f"✅ Removed admin <code>{target}</code>.",
                admins_keyboard(OWNER_ID, bot_admin_ids, is_owner))
            return True

        # ── Channels panel ──────────────────────────────────────────────
        if data == 'ch_panel':
            await event.answer()
            channels = self.db.get_force_join_channels()
            edit_msg(uid, mid, channels_panel_text(), channels_keyboard(channels))
            return True

        if data == 'ch_add':
            await event.answer()
            self.pending_channel_add[uid] = True
            edit_msg(uid, mid,
                "➕ <b>Add Force-Join Channel</b>\n\n"
                "Send in one message: <code>USERNAME | TITLE | INVITE_LINK</code>\n\n"
                "Example:\n<code>UzeronCommunity | Community Group | https://t.me/UzeronCommunity</code>\n\n"
                "<i>USERNAME must be a public channel/group username (no @). "
                "The bot must be an admin there for join-checks to work.</i>\n\n"
                "<i>/cancel to go back</i>",
                kb([[{"text": "❌ Cancel", "callback_data": "ch_panel"}]]))
            return True

        if data.startswith('ch_toggle_'):
            await event.answer()
            cid = int(data.split('_')[-1])
            self.db.toggle_force_join_channel(cid)
            channels = self.db.get_force_join_channels()
            edit_msg(uid, mid, channels_panel_text(), channels_keyboard(channels))
            return True

        if data.startswith('ch_view_'):
            await event.answer()
            cid = int(data.split('_')[-1])
            channels = self.db.get_force_join_channels()
            match = next((c for c in channels if c[0] == cid), None)
            if not match:
                await event.answer("Not found.", alert=True); return True
            _id, username, title, invite_link, is_active = match
            edit_msg(uid, mid,
                f"👀 <b>Channel Info</b>\n\n"
                f"🏷️ Title: {title or '—'}\n"
                f"🔗 Username: @{username}\n"
                f"🔗 Invite Link: {invite_link or '—'}\n"
                f"📌 Status: {'✅ Active' if is_active else '⚪ Inactive'}",
                kb([[{"text": "✏️ Set Invite Link", "callback_data": f"ch_setlink_{cid}"}],
                    [{"text": "🔙 Back", "callback_data": "ch_panel"}]]))
            return True

        if data.startswith('ch_setlink_'):
            await event.answer()
            cid = int(data.split('_')[-1])
            self.pending_channel_link[uid] = cid
            edit_msg(uid, mid,
                "🔗 <b>Set Invite Link</b>\n\nSend the new invite link (e.g. https://t.me/yourchannel).\n<i>/cancel to go back</i>",
                kb([[{"text": "❌ Cancel", "callback_data": "ch_panel"}]]))
            return True

        if data.startswith('ch_up_'):
            await event.answer()
            cid = int(data.split('_')[-1])
            self.db.reorder_force_join_channel(cid, -1)
            channels = self.db.get_force_join_channels()
            edit_msg(uid, mid, channels_panel_text(), channels_keyboard(channels))
            return True

        if data.startswith('ch_down_'):
            await event.answer()
            cid = int(data.split('_')[-1])
            self.db.reorder_force_join_channel(cid, 1)
            channels = self.db.get_force_join_channels()
            edit_msg(uid, mid, channels_panel_text(), channels_keyboard(channels))
            return True

        if data.startswith('ch_del_'):
            await event.answer()
            cid = int(data.split('_')[-1])
            self.db.delete_force_join_channel(cid)
            channels = self.db.get_force_join_channels()
            edit_msg(uid, mid, "✅ Deleted.\n\n" + channels_panel_text(), channels_keyboard(channels))
            return True

        # ── Settings panel ────────────────────────────────────────────────
        if data == 'setk_panel':
            await event.answer()
            s = self.db.get_all_settings()
            edit_msg(uid, mid, settings_panel_text(s), settings_keyboard())
            return True

        if data.startswith('setk_edit_'):
            await event.answer()
            key = data[len('setk_edit_'):]
            self.pending_setting_edit[uid] = key
            edit_msg(uid, mid,
                f"✏️ <b>Edit Setting</b>\n\nSend the new value for <code>{key}</code>.\n<i>/cancel to go back</i>",
                kb([[{"text": "❌ Cancel", "callback_data": "setk_panel"}]]))
            return True

        return False

    # ─── HANDLERS ─────────────────────────────────────────────────────────
    def register_handlers(self):

        @self.bot.on(events.NewMessage(pattern='/users'))
        async def h_users(event):
            if event.sender_id not in ADMINS: return
            ul  = self.db.get_all_users()
            msg = f"👥 <b>Free Users ({len(ul)}):</b>\n\n" + \
                  "".join(f"• {'@'+u if u else str(i)}\n" for i,u in ul)
            await event.reply(msg or "No users.", parse_mode='html')

        @self.bot.on(events.NewMessage(pattern='/ban'))
        async def h_ban(event):
            if event.sender_id not in ADMINS: return
            try:
                uid = int(event.message.text.split()[1])
                self.db.ban_user(uid)
                if uid in self.tasks: self.tasks[uid].cancel(); del self.tasks[uid]
                await event.reply(f"✅ Banned {uid}")
                send_msg(uid, f"🚫 Banned. Contact {CONTACT_USERNAME}", upgrade_keyboard())
                user_log(uid, f"🚫 Your account has been banned by admin.")
            except: await event.reply("❌ Usage: /ban USER_ID")

        @self.bot.on(events.NewMessage(pattern='/stats'))
        async def h_stats(event):
            if event.sender_id not in ADMINS: return
            await event.reply(
                f"📊 <b>Stats</b>\n\n👥 Users: {len(self.db.get_all_users())}\n🚀 Running: {len(self.tasks)}",
                parse_mode='html')

        @self.bot.on(events.NewMessage(pattern='/broadcast'))
        async def h_broadcast(event):
            if event.sender_id not in ADMINS: return
            uid = event.sender_id
            self.broadcast_state[uid] = {'step': 'waiting_msg'}
            await event.reply(
                "📣 <b>BROADCAST CONTROL CENTER</b>\n\n"
                "Send the message you want to broadcast to <b>all users</b> now.\n"
                "<i>HTML tags like &lt;b&gt; and &lt;i&gt; are supported.</i>\n\n"
                "<i>/cancel to abort</i>",
                parse_mode='html')

        @self.bot.on(events.NewMessage(pattern='/admin'))
        @self.bot.on(events.NewMessage(pattern='/adminhelp'))
        async def h_admin(event):
            if event.sender_id not in ADMINS: return
            bot_admin_ids = self.db.get_bot_admin_ids()
            send_msg(event.sender_id, admin_panel_text(OWNER_ID, bot_admin_ids), admin_panel_keyboard())

        @self.bot.on(events.NewMessage(pattern='/addcode'))
        async def h_addcode(event):
            if event.sender_id not in ADMINS: return
            parts = event.message.text.split()
            if len(parts) != 3 or not parts[2].isdigit():
                await event.reply(
                    "❌ <b>Usage:</b> <code>/addcode CODE DAYS</code>\n"
                    "Example: <code>/addcode xyz 21</code>", parse_mode='html')
                return
            code = parts[1].strip()
            days = int(parts[2])
            if days <= 0:
                await event.reply("❌ Days must be a positive number."); return
            ok = self.db.create_code(code, days, event.sender_id)
            if ok:
                await event.reply(
                    f"✅ <b>Code created!</b>\n\n"
                    f"🎟️ Code: <code>{code}</code>\n"
                    f"📅 Duration: {days} days\n"
                    f"🕐 Created: {datetime.now(IST).strftime('%d %b %Y %I:%M %p IST')}\n\n"
                    f"Share this code with the user — they can redeem it via the "
                    f"🎁 Redeem Code button in the bot.",
                    parse_mode='html')
            else:
                await event.reply(f"❌ Code <code>{code}</code> already exists. Choose a different name.", parse_mode='html')

        @self.bot.on(events.NewMessage(pattern='/checkjoin'))
        async def h_checkjoin(event):
            if event.sender_id not in ADMINS: return
            uid = event.sender_id
            channels = self.db.get_force_join_channels()
            if not channels:
                await event.reply("🔍 No force-join channels configured."); return
            lines = [f"🔍 <b>Membership Check Debug</b>\n\nYour UID: <code>{uid}</code>\n"]
            for _id, username, title, invite_link, is_active in channels:
                ok, err = check_member(uid, username)
                lines.append(
                    f"\n📢 {title or username} (@{username}) {'✅' if is_active else '⏸️ inactive'}:\n"
                    f"  Result: {'✅ Member' if ok else '❌ Not member'}\n"
                    f"  Error: <code>{err or 'None'}</code>")
            await event.reply("".join(lines), parse_mode='html')

        @self.bot.on(events.NewMessage(pattern='/start'))
        async def h_start(event):
            uid = event.sender_id
            if self.db.is_banned(uid):
                send_msg(uid, "🚫 You are banned.", upgrade_keyboard()); return
            self.db.register_user(uid, event.sender.username)
            all_joined, missing, api_error = self._check_join(uid)
            if not all_joined:
                chans = self.db.get_force_join_channels(active_only=True)
                send_msg(uid, force_join_text(missing), force_join_keyboard(chans)); return
            send_msg(uid, welcome_text(), welcome_keyboard())

        @self.bot.on(events.CallbackQuery())
        async def h_cb(event):
            uid  = event.sender_id
            data = event.data.decode()
            mid  = event.query.msg_id

            if self.db.is_banned(uid):
                await event.answer("🚫 Banned!", alert=True); return

            user       = self.db.get_user(uid)
            runtime    = self.db.get_runtime_today(uid) if user else 0
            is_premium = self.db.is_premium_active(uid)

            if data == 'check_join':
                await event.answer()
                self._join_cache.pop(uid, None)
                all_joined, missing, api_error = self._check_join(uid)
                if all_joined:
                    edit_msg(uid, mid, welcome_text(), welcome_keyboard())
                else:
                    chans = self.db.get_force_join_channels(active_only=True)
                    edit_msg(uid, mid, force_join_text(missing), force_join_keyboard(chans))
                return

            all_joined, missing, api_error = self._check_join(uid)
            if not all_joined:
                await event.answer("❌ Join all required channels first!", alert=True)
                chans = self.db.get_force_join_channels(active_only=True)
                edit_msg(uid, mid, force_join_text(missing), force_join_keyboard(chans))
                return

            if data == 'dashboard':
                await event.answer()
                msg_count = len(self.db.get_promo_messages(uid))
                expiry = self.db.get_premium_expiry(uid) if is_premium else None
                edit_msg(uid, mid,
                    dashboard_text(user, runtime, is_premium, expiry, msg_count),
                    dashboard_keyboard(is_premium)); return

            if data == 'upgrade':
                await event.answer()
                edit_msg(uid, mid, upgrade_text(), upgrade_keyboard()); return

            # ── REDEEM CODE FLOW ──────────────────────────────────────────
            if data == 'redeem':
                await event.answer()
                self.pending_redeem[uid] = True
                edit_msg(uid, mid,
                    "🎁 <b>Redeem a Code</b>\n\n"
                    "✍️ Send your redeem code as a text message now.\n"
                    "<i>/cancel to go back</i>",
                    kb([[{"text": "❌ Cancel", "callback_data": "dashboard"}]])); return

            if data == 'delay_info':
                await event.answer("60s message delay | 10min cycle.\nUpgrade for custom!", alert=True); return

            if data == 'account':
                await event.answer()
                ph  = user[1] if user and user[1] else "Not connected"
                con = "✅ Connected" if user and user[4] else "❌ Not connected"
                brd = "✅ Set" if user and user[11] else ("➖ Not required (Premium)" if is_premium else "⏳ Pending")
                plan = "👑 Premium" if is_premium else "🆓 Free"
                expiry_line = f"\n📅 Expires: {self.db.get_premium_expiry(uid)}" if is_premium else ""
                edit_msg(uid, mid,
                    f"👤 <b>My Account</b>\n\n"
                    f"📱 Phone: <code>{ph}</code>\n"
                    f"🔗 Status: {con}\n"
                    f"🏷️ Branding: {brd}\n"
                    f"💳 Plan: {plan}{expiry_line}",
                    kb([[{"text":"🔑 Login","callback_data":"login"},
                         {"text":"🚪 Logout","callback_data":"logout"}],
                        [{"text":"🏠 Dashboard","callback_data":"dashboard"}]])); return

            if data == 'status':
                await event.answer()
                s  = "🟢 Live" if user and user[6] else "🔴 Stopped"
                msgs = self.db.get_promo_messages(uid)
                mp = f"{len(msgs)} message(s) set" if msgs else "Not set"
                runtime_line = "Unlimited (Premium)" if is_premium else f"{runtime/3600:.1f}h / 8h"
                groups_line  = "Unlimited (Premium)" if is_premium else f"up to {FREE_MAX_GROUPS}"
                edit_msg(uid, mid,
                    f"📊 <b>Status</b>\n\n"
                    f"📱 <code>{user[1] if user and user[1] else 'Not set'}</code>\n"
                    f"💬 {mp}\n📡 {s}\n⏳ {runtime_line}\n📢 Groups: {groups_line}",
                    back_keyboard()); return

            # ── SET MESSAGES (multi-message manager) ──────────────────────
            if data == 'setmessages':
                await event.answer()
                msgs = self.db.get_promo_messages(uid)
                limit = MAX_PROMO_MESSAGES if is_premium else 1
                edit_msg(uid, mid,
                    f"💬 <b>Your Ad Messages</b> ({len(msgs)}/{limit})\n\n"
                    + ("The bot will randomly pick one of these each round.\n\n" if len(msgs) > 1 else "")
                    + "".join(f"<b>{i+1}.</b> {m[1][:60]}{'…' if len(m[1])>60 else ''}\n" for i,m in enumerate(msgs)),
                    messages_keyboard([m[1] for m in msgs], is_premium)); return

            if data == 'msgadd':
                await event.answer()
                msgs  = self.db.get_promo_messages(uid)
                limit = MAX_PROMO_MESSAGES if is_premium else 1
                if len(msgs) >= limit:
                    if is_premium:
                        await event.answer(f"❌ Max {limit} messages reached.", alert=True)
                    else:
                        edit_msg(uid, mid,
                            "❌ <b>Free plan allows only 1 message.</b>\n\nUpgrade to add up to 4 rotating messages.",
                            upgrade_keyboard())
                    return
                self.pending_new_message[uid] = True
                edit_msg(uid, mid,
                    "✍️ <b>Send your new ad message</b> as a text message now.\n<i>/cancel to go back</i>",
                    kb([[{"text": "❌ Cancel", "callback_data": "setmessages"}]])); return

            if data.startswith('msgview_'):
                await event.answer()
                idx = int(data.split('_',1)[1])
                msgs = self.db.get_promo_messages(uid)
                if 0 <= idx < len(msgs):
                    edit_msg(uid, mid,
                        f"📝 <b>Message {idx+1}</b>\n\n{msgs[idx][1]}",
                        kb([[{"text": "🗑️ Delete this message", "callback_data": f"msgdel_{idx}"}],
                            [{"text": "🔙 Back", "callback_data": "setmessages"}]]))
                return

            if data.startswith('msgdel_'):
                await event.answer()
                idx = int(data.split('_',1)[1])
                self.db.delete_promo_message_by_index(uid, idx)
                msgs = self.db.get_promo_messages(uid)
                limit = MAX_PROMO_MESSAGES if is_premium else 1
                edit_msg(uid, mid,
                    f"✅ Deleted.\n\n💬 <b>Your Ad Messages</b> ({len(msgs)}/{limit})",
                    messages_keyboard([m[1] for m in msgs], is_premium))
                return

            # ── SCHEDULE (premium-only) ────────────────────────────────────
            if data == 'schedule':
                await event.answer()
                if not is_premium:
                    edit_msg(uid, mid,
                        "⏰ <b>Auto Schedule</b> 👑 <i>Premium Feature</i>\n\n"
                        "By default premium campaigns auto-start at "
                        f"<b>{DEFAULT_SCHEDULE_START}</b> and auto-stop at <b>{DEFAULT_SCHEDULE_END}</b> daily.\n"
                        "You can also set your own custom hours.\n\n"
                        "Upgrade to unlock this.",
                        schedule_keyboard(False))
                    return
                start, end = self.db.get_schedule(uid)
                edit_msg(uid, mid,
                    f"⏰ <b>Auto Schedule</b>\n\n"
                    f"🟢 Start: <code>{start}</code>\n"
                    f"🔴 Stop:  <code>{end}</code>\n\n"
                    f"Campaign auto-starts and auto-stops daily within this window.\n"
                    f"(24-hour format, e.g. 07:00 / 23:50)",
                    schedule_keyboard(True))
                return

            if data in ('sched_start', 'sched_end'):
                await event.answer()
                if not is_premium:
                    await event.answer("💎 Premium feature — upgrade to customize schedule.", alert=True)
                    return
                self.pending_schedule[uid] = 'start' if data == 'sched_start' else 'end'
                which = "Start" if data == 'sched_start' else "Stop"
                edit_msg(uid, mid,
                    f"✏️ <b>Set {which} Time</b>\n\n"
                    f"Send the time in 24-hour <code>HH:MM</code> format, e.g. <code>07:00</code>.\n"
                    f"<i>/cancel to go back</i>",
                    kb([[{"text": "❌ Cancel", "callback_data": "schedule"}]]))
                return

            if data == 'sched_reset':
                await event.answer()
                if not is_premium:
                    await event.answer("💎 Premium feature.", alert=True); return
                self.db.reset_schedule(uid)
                start, end = self.db.get_schedule(uid)
                edit_msg(uid, mid,
                    f"✅ Reset to default.\n\n🟢 Start: <code>{start}</code>\n🔴 Stop: <code>{end}</code>",
                    schedule_keyboard(True))
                return

            if data == 'logout':
                await event.answer()
                if uid in self.tasks: self.tasks[uid].cancel(); del self.tasks[uid]
                phone = user[1] if user and user[1] else "unknown"
                self.db.logout_user(uid)
                new_user = self.db.get_user(uid)
                edit_msg(uid, mid,
                    dashboard_text(new_user, 0, is_premium, self.db.get_premium_expiry(uid) if is_premium else None,
                                   len(self.db.get_promo_messages(uid))),
                    dashboard_keyboard(is_premium))
                send_msg(uid, "✅ Logged out successfully!")
                user_log(uid,
                    f"🚪 <b>Account logged out</b>\n"
                    f"📱 Phone: <code>{phone}</code>\n"
                    f"🕐 Time: {datetime.now(IST).strftime('%d %b %Y %I:%M %p IST')}")
                return

            if data == 'startcampaign':
                await event.answer()
                if not user or not user[4]:
                    send_msg(uid, "❌ Please login first!"); return
                msgs = self.db.get_promo_messages(uid)
                if not msgs:
                    send_msg(uid, "❌ Set at least one ad message first!"); return
                if uid in self.tasks:
                    send_msg(uid, "⚠️ Campaign already running!"); return
                if not is_premium and runtime >= FREE_MAX_RUNTIME:
                    edit_msg(uid, mid, "⏰ <b>Daily Limit Reached!</b>\n\nCome back tomorrow or upgrade.", upgrade_keyboard()); return
                self.db.set_campaign_status(uid, 1)
                self.campaign_start_times[uid] = datetime.now()
                self.tasks[uid] = asyncio.create_task(self.run_campaign(uid))
                new_user = self.db.get_user(uid)
                edit_msg(uid, mid,
                    dashboard_text(new_user, runtime, is_premium,
                                   self.db.get_premium_expiry(uid) if is_premium else None, len(msgs)),
                    dashboard_keyboard(is_premium))
                if is_premium:
                    start, end = self.db.get_schedule(uid)
                    send_msg(uid,
                        f"🚀 <b>Campaign Started!</b> 👑\n\n"
                        f"📊 Unlimited groups  |  ⏱️ {FREE_MSG_DELAY}s  |  "
                        f"🔄 {FREE_CYCLE_DELAY//60}m cycle  |  ⏳ Unlimited runtime\n"
                        f"⏰ Schedule: {start} – {end} daily")
                else:
                    send_msg(uid,
                        f"🚀 <b>Campaign Started!</b>\n\n"
                        f"📊 {FREE_MAX_GROUPS} groups  |  ⏱️ {FREE_MSG_DELAY}s  |  "
                        f"🔄 {FREE_CYCLE_DELAY//60}m cycle  |  ⏳ 8h daily")
                user_log(uid,
                    f"🚀 <b>Campaign Started</b>\n"
                    f"📱 Account: <code>{user[1]}</code>\n"
                    f"🕐 Time: {datetime.now(IST).strftime('%d %b %Y %I:%M %p IST')}")
                return

            if data == 'stopcampaign':
                await event.answer()
                if uid not in self.tasks:
                    send_msg(uid, "⚠️ No campaign running!"); return
                self.db.set_campaign_status(uid, 0)
                self.tasks[uid].cancel(); del self.tasks[uid]
                elapsed = 0
                if uid in self.campaign_start_times:
                    elapsed = (datetime.now()-self.campaign_start_times[uid]).total_seconds()
                    self.db.add_runtime(uid, int(elapsed)); del self.campaign_start_times[uid]
                new_user = self.db.get_user(uid)
                edit_msg(uid, mid,
                    dashboard_text(new_user, self.db.get_runtime_today(uid), is_premium,
                                   self.db.get_premium_expiry(uid) if is_premium else None,
                                   len(self.db.get_promo_messages(uid))),
                    dashboard_keyboard(is_premium))
                send_msg(uid, "🛑 <b>Campaign Stopped!</b>")
                user_log(uid,
                    f"🛑 <b>Campaign Stopped</b>\n"
                    f"📱 Account: <code>{user[1] if user and user[1] else 'N/A'}</code>\n"
                    f"⏱️ Session runtime: {elapsed/3600:.1f}h\n"
                    f"🕐 Time: {datetime.now(IST).strftime('%d %b %Y %I:%M %p IST')}")
                return

            if data == 'login':
                await event.answer()
                if user and user[4]:
                    send_msg(uid, "✅ Already logged in!"); return
                self.login_states[uid] = {'step': 'api'}
                edit_msg(uid, mid,
                    "🔑 <b>Login — Step 1/3: API Credentials</b>\n\n"
                    "Send your credentials in <b>one message</b>:\n"
                    "<code>API_ID API_HASH</code>\n\n"
                    "📌 Get from: <a href='https://my.telegram.org/apps'>my.telegram.org/apps</a>\n"
                    "Example: <code>12345678 abc123def456</code>\n\n"
                    "<i>/cancel to go back</i>",
                    kb([[{"text":"❌ Cancel","callback_data":"cancel_login"}]])); return

            if data == 'cancel_login':
                await event.answer()
                await self._cleanup_login(uid)
                edit_msg(uid, mid, dashboard_text(user, runtime, is_premium), dashboard_keyboard(is_premium)); return

            if data.startswith('otp_'):
                await self._handle_numpad(event, uid, mid, data, 'otp'); return

            # ── BROADCAST style choice (admin only) ───────────────────────
            if data in ('bc_forward', 'bc_normal', 'bc_cancel') and uid in ADMINS:
                await event.answer()
                state = self.broadcast_state.pop(uid, None)
                if not state:
                    return
                if data == 'bc_cancel':
                    edit_msg(uid, mid, "❌ Broadcast cancelled.")
                    return
                if data == 'bc_forward':
                    edit_msg(uid, mid, "📡 Broadcasting (forward style)…")
                    asyncio.create_task(self.do_broadcast(
                        uid, from_chat_id=state['chat_id'], from_msg_id=state['msg_id']))
                else:
                    edit_msg(uid, mid, "📡 Broadcasting (normal style)…")
                    asyncio.create_task(self.do_broadcast(uid, message_text=state['text']))
                return

            # ── ADMIN PANEL ────────────────────────────────────────────────
            if data.startswith('adm_') or data.startswith('ch_') or data.startswith('setk_'):
                handled = await self._handle_admin_panel(event, uid, mid, data)
                if handled:
                    return

        @self.bot.on(events.NewMessage())
        async def h_text(event):
            uid  = event.sender_id
            text = (event.message.text or '').strip()
            if not text or self.db.is_banned(uid): return

            if text == '/cancel':
                await self._cleanup_login(uid)
                for d in (self.pending_message, self.pending_new_message,
                          self.pending_redeem, self.pending_schedule, self.broadcast_state,
                          self.pending_admin_add, self.pending_channel_add,
                          self.pending_channel_link, self.pending_setting_edit):
                    if uid in d: del d[uid]
                user       = self.db.get_user(uid)
                runtime    = self.db.get_runtime_today(uid) if user else 0
                is_premium = self.db.is_premium_active(uid)
                send_msg(uid, dashboard_text(user, runtime, is_premium), dashboard_keyboard(is_premium)); return

            if uid in self.broadcast_state and uid in ADMINS:
                if not text.startswith('/'):
                    self.broadcast_state[uid] = {
                        'step': 'choose_style',
                        'text': text,
                        'chat_id': event.chat_id,
                        'msg_id': event.message.id,
                    }
                    send_msg(uid,
                        "📡 <b>BROADCAST CONTROL CENTER</b>\n\n"
                        "<i>Choose how you want to send your message. "
                        "(HTML tags like &lt;b&gt; and &lt;i&gt; are natively supported!)</i>\n\n"
                        "✉️ <b>BOT TO USERS</b>\n"
                        "├ <b>Forward:</b> Original message with 'Forwarded from' tag.\n"
                        "└ <b>Normal:</b> Clean, brand new message from the bot.",
                        kb([[{"text": "➡️ Forward", "callback_data": "bc_forward"},
                             {"text": "📝 Normal",   "callback_data": "bc_normal"}],
                            [{"text": "❌ Cancel", "callback_data": "bc_cancel"}]]))
                    return

            # ── ADD ADMIN text input (owner only) ───────────────────────────
            if uid in self.pending_admin_add and uid in ADMINS:
                del self.pending_admin_add[uid]
                if uid != OWNER_ID:
                    send_msg(uid, "🚫 Only the owner can add admins."); return
                if not text.strip().isdigit() and not (text.strip().startswith('-') and text.strip()[1:].isdigit()):
                    send_msg(uid, "❌ Send a numeric Telegram user ID.")
                    return
                new_admin_id = int(text.strip())
                added = self.db.add_bot_admin(new_admin_id, uid)
                refresh_settings(self.db)
                if added:
                    send_msg(uid, f"✅ Added <code>{new_admin_id}</code> as admin.",
                              kb([[{"text": "🔙 Back to Admins", "callback_data": "adm_panel"}]]))
                else:
                    send_msg(uid, f"⚠️ <code>{new_admin_id}</code> is already an admin.",
                              kb([[{"text": "🔙 Back to Admins", "callback_data": "adm_panel"}]]))
                return

            # ── ADD CHANNEL text input ──────────────────────────────────────
            if uid in self.pending_channel_add and uid in ADMINS:
                del self.pending_channel_add[uid]
                parts = [p.strip() for p in text.split('|')]
                if len(parts) < 1 or not parts[0]:
                    send_msg(uid, "❌ Format: <code>USERNAME | TITLE | INVITE_LINK</code>")
                    return
                username    = parts[0].lstrip('@')
                title       = parts[1] if len(parts) > 1 and parts[1] else username
                invite_link = parts[2] if len(parts) > 2 and parts[2] else f"https://t.me/{username}"
                self.db.add_force_join_channel(username, title, invite_link)
                channels = self.db.get_force_join_channels()
                send_msg(uid, f"✅ Added channel <b>{title}</b> (@{username}).",
                          channels_keyboard(channels))
                return

            # ── SET CHANNEL INVITE LINK text input ─────────────────────────
            if uid in self.pending_channel_link and uid in ADMINS:
                cid = self.pending_channel_link.pop(uid)
                self.db.set_channel_invite_link(cid, text.strip())
                channels = self.db.get_force_join_channels()
                send_msg(uid, "✅ Invite link updated.", channels_keyboard(channels))
                return

            # ── EDIT SETTING text input ─────────────────────────────────────
            if uid in self.pending_setting_edit and uid in ADMINS:
                key = self.pending_setting_edit.pop(uid)
                self.db.set_setting(key, text.strip())
                refresh_settings(self.db)
                send_msg(uid, f"✅ <code>{key}</code> updated.", settings_keyboard())
                return

            # ── REDEEM CODE text input ──────────────────────────────────────
            if uid in self.pending_redeem and not text.startswith('/'):
                del self.pending_redeem[uid]
                code = text.strip()
                success, message, duration_days = self.db.redeem_code(code, uid)
                if success:
                    new_expiry = self.db.grant_premium(uid, duration_days)
                    send_msg(uid,
                        f"🎉 <b>Premium Activated!</b>\n\n"
                        f"🎟️ Code: <code>{code}</code>\n"
                        f"📅 Duration: {duration_days} days\n"
                        f"⏳ Expires: <code>{new_expiry}</code>\n\n"
                        f"👑 You now have unlimited groups, unlimited runtime, "
                        f"up to {MAX_PROMO_MESSAGES} rotating messages, and auto-schedule!",
                        kb([[{"text": "🏠 Dashboard", "callback_data": "dashboard"}]]))
                    user_log(uid, f"👑 <b>Premium activated</b> via code <code>{code}</code> — {duration_days} days.")
                else:
                    send_msg(uid, message, kb([[{"text": "🎁 Try Another Code", "callback_data": "redeem"}],
                                               [{"text": "🏠 Dashboard", "callback_data": "dashboard"}]]))
                return

            # ── SCHEDULE text input ──────────────────────────────────────────
            if uid in self.pending_schedule and not text.startswith('/'):
                which = self.pending_schedule[uid]
                if not valid_hhmm(text):
                    send_msg(uid, "❌ Invalid format. Send time as <code>HH:MM</code> in 24-hour format, e.g. <code>07:00</code>.")
                    return
                del self.pending_schedule[uid]
                if which == 'start':
                    self.db.set_schedule(uid, start=text.strip())
                else:
                    self.db.set_schedule(uid, end=text.strip())
                start, end = self.db.get_schedule(uid)
                send_msg(uid,
                    f"✅ <b>Schedule Updated!</b>\n\n🟢 Start: <code>{start}</code>\n🔴 Stop: <code>{end}</code>",
                    kb([[{"text": "🏠 Dashboard", "callback_data": "dashboard"}]]))
                return

            # ── NEW MESSAGE SLOT text input ─────────────────────────────────
            if uid in self.pending_new_message and not text.startswith('/'):
                del self.pending_new_message[uid]
                is_premium = self.db.is_premium_active(uid)
                limit = MAX_PROMO_MESSAGES if is_premium else 1
                msgs = self.db.get_promo_messages(uid)
                if len(msgs) >= limit:
                    send_msg(uid, f"❌ Max {limit} messages reached.")
                    return
                self.db.add_promo_message(uid, text)
                msgs = self.db.get_promo_messages(uid)
                send_msg(uid,
                    f"✅ <b>Message Saved!</b> ({len(msgs)}/{limit})\n\n📝 <i>{text[:100]}{'…' if len(text)>100 else ''}</i>",
                    kb([[{"text":"🚀 Start Campaign","callback_data":"startcampaign"}],
                        [{"text":"💬 Manage Messages","callback_data":"setmessages"}],
                        [{"text":"🏠 Dashboard","callback_data":"dashboard"}]]))
                return

            # ── legacy single-message flag (kept for safety, no longer wired to a button) ──
            if uid in self.pending_message and not text.startswith('/'):
                del self.pending_message[uid]
                self.db.set_promo_message(uid, text)
                send_msg(uid,
                    f"✅ <b>Ad Message Saved!</b>\n\n📝 <i>{text[:100]}{'…' if len(text)>100 else ''}</i>",
                    kb([[{"text":"🚀 Start Campaign","callback_data":"startcampaign"}],
                        [{"text":"🏠 Dashboard","callback_data":"dashboard"}]])); return

            if uid in self.login_states:
                state = self.login_states[uid]
                if not text.startswith('/'):
                    if state['step'] == 'api':
                        await self._login_got_api(uid, text)
                    elif state['step'] == 'phone':
                        await self._login_got_phone(uid, text)
                    elif state['step'] == '2fa':
                        await self._login_got_2fa(uid, text)

    # ─── LOGIN HELPERS ─────────────────────────────────────────────────────
    async def _cleanup_login(self, uid):
        if uid in self.login_states:
            try:
                c = self.login_states[uid].get('client')
                if c: await c.disconnect()
            except: pass
            del self.login_states[uid]

    async def _login_got_api(self, uid, text):
        parts = text.strip().split()
        if len(parts) != 2 or not parts[0].isdigit():
            send_msg(uid,
                "❌ Wrong format.\n\nSend: <code>API_ID API_HASH</code>\n"
                "Example: <code>12345678 abcdef1234567890</code>",
                kb([[{"text":"❌ Cancel","callback_data":"cancel_login"}]])); return
        self.login_states[uid].update({'api_id': int(parts[0]), 'api_hash': parts[1], 'step': 'phone'})
        send_msg(uid,
            "✅ <b>API credentials saved!</b>\n\n"
            "🔑 <b>Step 2/3: Phone Number</b>\n\n"
            "📱 Send your phone number:\n"
            "Example: <code>+917239879045</code>\n\n<i>/cancel to go back</i>",
            kb([[{"text":"❌ Cancel","callback_data":"cancel_login"}]]))

    async def _login_got_phone(self, uid, text):
        if not text.startswith('+'):
            send_msg(uid, "❌ Must include country code. Example: <code>+917239879045</code>"); return
        state = self.login_states[uid]
        send_msg(uid, "⏳ <b>Sending code…</b>")
        try:
            client = TelegramClient(StringSession(), state['api_id'], state['api_hash'])
            await client.connect()
            sent = await client.send_code_request(text.strip())
            state.update({
                'client': client, 'phone': text.strip(),
                'phone_code_hash': sent.phone_code_hash,
                'step': 'otp', 'otp_digits': ''
            })
            r = send_msg(uid,
                "📨 <b>Code sent to your Telegram!</b>\n\n"
                "🔢 <b>Step 3/3: Enter OTP</b>\n\nUse the buttons below:",
                numpad_keyboard('otp', ''))
            try: state['otp_msg_id'] = r['result']['message_id']
            except: state['otp_msg_id'] = None
        except Exception as e:
            send_msg(uid,
                f"❌ <b>Failed to send code:</b>\n<code>{e}</code>\n\n"
                "Check your API_ID / API_HASH and try again.",
                kb([[{"text":"🔑 Try Again","callback_data":"login"},
                     {"text":"🏠 Dashboard","callback_data":"dashboard"}]]))
            await self._cleanup_login(uid)

    async def _handle_numpad(self, event, uid, mid, data, prefix):
        if uid not in self.login_states:
            await event.answer(); return
        state  = self.login_states[uid]
        action = data[len(prefix)+1:]
        key    = 'otp_digits'
        if key not in state: state[key] = ''

        if   action == 'display': await event.answer(); return
        elif action == 'del':
            state[key] = state[key][:-1]; await event.answer("⌫")
        elif action == 'submit':
            await event.answer()
            if not state[key]:
                await event.answer("❌ Nothing entered!", alert=True); return
            await self._submit_otp(uid, mid, state[key]); return
        elif action.isdigit():
            if len(state[key]) < 10: state[key] += action
            await event.answer(state[key])
        else:
            await event.answer(); return

        digits = state[key]
        try:
            edit_msg(uid, mid,
                f"📨 <b>Enter OTP:</b>\n\nCode so far: <code>{digits or '—'}</code>",
                numpad_keyboard(prefix, digits))
        except: pass

    async def _submit_otp(self, uid, mid, code):
        state = self.login_states.get(uid)
        if not state: return
        try:
            await state['client'].sign_in(state['phone'], code,
                                           phone_code_hash=state['phone_code_hash'])
            await self._complete_login(uid, state, mid)
        except SessionPasswordNeededError:
            state['step'] = '2fa'
            edit_msg(uid, mid,
                "🔐 <b>2FA Enabled!</b>\n\n"
                "✍️ Type your 2FA password and send it as a message.\n"
                "<i>/cancel to go back</i>",
                kb([[{"text":"❌ Cancel Login","callback_data":"cancel_login"}]]))
        except Exception as e:
            edit_msg(uid, mid,
                f"❌ <b>Wrong code:</b> <code>{e}</code>\n\nTap Login again for a fresh code.",
                kb([[{"text":"🔑 Try Again","callback_data":"login"},
                     {"text":"🏠 Dashboard","callback_data":"dashboard"}]]))
            await self._cleanup_login(uid)

    async def _login_got_2fa(self, uid, text):
        state = self.login_states.get(uid)
        if not state: return
        try:
            await state['client'].sign_in(password=text)
            await self._complete_login(uid, state, mid=None)
        except Exception as e:
            send_msg(uid,
                f"❌ <b>Wrong 2FA password:</b> <code>{e}</code>\n\nType and send again:",
                kb([[{"text":"❌ Cancel Login","callback_data":"cancel_login"}]]))

    async def _complete_login(self, uid, state, mid):
        live_client = state['client']
        phone    = state['phone']
        api_id   = state['api_id']
        api_hash = state['api_hash']
        session  = live_client.session.save()

        self.db.save_session(uid, phone, api_id, api_hash, session)
        del self.login_states[uid]
        self.logger.log(uid, f"✅ Free login: {phone}")

        is_premium = self.db.is_premium_active(uid)

        notify = (
            "✅ <b>Login Successful!</b>\n\n"
            f"📱 Account: <code>{phone}</code>\n\n"
            + ("👑 Premium — branding not required." if is_premium
               else "🏷️ Setting branding on your account…")
        )
        if mid: edit_msg(uid, mid, notify)
        else:   send_msg(uid, notify)

        if is_premium:
            # Premium users skip mandatory branding enforcement.
            try: await live_client.disconnect()
            except: pass
            user_log(uid,
                f"✅ <b>Account Added Successfully</b> (Premium — no branding)\n"
                f"📱 Phone: <code>{phone}</code>\n"
                f"🕐 Time: {datetime.now(IST).strftime('%d %b %Y %I:%M %p IST')}")
            send_msg(uid,
                "✅ <b>Account Ready!</b> 👑\n\nNo branding required on Premium.",
                kb([[{"text":"💬 Set Ad Messages","callback_data":"setmessages"}],
                    [{"text":"🏠 Dashboard","callback_data":"dashboard"}]]))
            return

        ok = await self.apply_branding_on_live_client(uid, live_client)

        try: await live_client.disconnect()
        except: pass

        # Log to user via logger bot
        user_log(uid,
            f"✅ <b>Account Added Successfully</b>\n"
            f"📱 Phone: <code>{phone}</code>\n"
            f"🏷️ Branding: {'✅ Applied' if ok else '⚠️ Failed — add manually'}\n"
            f"🕐 Time: {datetime.now(IST).strftime('%d %b %Y %I:%M %p IST')}")

        if ok:
            send_msg(uid,
                "✅ <b>Account Ready!</b>\n\n"
                f"🏷️ Last name updated:\n<code>{FREE_BRANDING_LASTNAME}</code>\n\n"
                "⚠️ Do <b>NOT</b> remove it — 3 strikes = permanent ban\n"
                "💎 Upgrade to remove branding requirement!",
                kb([[{"text":"💬 Set Ad Messages","callback_data":"setmessages"}],
                    [{"text":"🏠 Dashboard","callback_data":"dashboard"}]]))
        else:
            send_msg(uid,
                "✅ <b>Login Successful!</b>\n\n"
                "⚠️ Branding could not be set automatically.\n"
                f"Please add manually to your last name:\n<code>{FREE_BRANDING_LASTNAME}</code>",
                kb([[{"text":"🏠 Dashboard","callback_data":"dashboard"}]]))

    # ─── CAMPAIGN ──────────────────────────────────────────────────────────
    async def run_campaign(self, uid):
        try:
            user   = self.db.get_user(uid)
            phone  = user[1]
            client = TelegramClient(StringSession(user[4]), user[2], user[3])
            await client.connect()
            if not await client.is_user_authorized():
                send_msg(uid, "❌ Session expired. Please logout and login again.")
                user_log(uid, f"❌ Session expired for <code>{phone}</code> — please re-login.")
                self.db.set_campaign_status(uid, 0); await client.disconnect(); return

            is_premium = self.db.is_premium_active(uid)

            dialogs = await client.get_dialogs()
            all_groups = [d for d in dialogs if d.is_group]
            groups = all_groups if is_premium else all_groups[:FREE_MAX_GROUPS]
            if not groups:
                send_msg(uid, "❌ No groups found!")
                self.db.set_campaign_status(uid, 0); await client.disconnect(); return

            send_msg(uid, f"📊 <b>Ready!</b> Found <b>{len(groups)}</b> groups — starting…")
            round_num = 0
            campaign_start = datetime.now()

            while self.db.get_user(uid)[6]:
                # Re-check premium status each loop (may have expired mid-campaign)
                is_premium = self.db.is_premium_active(uid)

                # ── Schedule gate (premium only) ─────────────────────────
                if is_premium:
                    start, end = self.db.get_schedule(uid)
                    if not is_within_schedule(start, end):
                        # Outside the allowed window — wait quietly and recheck.
                        await asyncio.sleep(60)
                        continue

                # ── Runtime cap (free only) ──────────────────────────────
                if not is_premium:
                    runtime         = self.db.get_runtime_today(uid)
                    elapsed_session = (datetime.now()-campaign_start).total_seconds()
                    total_today     = runtime + elapsed_session
                    if total_today >= FREE_MAX_RUNTIME:
                        self.db.set_campaign_status(uid, 0)
                        self.db.add_runtime(uid, int(elapsed_session))
                        if uid in self.campaign_start_times: del self.campaign_start_times[uid]
                        send_msg(uid, "⏰ <b>8h Daily Limit Reached!</b>\n\nResumes tomorrow.", upgrade_keyboard())
                        user_log(uid,
                            f"⏰ <b>Daily limit reached</b>\n"
                            f"📱 <code>{phone}</code> — 8h used today. Resumes tomorrow.")
                        break
                else:
                    runtime         = 0
                    elapsed_session = (datetime.now()-campaign_start).total_seconds()

                round_num += 1
                sent = failed = 0

                msgs = self.db.get_promo_messages(uid)
                if not msgs:
                    send_msg(uid, "❌ No ad message set — stopping campaign.")
                    self.db.set_campaign_status(uid, 0)
                    break

                for group in groups:
                    if not self.db.get_user(uid)[6]: break

                    # Re-check schedule mid-round for premium users
                    if is_premium:
                        start, end = self.db.get_schedule(uid)
                        if not is_within_schedule(start, end):
                            break

                    if not is_premium:
                        elapsed = (datetime.now()-campaign_start).total_seconds()
                        if runtime + elapsed >= FREE_MAX_RUNTIME: break

                    # Randomly pick one of the saved messages for this send
                    msg = random.choice(msgs)[1]

                    try:
                        await client.send_message(group.entity, msg)
                        sent += 1
                        user_log(uid,
                            f"[<code>{phone}</code>] "
                            f"[✓] Sent to <b>{group.name}</b> ({group.entity.id})")
                        await asyncio.sleep(FREE_MSG_DELAY)
                    except FloodWaitError as e:
                        send_msg(uid, f"⚠️ FloodWait {e.seconds}s — pausing…")
                        user_log(uid, f"[<code>{phone}</code>] ⚠️ FloodWait {e.seconds}s")
                        await asyncio.sleep(e.seconds)
                    except Exception as ex:
                        failed += 1
                        user_log(uid,
                            f"[<code>{phone}</code>] "
                            f"[✗] Failed to <b>{group.name}</b> ({group.entity.id}): {ex}")
                        await asyncio.sleep(10)

                now_str = datetime.now(IST).strftime('%d %b %Y %I:%M %p IST')
                if is_premium:
                    hl_line = "⏳ Unlimited runtime"
                else:
                    hl = max(0, 8-(runtime+elapsed_session)/3600)
                    hl_line = f"⏳ {hl:.1f}h left"
                send_msg(uid,
                    f"📊 <b>Round {round_num}</b> — ✅ {sent}  ❌ {failed}\n"
                    f"{hl_line}  |  Next in {FREE_CYCLE_DELAY//60}m",
                    kb([[{"text":"🛑 Stop","callback_data":"stopcampaign"}]]
                       + ([] if is_premium else [{"text":"💎 Upgrade","callback_data":"upgrade"}])))
                user_log(uid,
                    f"📊 <b>Round {round_num} complete</b>\n"
                    f"📱 <code>{phone}</code>\n"
                    f"✅ Sent: {sent}  ❌ Failed: {failed}\n"
                    f"{hl_line}\n"
                    f"🕐 {now_str}")
                await asyncio.sleep(FREE_CYCLE_DELAY)

            await client.disconnect()

        except asyncio.CancelledError:
            if uid in self.campaign_start_times:
                elapsed = (datetime.now()-self.campaign_start_times[uid]).total_seconds()
                self.db.add_runtime(uid, int(elapsed)); del self.campaign_start_times[uid]
            user_log(uid,
                f"🛑 <b>Campaign stopped</b>\n"
                f"📱 <code>{self.db.get_user(uid)[1] if self.db.get_user(uid) else 'N/A'}</code>\n"
                f"🕐 {datetime.now(IST).strftime('%d %b %Y %I:%M %p IST')}")
        except Exception as e:
            self.db.set_campaign_status(uid, 0)
            if uid in self.tasks: del self.tasks[uid]
            send_msg(uid, f"❌ Campaign error: <code>{e}</code>")
            user_log(uid, f"❌ <b>Campaign error</b>\n<code>{e}</code>")

# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════
async def main():
    print("="*50+"\n  🆓 UZERON ADSBOT — Free Tier\n"+"="*50)
    missing = [v for v in ['API_ID','API_HASH','FREE_BOT_TOKEN','LOGGER_BOT_TOKEN',
                            'ADMIN_IDS','DATABASE_URL']
               if not os.getenv(v)]
    if missing:
        print(f"❌ Missing env vars: {', '.join(missing)}"); sys.exit(1)
    await UzeronFreeBot().start()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🆓 Stopped")
    except Exception as e:
        print(f"Fatal: {e}"); sys.exit(1)
