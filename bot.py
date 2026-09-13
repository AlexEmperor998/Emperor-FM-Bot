import asyncio
import json
import logging
import os
import re
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeChat,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", "0"))

CHANNEL_1_ID = int(os.getenv("CHANNEL_1_ID", "0"))
CHANNEL_1_USERNAME = os.getenv("CHANNEL_1_USERNAME", "").strip()
CHANNEL_1_NAME = os.getenv("CHANNEL_1_NAME", "Channel 1").strip()

CHANNEL_2_ID = int(os.getenv("CHANNEL_2_ID", "0"))
CHANNEL_2_USERNAME = os.getenv("CHANNEL_2_USERNAME", "").strip()
CHANNEL_2_NAME = os.getenv("CHANNEL_2_NAME", "Channel 2").strip()

# Main Channel where admin-created button posts will be published.
# If MAIN_CHANNEL_ID is not set, CHANNEL_1_ID is used.
MAIN_CHANNEL_ID = int(os.getenv("MAIN_CHANNEL_ID", str(CHANNEL_1_ID)))

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
}

REQUIRED_CHANNELS = [
    {
        "id": CHANNEL_1_ID,
        "username": CHANNEL_1_USERNAME,
        "name": CHANNEL_1_NAME,
    },
    {
        "id": CHANNEL_2_ID,
        "username": CHANNEL_2_USERNAME,
        "name": CHANNEL_2_NAME,
    },
]

FILE_EXPIRY_SECONDS = 10 * 60
REQUEST_COOLDOWN_SECONDS = 15
AUTO_RANGE_GAP_SECONDS = 0.5

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)



SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise RuntimeError(
        "SUPABASE_URL and SUPABASE_SECRET_KEY are required."
    )

SUPABASE_REST_URL = f"{SUPABASE_URL}/rest/v1"


def supabase_request(method: str, table: str, params=None, data=None, headers=None):
    url = f"{SUPABASE_REST_URL}/{table}"
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)

    request_headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
    }

    if data is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    import urllib.request
    import urllib.error

    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return response.status, None, dict(response.headers)
            try:
                return response.status, json.loads(raw), dict(response.headers)
            except json.JSONDecodeError:
                return response.status, raw, dict(response.headers)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase HTTP {e.code}: {error_body}"
        ) from e


def supabase_count(table: str, params=None) -> int:
    query = dict(params or {})
    query["select"] = "id"
    query["limit"] = "1"

    status, rows, headers = supabase_request(
        "GET",
        table,
        params=query,
        headers={"Prefer": "count=exact"},
    )

    content_range = ""
    for header_name, header_value in headers.items():
        if header_name.lower() == "content-range":
            content_range = header_value
            break

    if "/" in content_range:
        try:
            return int(content_range.rsplit("/", 1)[1])
        except ValueError:
            pass

    return len(rows or [])


def init_db():
    # Supabase tables are created separately in the Supabase SQL Editor.
    # No local database is used by the live bot.
    status, _, _ = supabase_request(
        "GET",
        "files",
        params={"select": "id", "limit": "1"},
    )
    if status != 200:
        raise RuntimeError("Supabase database connection failed.")

VALID_TYPES = {"audio", "video", "pdf", "image", "document"}


def normalize_file_type(value: str):
    value = value.lower().strip()
    aliases = {
        "aud": "audio",
        "mp3": "audio",
        "m4a": "audio",
        "aac": "audio",
        "wav": "audio",
        "ogg": "audio",
        "flac": "audio",
        "mp4": "video",
        "mkv": "video",
        "mov": "video",
        "jpg": "image",
        "jpeg": "image",
        "png": "image",
        "webp": "image",
        "doc": "document",
        "docx": "document",
    }
    value = aliases.get(value, value)
    if value not in VALID_TYPES:
        raise ValueError("Invalid file type")
    return value


def register_file(story_id: int, episode: int, file_type: str, storage_message_id: int):
    supabase_request(
        "POST",
        "files",
        data={
            "story_id": story_id,
            "episode": episode,
            "file_type": file_type,
            "storage_message_id": storage_message_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        headers={
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )


def get_file(story_id: int, episode: int, file_type: str):
    status, rows, _ = supabase_request(
        "GET",
        "files",
        params={
            "select": "id,story_id,episode,file_type,storage_message_id,created_at",
            "story_id": f"eq.{story_id}",
            "episode": f"eq.{episode}",
            "file_type": f"eq.{file_type}",
            "limit": "1",
        },
    )
    if status == 200 and rows:
        return rows[0]
    return None


def parse_audio_filename(filename: str):
    if not filename:
        return None

    name = os.path.basename(filename)
    match = re.match(
        r"^(\d+)x(\d+)\.(m4a|mp3|aac|wav|ogg|flac)$",
        name,
        re.IGNORECASE,
    )
    if not match:
        return None

    return int(match.group(1)), int(match.group(2))


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def check_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    not_joined = []

    for channel in REQUIRED_CHANNELS:
        if not channel["id"]:
            continue

        try:
            member = await context.bot.get_chat_member(
                chat_id=channel["id"],
                user_id=user_id,
            )

            if member.status in ("left", "kicked"):
                not_joined.append(channel)

        except Exception as e:
            logger.error(
                "Membership check failed for %s: %s",
                channel["name"],
                e,
            )
            not_joined.append(channel)

    return not_joined


def welcome_text():
    return (
        "👋 <b>Welcome to Emperor FM File Bot!</b>\n\n"
        "📁 Aapki requested file yahan se receive hogi.\n\n"
        "⚠️ <b>Important:</b>\n"
        "File receive hone ke <b>10 minutes</b> baad ye message automatically delete ho jayega.\n\n"
        "🔗 Zarurat padne par same link se file dobara request kar sakte hain.\n\n"
        "👇 File receive karne ke liye neeche diye gaye sabhi channels join karein."
    )


def build_join_keyboard(not_joined):
    buttons = []

    for channel in not_joined:
        username = channel["username"].lstrip("@")

        if username:
            buttons.append([
                InlineKeyboardButton(
                    f"➕ Join {channel['name']}",
                    url=f"https://t.me/{username}",
                )
            ])

    buttons.append([
        InlineKeyboardButton(
            "♻️ Try Again",
            callback_data="try_again",
        )
    ])

    return InlineKeyboardMarkup(buttons)


def build_episode_keyboard(bot_username, story_id, start_ep, end_ep, file_type):
    rows = []
    current_row = []

    for episode in range(start_ep, end_ep + 1):
        payload = f"f_{story_id}_{episode}_{file_type}"
        url = f"https://t.me/{bot_username}?start={payload}"

        current_row.append(
            InlineKeyboardButton(
                f"EP {episode}",
                url=url,
            )
        )

        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    return InlineKeyboardMarkup(rows)


async def send_join_required(update: Update, not_joined):
    keyboard = build_join_keyboard(not_joined)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=welcome_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    elif update.message:
        await update.message.reply_text(
            text=welcome_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


def parse_file_payload(payload: str):
    if not payload:
        return None

    parts = payload.split("_")

    if len(parts) != 4 or parts[0] != "f":
        return None

    try:
        story_id = int(parts[1])
        episode = int(parts[2])
        file_type = normalize_file_type(parts[3])
    except Exception:
        return None

    return story_id, episode, file_type


def parse_range_payload(payload: str):
    if not payload:
        return None

    parts = payload.split("_")

    if len(parts) != 5 or parts[0] != "r":
        return None

    try:
        story_id = int(parts[1])
        start_ep = int(parts[2])
        end_ep = int(parts[3])
        file_type = normalize_file_type(parts[4])

        if start_ep < 1 or end_ep < start_ep:
            return None

    except Exception:
        return None

    return story_id, start_ep, end_ep, file_type


def check_cooldown(user_id: int, file_key: str):
    now = int(time.time())

    status, rows, _ = supabase_request(
        "GET",
        "cooldowns",
        params={
            "select": "last_request",
            "user_id": f"eq.{user_id}",
            "file_key": f"eq.{file_key}",
            "limit": "1",
        },
    )

    if status == 200 and rows:
        elapsed = now - int(rows[0]["last_request"])
        if elapsed < REQUEST_COOLDOWN_SECONDS:
            return False

    supabase_request(
        "POST",
        "cooldowns",
        data={
            "user_id": user_id,
            "file_key": file_key,
            "last_request": now,
        },
        headers={
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    return True


def save_request(
    user_id,
    story_id,
    episode,
    file_type,
    sent_message_id,
    expires_at,
):
    supabase_request(
        "POST",
        "requests",
        data={
            "user_id": user_id,
            "story_id": story_id,
            "episode": episode,
            "file_type": file_type,
            "sent_message_id": sent_message_id,
            "expires_at": expires_at.isoformat(),
        },
        headers={
            "Prefer": "return=minimal",
        },
    )


def delete_request_record(user_id: int, message_id: int):
    supabase_request(
        "DELETE",
        "requests",
        params={
            "user_id": f"eq.{user_id}",
            "sent_message_id": f"eq.{message_id}",
        },
    )


async def delete_expired_message(
    context,
    user_id,
    message_id,
    expires_at,
):
    try:
        now = datetime.now(timezone.utc)
        wait_seconds = (expires_at - now).total_seconds()

        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)

        try:
            await context.bot.delete_message(
                chat_id=user_id,
                message_id=message_id,
            )
            logger.info(
                "Deleted expired message user=%s message=%s",
                user_id,
                message_id,
            )
        except Exception as e:
            logger.info(
                "Message deletion failed or already deleted: %s",
                e,
            )

        delete_request_record(user_id, message_id)

    except asyncio.CancelledError:
        raise

    except Exception:
        logger.exception("Expiry task failed")


async def recover_pending_deletions(application: Application):
    status, rows, _ = supabase_request(
        "GET",
        "requests",
        params={
            "select": "id,user_id,story_id,episode,file_type,sent_message_id,expires_at",
            "limit": "1000",
        },
    )

    if status != 200 or not rows:
        return

    now = datetime.now(timezone.utc)

    for row in rows:
        try:
            expires_at = datetime.fromisoformat(
                row["expires_at"].replace("Z", "+00:00")
            )

            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)

            if expires_at <= now:
                try:
                    await application.bot.delete_message(
                        chat_id=row["user_id"],
                        message_id=row["sent_message_id"],
                    )
                except Exception:
                    pass

                delete_request_record(
                    row["user_id"],
                    row["sent_message_id"],
                )
                continue

            application.create_task(
                delete_expired_message(
                    application,
                    row["user_id"],
                    row["sent_message_id"],
                    expires_at,
                )
            )

        except Exception:
            logger.exception(
                "Could not recover request %s",
                row.get("id"),
            )


async def private_user_message_guard(update, context):
    message = update.message
    if not message:
        return

    user = update.effective_user
    if user and is_admin(user.id):
        return

    try:
        await message.delete()
    except Exception:
        # If Telegram does not allow deletion, do not send a reply.
        pass


async def post_init(application: Application):
    init_db()

    # Keep the command menu clean for normal users.
    await application.bot.set_my_commands([
        BotCommand("start", "Start the bot"),
    ])

    # Admins get the requested admin command menu.
    admin_commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("stats", "Bot statistics"),
        BotCommand("link", "Generate file link"),
        BotCommand("postbuttons", "Post Main Channel buttons"),
    ]
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            logger.exception("Could not set admin command menu for %s", admin_id)
    await recover_pending_deletions(application)
    logger.info("Bot database initialized.")


async def send_requested_file(
    update,
    context,
    story_id,
    episode,
    file_type,
):
    user = update.effective_user

    if not user:
        return

    file_key = f"{story_id}_{episode}_{file_type}"

    if not check_cooldown(user.id, file_key):
        if update.callback_query:
            await update.callback_query.answer(
                "⏳ Please wait a few seconds.",
                show_alert=True,
            )

        elif update.message:
            await update.message.reply_text(
                "⏳ Please wait a few seconds and try again."
            )

        return

    file_row = get_file(
        story_id,
        episode,
        file_type,
    )

    if not file_row:
        text = (
            "❌ <b>File unavailable</b>\n\n"
            "Ye file abhi Storage Channel mein "
            "registered nahi hai."
        )

        if update.callback_query:
            await update.callback_query.message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
            )

        elif update.message:
            await update.message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
            )

        return

    try:
        sent = await context.bot.copy_message(
            chat_id=user.id,
            from_chat_id=STORAGE_CHANNEL_ID,
            message_id=file_row["storage_message_id"],
        )

        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=FILE_EXPIRY_SECONDS)
        )

        save_request(
            user_id=user.id,
            story_id=story_id,
            episode=episode,
            file_type=file_type,
            sent_message_id=sent.message_id,
            expires_at=expires_at,
        )

        context.application.create_task(
            delete_expired_message(
                context,
                user.id,
                sent.message_id,
                expires_at,
            )
        )

        logger.info(
            "Sent file story=%s episode=%s type=%s user=%s",
            story_id,
            episode,
            file_type,
            user.id,
        )

    except Exception:
        logger.exception("File sending error")

        error_text = (
            "❌ File send karte waqt error aa gaya.\n"
            "Please thodi der baad try karein."
        )

        if update.callback_query:
            await update.callback_query.message.reply_text(
                error_text
            )

        elif update.message:
            await update.message.reply_text(
                error_text
            )


async def show_range_menu(
    update,
    context,
    story_id,
    start_ep,
    end_ep,
    file_type,
):
    bot_username = context.bot.username

    keyboard = build_episode_keyboard(
        bot_username,
        story_id,
        start_ep,
        end_ep,
        file_type,
    )

    text = (
        f"📚 <b>Episodes {start_ep} to {end_ep}</b>\n\n"
        "👇 Episode select karein:"
    )

    if update.message:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    elif update.callback_query:
        await update.callback_query.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )



async def auto_send_range(update, context, story_id, start_ep, end_ep, file_type):
    user = update.effective_user
    if not user:
        return

    task = asyncio.current_task()
    context.user_data["auto_range_task"] = task
    try:
        for episode in range(start_ep, end_ep + 1):
            if context.user_data.get("auto_range_cancelled"):
                break
            await send_requested_file(update, context, story_id, episode, file_type)
            if episode < end_ep:
                try:
                    await asyncio.sleep(AUTO_RANGE_GAP_SECONDS)
                except asyncio.CancelledError:
                    break
    except asyncio.CancelledError:
        pass
    finally:
        if context.user_data.get("auto_range_task") is task:
            context.user_data.pop("auto_range_task", None)
        context.user_data.pop("auto_range_cancelled", None)


async def start_auto_range(update, context, story_id, start_ep, end_ep, file_type):
    old_task = context.user_data.get("auto_range_task")
    if old_task and not old_task.done():
        old_task.cancel()

    context.user_data["auto_range_cancelled"] = False
    text = (
        f"Class {start_ep:02d} to {end_ep:02d} ❄️ Ranking and Comparison\n\n"
        "📚 <b>Auto Episode Delivery</b>\n"
        "Episodes automatically bheje ja rahe hain...\n\n"
        "Har episode ke beech 500 milliseconds ka gap hai."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_auto_range")]
    ])
    if update.message:
        status_message = await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    else:
        status_message = await update.callback_query.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    context.user_data["auto_range_status_message_id"] = status_message.message_id
    task = context.application.create_task(
        auto_send_range(update, context, story_id, start_ep, end_ep, file_type)
    )
    context.user_data["auto_range_task"] = task


async def cancel_auto_range(update, context):
    query = update.callback_query
    await query.answer("❌ Auto delivery cancelled.")
    task = context.user_data.get("auto_range_task")
    context.user_data["auto_range_cancelled"] = True
    if task and not task.done():
        task.cancel()
    try:
        await query.edit_message_text(
            "❌ <b>Auto episode delivery cancelled.</b>", parse_mode=ParseMode.HTML
        )
    except Exception:
        pass
    context.user_data.pop("auto_range_task", None)
    context.user_data.pop("auto_range_status_message_id", None)
    context.user_data.pop("auto_range_cancelled", None)


async def start_command(update, context):
    if not update.message:
        return

    user = update.effective_user
    payload = context.args[0] if context.args else None

    file_request = parse_file_payload(payload)
    range_request = parse_range_payload(payload)

    if not file_request and not range_request:
        await update.message.reply_text(
            welcome_text(),
            parse_mode=ParseMode.HTML,
        )
        return

    not_joined = await check_membership(
        user.id,
        context,
    )

    if not_joined:
        if file_request:
            story_id, episode, file_type = file_request

            context.user_data["pending_request"] = {
                "story_id": story_id,
                "episode": episode,
                "file_type": file_type,
            }

        else:
            story_id, start_ep, end_ep, file_type = range_request

            context.user_data["pending_range"] = {
                "story_id": story_id,
                "start_ep": start_ep,
                "end_ep": end_ep,
                "file_type": file_type,
            }

        await send_join_required(
            update,
            not_joined,
        )
        return

    if file_request:
        story_id, episode, file_type = file_request

        await send_requested_file(
            update,
            context,
            story_id,
            episode,
            file_type,
        )
        return

    story_id, start_ep, end_ep, file_type = range_request

    await start_auto_range(
        update,
        context,
        story_id,
        start_ep,
        end_ep,
        file_type,
    )


async def try_again(update, context):
    query = update.callback_query
    await query.answer()

    user = query.from_user

    pending_file = context.user_data.get(
        "pending_request"
    )

    pending_range = context.user_data.get(
        "pending_range"
    )

    not_joined = await check_membership(
        user.id,
        context,
    )

    if not_joined:
        await send_join_required(
            update,
            not_joined,
        )
        return

    try:
        await query.message.delete()
    except Exception:
        pass

    if pending_file:
        await send_requested_file(
            update,
            context,
            pending_file["story_id"],
            pending_file["episode"],
            pending_file["file_type"],
        )

        context.user_data.pop(
            "pending_request",
            None,
        )
        return

    if pending_range:
        await start_auto_range(
            update,
            context,
            pending_range["story_id"],
            pending_range["start_ep"],
            pending_range["end_ep"],
            pending_range["file_type"],
        )

        context.user_data.pop(
            "pending_range",
            None,
        )
        return

    await query.message.reply_text(
        "❌ Request expire ho gaya hai.\n\n"
        "Please Main Channel se link dobara open karein."
    )


async def storage_channel_post(update, context):
    message = update.channel_post

    if not message or message.chat.id != STORAGE_CHANNEL_ID:
        return

    if message.text:
        text = message.text.strip()

        if text.startswith("/register"):
            parts = text.split()

            if len(parts) != 4:
                await message.reply_text(
                    "❌ Format:\n\n"
                    "/register STORY_ID EPISODE TYPE\n\n"
                    "Example:\n"
                    "/register 1 2450 pdf"
                )
                return

            try:
                story_id = int(parts[1])
                episode = int(parts[2])
                file_type = normalize_file_type(parts[3])

            except Exception:
                await message.reply_text(
                    "❌ Invalid register command."
                )
                return

            if not message.reply_to_message:
                await message.reply_text(
                    "❌ /register command ko file/message "
                    "ke reply mein bhejein."
                )
                return

            target = message.reply_to_message

            register_file(
                story_id=story_id,
                episode=episode,
                file_type=file_type,
                storage_message_id=target.message_id,
            )

            await message.reply_text(
                "✅ File registered successfully.\n\n"
                f"Story ID: {story_id}\n"
                f"Episode: {episode}\n"
                f"Type: {file_type}\n"
                f"Message ID: {target.message_id}"
            )

            return

    filename = None

    if message.audio:
        filename = message.audio.file_name

    elif message.document:
        filename = message.document.file_name

    elif message.video:
        filename = message.video.file_name

    parsed = parse_audio_filename(filename)

    if not parsed:
        return

    story_id, episode = parsed

    register_file(
        story_id=story_id,
        episode=episode,
        file_type="audio",
        storage_message_id=message.message_id,
    )

    logger.info(
        "Auto registered audio story=%s episode=%s message=%s",
        story_id,
        episode,
        message.message_id,
    )


async def register_private_command(update, context):
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text(
            "❌ Admin only."
        )
        return

    if len(context.args) != 4:
        await update.message.reply_text(
            "❌ Format:\n\n"
            "/register STORY_ID EPISODE TYPE MESSAGE_ID\n\n"
            "Example:\n"
            "/register 1 2450 pdf 1234"
        )
        return

    try:
        story_id = int(context.args[0])
        episode = int(context.args[1])
        file_type = normalize_file_type(context.args[2])
        message_id = int(context.args[3])

    except Exception:
        await update.message.reply_text(
            "❌ Invalid values."
        )
        return

    register_file(
        story_id=story_id,
        episode=episode,
        file_type=file_type,
        storage_message_id=message_id,
    )

    await update.message.reply_text(
        "✅ File registered successfully.\n\n"
        f"Story ID: {story_id}\n"
        f"Episode: {episode}\n"
        f"Type: {file_type}\n"
        f"Storage Message ID: {message_id}"
    )


async def link_command(update, context):
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text(
            "❌ Admin only."
        )
        return

    if len(context.args) != 3:
        await update.message.reply_text(
            "❌ Format:\n\n"
            "/link STORY_ID EPISODE TYPE\n\n"
            "Example:\n"
            "/link 1 2450 audio"
        )
        return

    try:
        story_id = int(context.args[0])
        episode = int(context.args[1])
        file_type = normalize_file_type(context.args[2])

    except Exception:
        await update.message.reply_text(
            "❌ Invalid values."
        )
        return

    bot_username = context.bot.username
    payload = f"f_{story_id}_{episode}_{file_type}"
    link = (
        f"https://t.me/{bot_username}"
        f"?start={payload}"
    )

    await update.message.reply_text(
        "🔗 <b>File Link</b>\n\n"
        f"{link}",
        parse_mode=ParseMode.HTML,
    )


async def postbuttons_command(update, context):
    """
    Create one or many custom buttons in the Main Channel.

    Format:
    /postbuttons
    BUTTON NAME|STORY_ID|START_EP|END_EP|TYPE
    BUTTON NAME|STORY_ID|START_EP|END_EP|TYPE

    START_EP == END_EP creates a one-episode button.
    """
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text(
            "❌ Admin only."
        )
        return

    raw = update.message.text or ""
    lines = raw.splitlines()[1:]

    if not lines:
        await update.message.reply_text(
            "❌ Format:\n\n"
            "/postbuttons\n"
            "EP ❄️ 1 to 50|1|1|50|video\n"
            "EP ❄️ 51 to 100|1|51|100|video\n\n"
            "Single episode example:\n"
            "Class 01|1|1|1|video"
        )
        return

    buttons = []

    for line in lines:
        line = line.strip()

        if not line:
            continue

        parts = [x.strip() for x in line.split("|")]

        if len(parts) != 5:
            await update.message.reply_text(
                "❌ Invalid line:\n\n"
                f"{line}\n\n"
                "Required:\n"
                "BUTTON NAME|STORY_ID|START_EP|END_EP|TYPE"
            )
            return

        button_name = parts[0]

        if not button_name:
            await update.message.reply_text(
                "❌ Button name cannot be empty."
            )
            return

        try:
            story_id = int(parts[1])
            start_ep = int(parts[2])
            end_ep = int(parts[3])
            file_type = normalize_file_type(parts[4])

            if start_ep < 1 or end_ep < start_ep:
                raise ValueError

            # Telegram deep-link payload must stay short.
            payload = (
                f"r_{story_id}_{start_ep}_"
                f"{end_ep}_{file_type}"
            )

            if len(payload.encode("utf-8")) > 64:
                raise ValueError(
                    "payload too long"
                )

        except Exception:
            await update.message.reply_text(
                "❌ Invalid values:\n\n"
                f"{line}\n\n"
                "Example:\n"
                "EP ❄️ 1 to 50|1|1|50|video"
            )
            return

        buttons.append(
            InlineKeyboardButton(
                button_name,
                url=(
                    f"https://t.me/{context.bot.username}"
                    f"?start={payload}"
                ),
            )
        )

    if not buttons:
        await update.message.reply_text(
            "❌ No valid buttons found."
        )
        return

    keyboard = InlineKeyboardMarkup(
        [[button] for button in buttons]
    )

    try:
        sent = await context.bot.send_message(
            chat_id=MAIN_CHANNEL_ID,
            text="👇 <b>Episodes</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

        await update.message.reply_text(
            "✅ Buttons posted successfully.\n\n"
            f"Main Channel Message ID: {sent.message_id}\n"
            f"Buttons: {len(buttons)}"
        )

    except Exception:
        logger.exception(
            "Could not post buttons to Main Channel."
        )

        await update.message.reply_text(
            "❌ Main Channel mein buttons post nahi ho paaye.\n\n"
            "Check karein ki bot Main Channel mein admin hai "
            "aur uske paas Post Messages permission hai."
        )


async def stats_command(update, context):
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text(
            "❌ Admin only."
        )
        return

    total_files = supabase_count("files")
    audio_files = supabase_count(
        "files", {"file_type": "eq.audio"}
    )
    video_files = supabase_count(
        "files", {"file_type": "eq.video"}
    )
    pdf_files = supabase_count(
        "files", {"file_type": "eq.pdf"}
    )
    image_files = supabase_count(
        "files", {"file_type": "eq.image"}
    )
    document_files = supabase_count(
        "files", {"file_type": "eq.document"}
    )
    pending = supabase_count("requests")

    text = (
        "📊 <b>Emperor FM File Bot Stats</b>\n\n"
        f"📁 Total files: {total_files}\n"
        f"🎧 Audio: {audio_files}\n"
        f"🎬 Video: {video_files}\n"
        f"📄 PDF: {pdf_files}\n"
        f"🖼 Image: {image_files}\n"
        f"📎 Document: {document_files}\n\n"
        f"⏳ Pending deletion: {pending}"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


async def admin_help(update, context):
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text(
            "❌ Admin only."
        )
        return

    await update.message.reply_text(
        "🛠 <b>Admin Commands</b>\n\n"
        "/stats - Bot statistics\n\n"
        "/link STORY EP TYPE - Generate file link\n\n"
        "/register STORY EP TYPE MESSAGE_ID - Register file\n\n"
        "/postbuttons - Main Channel custom buttons\n\n"
        "<b>/postbuttons format:</b>\n"
        "BUTTON NAME|STORY_ID|START_EP|END_EP|TYPE\n\n"
        "<b>Range:</b>\n"
        "EP ❄️ 1 to 50|1|1|50|video\n\n"
        "<b>Single:</b>\n"
        "Class 01|1|1|1|video\n\n"
        "Audio files named like:\n"
        "1x2450.m4a\n"
        "5x1339.m4a\n"
        "automatically register ho jayengi.",
        parse_mode=ParseMode.HTML,
    )


async def error_handler(update, context):
    logger.error(
        "Unhandled bot error: %s",
        context.error,
        exc_info=context.error,
    )


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Health server listening on 0.0.0.0:%s", port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not configured."
        )

    if not STORAGE_CHANNEL_ID:
        raise RuntimeError(
            "STORAGE_CHANNEL_ID is not configured."
        )

    if not MAIN_CHANNEL_ID:
        raise RuntimeError(
            "MAIN_CHANNEL_ID/CHANNEL_1_ID is not configured."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            try_again,
            pattern=r"^try_again$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_auto_range,
            pattern=r"^cancel_auto_range$",
        )
    )

    application.add_handler(
        CommandHandler(
            "register",
            register_private_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "link",
            link_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "postbuttons",
            postbuttons_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_help,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE,
            private_user_message_guard,
            block=True,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.ALL,
            storage_channel_post,
            block=False,
        )
    )

    application.add_error_handler(
        error_handler
    )

    start_health_server()

    logger.info(
        "Emperor FM File Bot starting..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
