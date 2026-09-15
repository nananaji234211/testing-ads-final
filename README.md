# 🆓 Uzeron AdsBot — Free Tier

A Telegram userbot-based ad broadcasting bot with force-join gate, branding enforcement, admin broadcast, and a full login flow.

---

## 📁 File Structure

```
free_bot.py       ← main bot file (everything is here)
.env              ← environment variables (never commit this)
requirements.txt  ← pip dependencies
```

---

## ⚙️ Environment Variables

Set these in Railway (or your `.env` file):

| Variable | Description |
|---|---|
| `API_ID` | Telegram app API ID from my.telegram.org/apps |
| `API_HASH` | Telegram app API Hash from my.telegram.org/apps |
| `FREE_BOT_TOKEN` | Bot token from @BotFather for the free bot |
| `LOGGER_BOT_TOKEN` | Bot token for logging errors/events to admin |
| `ADMIN_IDS` | Comma-separated Telegram user IDs of admins e.g. `123456,789012` |
| `DATABASE_URL` | PostgreSQL connection string (Railway provides this) |
| `BOT_SESSION_STRING` | *(auto-generated on first run — see below)* |

### BOT_SESSION_STRING

On first run the bot prints a session string in logs like:
```
Add to Railway env → BOT_SESSION_STRING:
1BVtsOJ8Buxxxxxxxxxxxxxxxxxxxxxxxx...
```
Copy it and add it as a Railway env var. This prevents the bot from needing to re-authenticate on every restart.

---

## 📦 Requirements

```
telethon
psycopg2-binary
requests
pytz
python-dotenv
```

Install with:
```bash
pip install telethon psycopg2-binary requests pytz python-dotenv
```

---

## 🔗 Links Used in the Bot

| Constant | Value | Purpose |
|---|---|---|
| `CHANNEL_LINK` | `https://t.me/Uzeron_AdsBot` | Updates channel (force-join) |
| `COMMUNITY_LINK` | `https://t.me/UzeronCommunity` | Community group (force-join) |
| `HOW_TO_USE_LINK` | `https://t.me/Uzeron_Ads` | How-to guide channel |
| `SUPPORT_LINK` | `https://t.me/Uzeron_Ads_support` | Support channel |
| `CONTACT_USERNAME` | `@Pandaysubscription` | Premium upgrade contact |
| `PREMIUM_BOT` | `@Uzeron_AdsBot` | Premium bot handle |

To change any link, edit the constants at the top of `free_bot.py`.

---

## 🚪 Force-Join Gate

Every user must join **both** before they can use the bot:
1. **Updates Channel** — `@Uzeron_AdsBot`
2. **Community Group** — `@UzeronCommunity`

**How it works:**
- On `/start`, the bot calls Telegram's `getChatMember` API for both chats.
- If the user hasn't joined either, they see a message with **Join Channel**, **Join Community**, and **Try Again** buttons.
- After joining, they tap **Try Again** and the check runs again.
- Once both are joined, the welcome screen appears.
- Every callback button also re-checks membership silently.

> ⚠️ The bot must be an **admin** in both the channel and the group for `getChatMember` to work on private chats. For public chats it works without admin rights.

---

## 🔑 User Login Flow

Users login with their **own Telegram API credentials**:

```
Step 1 → Send API_ID and API_HASH (from my.telegram.org/apps)
Step 2 → Send phone number  (+91xxxxxxxxxx)
Step 3 → Enter OTP via inline numpad buttons
       → If 2FA enabled: type password as a text message
```

After login, branding is applied immediately on the live session.

---

## 🏷️ Branding System

When a user logs in, the bot automatically:
- **Last name** → appends `• via @Uzeron_AdsBot` to whatever name they already have
  - `Rahul Kumar` → `Rahul Kumar • via @Uzeron_AdsBot`
  - `Rahul` → `Rahul • via @Uzeron_AdsBot`
  - *(empty)* → `• via @Uzeron_AdsBot`
- **Bio** → replaced with `Free Automated ads • via @Uzeron_AdsBot`

### Branding Enforcement (every 30 minutes)

The `branding_checker` runs every 30 minutes and checks all users who have `branding_set=1`. If the tag is missing from their last name:
- **Warning 1–2**: re-applies branding + sends warning message
- **Warning 3**: bans the user from the free tier permanently

---

## 📣 Broadcast (Admin Only)

Send `/broadcast` to the bot as an admin. The bot will ask for your message. Send it, and it gets delivered to **all non-banned users**.

```
/broadcast
→ Bot asks: "Send the message to broadcast"
→ You send: "🎉 New feature released! Check the dashboard."
→ Bot sends it to all users and reports: Sent: 312 / Failed: 2
```

Rate is limited to ~20 messages/second to stay within Telegram limits.

---

## 👑 Admin Commands

| Command | Description |
|---|---|
| `/users` | List all non-banned users |
| `/ban USER_ID` | Ban a specific user |
| `/stats` | Show total users and running campaigns |
| `/broadcast` | Send a message to all users |

---

## 🗃️ Database Schema

Table: `free_users`

| Column | Type | Description |
|---|---|---|
| `user_id` | BIGINT PK | Telegram user ID |
| `username` | TEXT | Telegram @username |
| `phone` | TEXT | Phone number after login |
| `api_id` | INTEGER | User's Telegram app API ID |
| `api_hash` | TEXT | User's Telegram app API Hash |
| `session_string` | TEXT | Telethon session string |
| `promo_message` | TEXT | The user's saved ad message |
| `is_active` | INTEGER | 1 = campaign running, 0 = stopped |
| `runtime_today` | INTEGER | Seconds used today |
| `last_reset` | TEXT | Date of last runtime reset (YYYY-MM-DD) |
| `warning_count` | INTEGER | Branding removal warnings (max 3) |
| `is_banned` | INTEGER | 1 = banned |
| `branding_set` | INTEGER | 1 = branding has been applied |
| `created_at` | TEXT | Registration timestamp |

---

## 🆓 Free Tier Limits

| Limit | Value |
|---|---|
| Max groups per round | 100 |
| Message delay | 60 seconds |
| Cycle delay (between rounds) | 10 minutes |
| Daily runtime cap | 8 hours |
| Branding warnings before ban | 3 |

---

## 🚀 Deploying on Railway

1. Create a new Railway project
2. Add a **PostgreSQL** plugin — Railway auto-sets `DATABASE_URL`
3. Set all env vars listed above
4. Deploy — Railway runs `python free_bot.py`
5. Check logs for the `BOT_SESSION_STRING` line on first run, copy it into env vars
6. Redeploy — the bot is now fully live

---

## 🐛 Troubleshooting

| Problem | Fix |
|---|---|
| Branding not setting | Check Railway logs for `apply_branding uid=` — the real error is printed there and sent to the first admin via logger bot |
| "Could not set branding" after login | Check if the API_ID/HASH the user entered matches their Telegram account |
| Force-join check always failing | Make sure `CHANNEL_USERNAME` and `COMMUNITY_USERNAME` constants in the code match the actual usernames (no `@`, no `https://t.me/`) |
| Bot not responding after restart | `BOT_SESSION_STRING` may be missing from env vars — check logs on startup |
| `getChatMember` returns error | Bot needs admin rights in private channels/groups to check membership |
