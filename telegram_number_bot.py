import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events
from telethon.errors import (
    AuthRestartError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telegram import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import RetryAfter
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)


BASE_DIR = Path(__file__).resolve().parent
USER_FILE = BASE_DIR / "users.json"
PROTECT_FILE = BASE_DIR / "protected.json"
LEARN_FILE = BASE_DIR / "learn.json"

BOT_TIMEZONE_NAME = os.getenv("BOT_TIMEZONE", "Asia/Kolkata")
BOT_TIMEZONE = ZoneInfo(BOT_TIMEZONE_NAME)
START_HOUR = int(os.getenv("BOT_START_HOUR", "6"))
END_HOUR = int(os.getenv("BOT_END_HOUR", "1"))

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1004442136193"))
GROUP_ID_JOIN = int(os.getenv("GROUP_ID_JOIN", "-1003926294980"))
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/+it1jwycPHoQ4YzM1")
GROUP_URL = os.getenv("GROUP_URL", "https://t.me/infotele007")

client: TelegramClient | None = None
bot_app = None
owner_id: int | None = None
pending: dict[int, dict[str, int | str]] = {}


class LoginPendingError(RuntimeError):
    """The user must update a one-time Telegram login Secret."""


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required Secret: {name}")
    return value


def get_config() -> tuple[int, str, str, int]:
    try:
        api_id = int(required_env("API_ID"))
        configured_owner_id = int(required_env("OWNER_ID"))
    except ValueError as exc:
        raise RuntimeError("API_ID and OWNER_ID must be numbers") from exc

    return (
        api_id,
        required_env("API_HASH"),
        required_env("BOT_TOKEN"),
        configured_owner_id,
    )


def ensure_data_files() -> None:
    for path in (USER_FILE, PROTECT_FILE, LEARN_FILE):
        if not path.exists():
            path.write_text("{}", encoding="utf-8")


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and path == PROTECT_FILE:
            return {str(item).strip().lower(): "protected" for item in data}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(data, indent=4, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def load_protected() -> dict:
    return load_json(PROTECT_FILE)


def save_protected(data: dict) -> None:
    save_json(PROTECT_FILE, data)


def load_users() -> dict:
    return load_json(USER_FILE)


def save_users(data: dict) -> None:
    save_json(USER_FILE, data)


def load_learn() -> dict:
    return load_json(LEARN_FILE)


def save_learn(data: dict) -> None:
    save_json(LEARN_FILE, data)


def record_successful_search(user_id: int, target: str) -> None:
    target = target.strip()
    if not target:
        return

    learn = load_learn()
    uid = str(user_id)
    searches = learn.setdefault(uid, [])
    if not isinstance(searches, list):
        searches = []
        learn[uid] = searches
    if target not in searches:
        searches.append(target)
        save_learn(learn)


def today_string() -> str:
    return str(datetime.now(BOT_TIMEZONE).date())


def default_user(today: str | None = None) -> dict:
    return {
        "daily": 2,
        "bonus": 0,
        "date": today or today_string(),
        "referrals": [],
    }


def normalize_user(user: dict, today: str) -> None:
    try:
        user["daily"] = max(0, int(user.get("daily", 2)))
    except (TypeError, ValueError):
        user["daily"] = 2
    try:
        user["bonus"] = max(0, int(user.get("bonus", 0)))
    except (TypeError, ValueError):
        user["bonus"] = 0
    if not isinstance(user.get("referrals"), list):
        user["referrals"] = []
    if user.get("date") != today:
        # Only the daily allocation resets. Referral bonus is permanent.
        user["daily"] = 2
        user["date"] = today


def get_user(user_id: int) -> dict:
    users = load_users()
    uid = str(user_id)
    today = today_string()
    user = users.setdefault(uid, default_user(today))
    normalize_user(user, today)

    save_users(users)
    return user


def remaining_limit(user_id: int) -> int:
    user = get_user(user_id)
    return max(0, int(user.get("daily", 0))) + max(0, int(user.get("bonus", 0)))


def result_does_not_consume_limit(result_text: str) -> bool:
    low = result_text.lower()
    phrases = (
        "not found",
        "no result",
        "no found",
        "data not found",
        "no data linked",
    )
    return any(phrase in low for phrase in phrases)


def use_limit(user_id: int, result_text: str | None = None) -> bool:
    if result_text and result_does_not_consume_limit(result_text):
        return True

    users = load_users()
    uid = str(user_id)
    today = today_string()
    user = users.setdefault(uid, default_user(today))
    normalize_user(user, today)

    # Referral bonus is consumed first; the daily allocation is second.
    if user["bonus"] > 0:
        user["bonus"] -= 1
    elif user["daily"] > 0:
        user["daily"] -= 1
    else:
        return False

    save_users(users)
    return True


def reset_limit(user_id: int) -> None:
    users = load_users()
    uid = str(user_id)
    today = today_string()
    user = users.setdefault(uid, default_user(today))
    normalize_user(user, today)
    user["daily"] = 2
    # Do not modify user["bonus"]: referral credits carry forward.
    save_users(users)


def normalized_target(value: str) -> str:
    return value.strip().lower()


def is_protected(value: str) -> bool:
    return normalized_target(value) in load_protected()


def is_owner(user_id: int) -> bool:
    return owner_id is not None and user_id == owner_id


def join_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_URL)],
            [InlineKeyboardButton("👥 Join Group", url=GROUP_URL)],
            [InlineKeyboardButton("✅ Done", callback_data="check_join")],
        ]
    )


def menu_text(user: dict) -> str:
    return f"""
🤖 Telegram to Number Bot

🔍 Use /tg <username or number>
📞 Use /num <phone number>
🚗 Use /veh <vehicle number>

📊 Daily Limit: {user["daily"]}
🎁 Referral Bonus: {user["bonus"]}
✅ Remaining today: {user["daily"] + user["bonus"]}

✨ Purchase Premium - @Himanshupa007
💎 Premium = Unlimited Access ✨
"""


async def membership_status(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> bool | None:
    try:
        channel_member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        group_member = await context.bot.get_chat_member(GROUP_ID_JOIN, user_id)
        allowed = {"member", "administrator", "creator", "restricted"}
        return (
            channel_member.status in allowed
            and group_member.status in allowed
            and getattr(channel_member, "is_member", True)
            and getattr(group_member, "is_member", True)
        )
    except Exception as exc:
        print(f"Membership check failed: {type(exc).__name__}")
        return None


async def require_membership(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    user_id = update.effective_user.id
    joined = await membership_status(context, user_id)
    if joined is True:
        return True
    if joined is None:
        await update.message.reply_text("⚠️ Membership check failed. Try again.")
    else:
        await update.message.reply_text(
            "⚠️ Must join our Channel & Group first!",
            reply_markup=join_markup(),
        )
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = get_user(user_id)

    if context.args:
        referrer_id = context.args[0].strip()
        if referrer_id != str(user_id):
            users = load_users()
            referrer = users.get(referrer_id)
            referrals = referrer.setdefault("referrals", []) if referrer else []
            if referrer and str(user_id) not in referrals:
                referrals.append(str(user_id))
                referrer["bonus"] = int(referrer.get("bonus", 0)) + 1
                save_users(users)

    joined = await membership_status(context, user_id)
    if joined is None:
        await update.message.reply_text("⚠️ Membership check failed. Try again.")
    elif not joined:
        await update.message.reply_text(
            "⚠️ Must join our Channel & Group first!",
            reply_markup=join_markup(),
        )
    else:
        await update.message.reply_text(
            menu_text(user),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎁 Refer", callback_data="refer")]]
            ),
        )


async def check_join(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    joined = await membership_status(context, query.from_user.id)
    if joined is True:
        await query.answer("✅ Verified! You joined both.")
        user = get_user(query.from_user.id)
        await query.edit_message_text(
            menu_text(user),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎁 Refer", callback_data="refer")]]
            ),
        )
    elif joined is False:
        await query.answer("❌ Join both first.", show_alert=True)
        await query.edit_message_text(
            "❌ Join request pending.\n\nJoin both and press Done again.",
            reply_markup=join_markup(),
        )
    else:
        await query.answer("⚠️ Check failed. Try again.", show_alert=True)


async def refer_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    me = await context.bot.get_me()
    username = me.username or "your_bot"
    refer_link = f"https://t.me/{username}?start={query.from_user.id}"
    await query.answer("🔗 Referral link generated")
    await query.edit_message_text(
        f"🔗 Your referral link:\n{refer_link}\n\n"
        "Share this link with friends. When they join using it, "
        "you will get +1 limit."
        ,
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "📋 Copy referral link",
                    copy_text=CopyTextButton(text=refer_link),
                )
            ]]
        ),
    )


async def protect_number(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Not allowed")
        return
    if not context.args:
        await update.message.reply_text("Use:\n/protect_number <number>")
        return
    number = normalized_target(context.args[0])
    protected = load_protected()
    protected[number] = "protected_number"
    save_protected(protected)
    await update.message.reply_text(f"✅ Protected number {number} successfully.")


async def protect_username(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Not allowed")
        return
    if not context.args:
        await update.message.reply_text("Use:\n/protect_username <username>")
        return
    username = context.args[0].strip()
    if not username.startswith("@"):
        username = f"@{username}"
    username = normalized_target(username)
    protected = load_protected()
    protected[username] = "protected_username"
    save_protected(protected)
    await update.message.reply_text(
        f"✅ Protected username {username} successfully."
    )


async def userscount(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    del context
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Not allowed")
        return
    await update.message.reply_text(f"👥 Total Users: {len(load_users())}")


async def addlimit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Not allowed")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Use:\n/addlimit <user_id> <limit>")
        return
    try:
        new_limit = int(context.args[1])
        if new_limit < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid limit number")
        return

    users = load_users()
    uid = str(context.args[0])
    user = users.setdefault(
        uid,
        default_user(today_string()),
    )
    normalize_user(user, today_string())
    user["daily"] = new_limit
    user["date"] = today_string()
    save_users(users)
    await update.message.reply_text(
        f"✅ Daily limit for {uid} set to {new_limit}"
    )


async def restlimit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Not allowed")
        return
    if context.args and context.args[0].lower() == "all":
        users = load_users()
        today = today_string()
        for user in users.values():
            normalize_user(user, today)
            user["daily"] = 2
            user["date"] = today
        save_users(users)
        await update.message.reply_text(
            "✅ All users' limits reset to 2 + referral bonus."
        )
        return
    if context.args:
        try:
            reset_limit(int(context.args[0]))
        except ValueError:
            await update.message.reply_text("Use:\n/restlimit <user_id> or all")
            return
        await update.message.reply_text("✅ User limit reset.")
        return
    await update.message.reply_text("Use:\n/restlimit <user_id> or all")


async def broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Only Owner can use /broadcast.")
        return
    if not context.args:
        await update.message.reply_text("Use:\n/broadcast <message>")
        return
    if bot_app is None:
        await update.message.reply_text("⚠️ Bot is still starting.")
        return

    message_text = " ".join(context.args)
    count = 0
    for uid in load_users():
        try:
            await bot_app.bot.send_message(int(uid), message_text)
            count += 1
            await asyncio.sleep(0.05)
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
        except Exception:
            continue
    await update.message.reply_text(f"✅ Broadcast sent to {count} users.")


async def send_lookup(
    update: Update,
    text: str,
    display_text: str = "🔍 Searching...",
    target: str | None = None,
) -> None:
    if client is None:
        await update.message.reply_text("⚠️ Search service is unavailable.")
        return
    sent = await client.send_message("@Kihoebot", text)
    pending[sent.id] = {
        "chat_id": int(update.effective_chat.id),
        "user_id": int(update.effective_user.id),
        "target": target or text,
    }
    await update.message.reply_text(display_text)


async def prepare_lookup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    if not await require_membership(update, context):
        return False
    if remaining_limit(update.effective_user.id) <= 0:
        await update.message.reply_text(
            "❌ Daily limit finished. Try again tomorrow or use referral bonus."
        )
        return False
    return True


async def tg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Use:\n/tg <number or username>")
        return
    query = context.args[0].strip()
    if not await prepare_lookup(update, context):
        return
    if is_protected(query):
        await update.message.reply_text("❌ This target is protected.")
        return
    await send_lookup(update, query, target=query)


async def num(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Use:\n/num <number>")
        return
    full_text = " ".join(context.args).strip()
    if not await prepare_lookup(update, context):
        return
    if is_protected(full_text):
        await update.message.reply_text(
            "❌ This number is protected and cannot be searched."
        )
        return
    await send_lookup(
        update,
        f"/num {full_text}",
        f"🔍 Searching: {full_text}",
        target=full_text,
    )


async def veh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Use:\n/veh <vehicle number>")
        return
    full_text = " ".join(context.args).strip()
    if not await prepare_lookup(update, context):
        return
    if is_protected(full_text):
        await update.message.reply_text(
            "❌ This vehicle number is protected and cannot be searched."
        )
        return
    await send_lookup(update, f"/veh {full_text}", target=full_text)


async def check_reply(event) -> None:
    text = (event.message.message or "").strip()
    if not text:
        return
    low = text.lower()
    if "searching" in low or "loading" in low:
        return
    if bot_app is None:
        return

    item = next(iter(pending.items()), None)
    if item is None:
        return
    message_id, request = item
    await asyncio.sleep(1)
    try:
        await bot_app.bot.send_message(request["chat_id"], text)
        if not result_does_not_consume_limit(text):
            record_successful_search(
                int(request["user_id"]),
                str(request["target"]),
            )
        use_limit(request["user_id"], text)
    finally:
        pending.pop(message_id, None)


async def new_reply(event) -> None:
    await check_reply(event)


async def edit_reply(event) -> None:
    await check_reply(event)


async def login_telethon(telethon_client: TelegramClient) -> None:
    await telethon_client.connect()
    if await telethon_client.is_user_authorized():
        return

    phone = required_env("TELETHON_PHONE")
    try:
        sent_code = await telethon_client.send_code_request(phone)
    except AuthRestartError as exc:
        raise LoginPendingError(
            "Telegram asked to restart authorization. Try again once."
        ) from exc

    code = os.getenv("TELETHON_CODE_LATEST")
    if not code:
        raise LoginPendingError(
            "Telegram code sent. Add it as TELETHON_CODE_LATEST, then restart."
        )

    try:
        await telethon_client.sign_in(
            phone=phone,
            code=code,
            phone_code_hash=sent_code.phone_code_hash,
        )
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
        raise LoginPendingError(
            "The Telegram code is invalid or expired. Request a fresh code."
        ) from exc
    except SessionPasswordNeededError as exc:
        password = os.getenv("TELETHON_PASSWORD")
        if not password:
            raise LoginPendingError(
                "Telegram 2FA password required. Add TELETHON_PASSWORD."
            ) from exc
        await telethon_client.sign_in(password=password)


async def run_active_window(telethon_client: TelegramClient) -> None:
    global bot_app
    bot_app = ApplicationBuilder().token(required_env("BOT_TOKEN")).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("tg", tg))
    bot_app.add_handler(CommandHandler("num", num))
    bot_app.add_handler(CommandHandler("veh", veh))
    bot_app.add_handler(CommandHandler("userscount", userscount))
    bot_app.add_handler(CommandHandler("addlimit", addlimit))
    bot_app.add_handler(CommandHandler("restlimit", restlimit))
    bot_app.add_handler(CommandHandler("broadcast", broadcast))
    bot_app.add_handler(CommandHandler("protect_number", protect_number))
    bot_app.add_handler(CommandHandler("protect_username", protect_username))
    bot_app.add_handler(CommandHandler("protectnum", protect_number))
    bot_app.add_handler(CallbackQueryHandler(check_join, pattern="^check_join$"))
    bot_app.add_handler(CallbackQueryHandler(refer_button, pattern="^refer$"))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()

    now = datetime.now(BOT_TIMEZONE)
    boundary = next_boundary(now, END_HOUR)
    stop_task = asyncio.create_task(
        asyncio.sleep(max(1, (boundary - now).total_seconds()))
    )
    client_task = asyncio.create_task(
        telethon_client.run_until_disconnected()
    )
    try:
        await asyncio.wait(
            {stop_task, client_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for task in (stop_task, client_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stop_task, client_task, return_exceptions=True)
        if bot_app.updater:
            await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        bot_app = None


def is_active_window(now: datetime) -> bool:
    if START_HOUR < END_HOUR:
        return START_HOUR <= now.hour < END_HOUR
    return now.hour >= START_HOUR or now.hour < END_HOUR


def next_boundary(now: datetime, hour: int) -> datetime:
    boundary = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if boundary <= now:
        boundary += timedelta(days=1)
    return boundary


async def wait_for_schedule() -> None:
    now = datetime.now(BOT_TIMEZONE)
    target_hour = END_HOUR if is_active_window(now) else START_HOUR
    boundary = next_boundary(now, target_hour)
    print(
        f"Bot {'active' if is_active_window(now) else 'paused'} until "
        f"{boundary.strftime('%Y-%m-%d %H:%M')} ({BOT_TIMEZONE_NAME})"
    )
    await asyncio.sleep(max(1, (boundary - now).total_seconds()))


async def main() -> None:
    global client, owner_id
    api_id, api_hash, _, owner_id = get_config()
    ensure_data_files()

    session_string = os.getenv("TELETHON_SESSION_STRING")
client = TelegramClient(StringSession(session_string), api_id, api_hash)

    client.add_event_handler(
        new_reply,
        events.NewMessage(from_users="@Kihoebot"),
    )
    client.add_event_handler(
        edit_reply,
        events.MessageEdited(from_users="@Kihoebot"),
    )

    print(
        f"Schedule: {START_HOUR:02d}:00–{END_HOUR:02d}:00 "
        f"({BOT_TIMEZONE_NAME})"
    )

    while True:
        if not is_active_window(datetime.now(BOT_TIMEZONE)):
            await wait_for_schedule()
            continue
        try:
            await login_telethon(client)
            print("Bot running...")
            await run_active_window(client)
        except LoginPendingError as exc:
            print(f"Login pending: {exc}")
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Bot stopped: {type(exc).__name__}: {exc}")
            await asyncio.sleep(30)
        finally:
            if client.is_connected():
                await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
