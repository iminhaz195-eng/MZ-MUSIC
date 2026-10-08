import os
import asyncio
import logging
import shutil
import sqlite3
import subprocess
import tempfile
import time

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ==================== CONFIG ====================
BOT_TOKEN = "8745155614:AAEtfncSmlS-93zxP49cAU_QrzPEig24TzI"
DEVELOPER = "MZ MINHAZ"
WELCOME_GIF = "https://media.giphy.com/media/JIX9t2j0ZTN9S/giphy.gif"  # nijer GIF boshao
ADMIN_IDS = {8255204869}
DEFAULT_CHANNEL = "@mz_creations_official"
DEFAULT_CHANNEL_TITLE = "MZ Creations Official"
DB_PATH = "bot.db"
DOWNLOAD_DIR = "downloads"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB Telegram Bot API limit
# ================================================

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)


# ==================== DATABASE ====================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    c = db()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            joined_at INTEGER,
            banned INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS channels(
            username TEXT PRIMARY KEY,
            title TEXT,
            added_at INTEGER
        );
        """
    )
    row = c.execute("SELECT COUNT(*) AS n FROM channels").fetchone()
    if row["n"] == 0:
        c.execute(
            "INSERT INTO channels(username, title, added_at) VALUES(?,?,?)",
            (DEFAULT_CHANNEL, DEFAULT_CHANNEL_TITLE, int(time.time())),
        )
    c.commit()
    c.close()


def upsert_user(user):
    c = db()
    c.execute(
        """INSERT INTO users(user_id, first_name, username, joined_at)
           VALUES(?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             first_name=excluded.first_name,
             username=excluded.username""",
        (user.id, user.first_name or "", user.username or "", int(time.time())),
    )
    c.commit()
    c.close()


def is_banned(uid: int) -> bool:
    c = db()
    r = c.execute("SELECT banned FROM users WHERE user_id=?", (uid,)).fetchone()
    c.close()
    return bool(r and r["banned"])


def all_user_ids():
    c = db()
    rows = c.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
    c.close()
    return [r["user_id"] for r in rows]


def get_channels():
    c = db()
    rows = c.execute("SELECT * FROM channels ORDER BY added_at ASC").fetchall()
    c.close()
    return rows


def add_channel(username: str, title: str = ""):
    c = db()
    c.execute(
        "INSERT OR REPLACE INTO channels(username, title, added_at) VALUES(?,?,?)",
        (username, title, int(time.time())),
    )
    c.commit()
    c.close()


def remove_channel(username: str):
    c = db()
    c.execute("DELETE FROM channels WHERE username=?", (username,))
    c.commit()
    c.close()


def stats_counts():
    c = db()
    total = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    banned = c.execute("SELECT COUNT(*) AS n FROM users WHERE banned=1").fetchone()["n"]
    c.close()
    return total, banned


# ==================== FORCE SUB ====================
async def is_user_joined(bot, user_id: int) -> bool:
    for ch in get_channels():
        try:
            m = await bot.get_chat_member(chat_id=ch["username"], user_id=user_id)
            status = m.status
            if hasattr(status, "value"):
                status = status.value
            if status not in ("member", "administrator", "creator"):
                return False
        except Exception as e:
            log.warning(f"join-check fail {ch['username']}: {e}")
            return False
    return True


def join_kb() -> InlineKeyboardMarkup:
    rows = []
    for ch in get_channels():
        uname = ch["username"]
        title = ch["title"] or uname
        link = f"https://t.me/{uname.lstrip('@')}"
        rows.append([InlineKeyboardButton(f"📢 {title}", url=link)])
    rows.append([InlineKeyboardButton("✅ Join Korechi", callback_data="check_join")])
    return InlineKeyboardMarkup(rows)


# ==================== WELCOME ====================
def welcome_caption(first_name: str) -> str:
    return (
        f"👋 Assalamu Alaikum, <b>{first_name}</b>!\n\n"
        f"🎶 Ami ekta music bot. Jekono ganer nam pathan — audio ba video "
        f"direct pathiye dibo.\n\n"
        f"🎧 <b>Voice</b> button → gan voice note hisebe\n"
        f"🎬 <b>Video</b> button → gan direct video hisebe\n\n"
        f"❌ Kono link pathabo na, shudhu file.\n\n"
        f"👨‍💻 <b>Developer:</b> {DEVELOPER}"
    )


def welcome_kb(user_id: int):
    rows = []
    if user_id in ADMIN_IDS:
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows) if rows else None


async def send_welcome(chat, user):
    try:
        await chat.send_animation(
            animation=WELCOME_GIF,
            caption=welcome_caption(user.first_name),
            parse_mode="HTML",
            reply_markup=welcome_kb(user.id),
        )
    except Exception as e:
        log.warning(f"GIF fallback: {e}")
        await chat.send_message(
            welcome_caption(user.first_name),
            parse_mode="HTML",
            reply_markup=welcome_kb(user.id),
        )


# ==================== yt-dlp ====================
def yt_search(query: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            if info and info.get("entries"):
                return info["entries"][0]
    except Exception as e:
        log.error(f"search err: {e}")
    return None


def yt_to_voice(url: str, outdir: str):
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        src = ydl.prepare_filename(info)
    voice_path = os.path.join(outdir, "voice.ogg")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", src,
            "-c:a", "libopus", "-b:a", "96k",
            "-ar", "48000", "-ac", "1",
            voice_path,
        ],
        check=True,
        capture_output=True,
    )
    return voice_path, info


def yt_to_video(url: str, outdir: str):
    opts = {
        "format": "best[ext=mp4][filesize<50000000]/best[ext=mp4]/best",
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info), info


# ==================== HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)
    context.user_data.clear()

    if is_banned(user.id):
        await update.message.reply_text("🚫 Apni ban hoye achen.")
        return

    if not await is_user_joined(context.bot, user.id):
        await update.message.reply_text(
            "🔒 Bot use korte hole prothome amader channel e join korun.",
            reply_markup=join_kb(),
        )
        return

    await send_welcome(update.effective_chat, user)


async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    if await is_user_joined(context.bot, user.id):
        await q.answer("✅ Verified!")
        try:
            await q.message.delete()
        except Exception:
            pass
        await send_welcome(q.message.chat, user)
    else:
        await q.answer(
            "❌ Ekhono join korun nai. Join kore abar chapun.",
            show_alert=True,
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)

    if is_banned(user.id):
        await update.message.reply_text("🚫 Apni ban hoye achen.")
        return

    if not await is_user_joined(context.bot, user.id):
        await update.message.reply_text(
            "🔒 Channel e join korun.", reply_markup=join_kb()
        )
        return

    # admin state machine
    if user.id in ADMIN_IDS and context.user_data.get("admin_state"):
        await handle_admin_state(update, context)
        return

    query = (update.message.text or "").strip()
    if not query:
        return

    status = await update.message.reply_text("🔍 Khujchi...")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, yt_search, query)

    if not result:
        await status.edit_text("❌ Kono gan khuje pai nai. Nam thik kore likhun.")
        return

    context.user_data["last_search"] = result
    title = result.get("title", "Unknown")
    dur = int(result.get("duration") or 0)
    uploader = result.get("uploader", "")
    thumb = result.get("thumbnail")
    m, s = divmod(dur, 60)

    caption = (
        f"🎵 <b>{title}</b>\n"
        f"⏱ {m}:{s:02d}\n"
        f"📺 {uploader}\n\n"
        f"Ki vabe pathabo?"
    )
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🎧 Voice", callback_data="dl_audio"),
            InlineKeyboardButton("🎬 Video", callback_data="dl_video"),
        ]]
    )

    try:
        await status.delete()
    except Exception:
        pass

    if thumb:
        try:
            await update.message.reply_photo(
                photo=thumb, caption=caption,
                reply_markup=kb, parse_mode="HTML",
            )
            return
        except Exception:
            pass

    await update.message.reply_text(
        caption, reply_markup=kb, parse_mode="HTML"
    )


async def dl_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ Voice banacchi...")
    r = context.user_data.get("last_search")
    if not r:
        await q.message.reply_text("❌ Search info nai. Abar search korun.")
        return
    url = r.get("webpage_url") or r.get("url")
    if not url:
        await q.message.reply_text("❌ URL pai nai.")
        return

    status = await q.message.reply_text("⬇️ Download + convert hocche...")
    outdir = tempfile.mkdtemp(dir=DOWNLOAD_DIR)

    try:
        loop = asyncio.get_event_loop()
        path, info = await loop.run_in_executor(None, yt_to_voice, url, outdir)
        if not os.path.exists(path):
            await status.edit_text("❌ Conversion fail.")
            return
        size = os.path.getsize(path)
        if size > MAX_FILE_SIZE:
            await status.edit_text(
                f"❌ {size // (1024 * 1024)}MB — 50MB limit er beshi."
            )
            return

        await status.edit_text("📤 Pathacchi...")
        with open(path, "rb") as f:
            await q.message.reply_voice(
                voice=f,
                caption=f"🎧 {info.get('title')}\n\n👨‍💻 {DEVELOPER}",
                duration=int(info.get("duration") or 0),
                parse_mode="HTML",
            )
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        log.error(f"audio err: {e}")
        await status.edit_text(f"❌ Error: {e}")
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


async def dl_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ Download shuru...")
    r = context.user_data.get("last_search")
    if not r:
        await q.message.reply_text("❌ Search info nai. Abar search korun.")
        return
    url = r.get("webpage_url") or r.get("url")
    if not url:
        await q.message.reply_text("❌ URL pai nai.")
        return

    status = await q.message.reply_text("⬇️ Video download hocche...")
    outdir = tempfile.mkdtemp(dir=DOWNLOAD_DIR)

    try:
        loop = asyncio.get_event_loop()
        path, info = await loop.run_in_executor(None, yt_to_video, url, outdir)
        if not os.path.exists(path):
            await status.edit_text("❌ Download fail.")
            return
        size = os.path.getsize(path)
        if size > MAX_FILE_SIZE:
            await status.edit_text(
                f"❌ {size // (1024 * 1024)}MB — 50MB limit er beshi. Voice try korun."
            )
            return

        await status.edit_text("📤 Pathacchi...")
        with open(path, "rb") as f:
            await q.message.reply_video(
                video=f,
                caption=f"🎬 {info.get('title')}\n\n👨‍💻 {DEVELOPER}",
                duration=int(info.get("duration") or 0),
                supports_streaming=True,
                parse_mode="HTML",
            )
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        log.error(f"video err: {e}")
        await status.edit_text(f"❌ Error: {e}")
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


# ==================== ADMIN PANEL ====================
def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistics", callback_data="adm_stats")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast")],
        [InlineKeyboardButton("👥 Users List", callback_data="adm_users")],
        [InlineKeyboardButton("📡 Manage Channels", callback_data="adm_channels")],
        [InlineKeyboardButton("🚫 Ban / Unban", callback_data="adm_ban")],
        [InlineKeyboardButton("❌ Close", callback_data="adm_close")],
    ])


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("❌ Not admin.", show_alert=True)
        return
    await q.answer()
    await q.message.reply_text(
        "🛠 <b>Admin Panel</b>\n\nButton choose korun:",
        reply_markup=admin_kb(),
        parse_mode="HTML",
    )


async def adm_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        return
    await q.answer()
    total, banned = stats_counts()
    chans = get_channels()
    await q.message.reply_text(
        f"📊 <b>Statistics</b>\n\n"
        f"👥 Total Users: <b>{total}</b>\n"
        f"🚫 Banned: <b>{banned}</b>\n"
        f"📡 Channels: <b>{len(chans)}</b>",
        parse_mode="HTML",
    )


async def adm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        return
    await q.answer()
    context.user_data["admin_state"] = "broadcast"
    await q.message.reply_text(
        "📢 <b>Broadcast mode ON</b>\n\n"
        "Ekhon je message pathaben seta sob user ke jabe.\n"
        "Cancel korte <b>/start</b> chapun.",
        parse_mode="HTML",
    )


async def adm_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        return
    await q.answer()
    c = db()
    rows = c.execute(
        "SELECT user_id, first_name, username, banned FROM users "
        "ORDER BY joined_at DESC LIMIT 30"
    ).fetchall()
    c.close()
    if not rows:
        await q.message.reply_text("Kono user nai.")
        return
    lines = ["👥 <b>Recent Users (30)</b>\n"]
    for r in rows:
        mark = "🚫" if r["banned"] else "✅"
        uname = f"@{r['username']}" if r["username"] else "—"
        lines.append(f"{mark} <code>{r['user_id']}</code> — {r['first_name']} ({uname})")
    await q.message.reply_text("\n".join(lines), parse_mode="HTML")


async def adm_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        return
    await q.answer()
    chans = get_channels()
    lines = ["📡 <b>Force-Sub Channels</b>\n"]
    rows = []
    for i, ch in enumerate(chans, 1):
        lines.append(f"{i}. <code>{ch['username']}</code> — {ch['title'] or '—'}")
        rows.append([InlineKeyboardButton(
            f"🗑 Remove {ch['username']}", callback_data=f"adm_ch_rm:{ch['username']}"
        )])
    rows.append([InlineKeyboardButton("➕ Add Channel", callback_data="adm_ch_add")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await q.message.reply_text(
        "\n".join(lines) if len(lines) > 1 else "Kono channel nai.",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
    )


async def adm_ch_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        return
    await q.answer()
    context.user_data["admin_state"] = "add_channel"
    await q.message.reply_text(
        "➕ Channel username pathan (e.g. <code>@mychannel</code>).\n"
        "Cancel: <b>/start</b>",
        parse_mode="HTML",
    )


async def adm_ch_rm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        return
    username = q.data.split(":", 1)[1]
    remove_channel(username)
    await q.answer(f"Removed {username}")
    await q.message.reply_text(f"✅ Removed <code>{username}</code>", parse_mode="HTML")


async def adm_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        return
    await q.answer()
    context.user_data["admin_state"] = "ban_user"
    await q.message.reply_text(
        "🚫 User er numeric ID pathan (ban/unban toggle hobe).\n"
        "Cancel: <b>/start</b>",
        parse_mode="HTML",
    )


async def adm_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Closed")
    try:
        await q.message.delete()
    except Exception:
        pass


async def admin_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = update.callback_query.data
    if d == "admin_panel":
        return await admin_panel(update, context)
    if d == "adm_stats":
        return await adm_stats(update, context)
    if d == "adm_broadcast":
        return await adm_broadcast(update, context)
    if d == "adm_users":
        return await adm_users(update, context)
    if d == "adm_channels":
        return await adm_channels(update, context)
    if d == "adm_ch_add":
        return await adm_ch_add(update, context)
    if d == "adm_ban":
        return await adm_ban(update, context)
    if d == "adm_close":
        return await adm_close(update, context)
    if d.startswith("adm_ch_rm:"):
        return await adm_ch_rm(update, context)


# ==================== ADMIN STATE MACHINE ====================
async def handle_admin_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("admin_state")

    if state == "broadcast":
        context.user_data.pop("admin_state", None)
        msg = update.message
        status = await msg.reply_text("📤 Broadcasting...")
        ids = all_user_ids()
        sent, fail = 0, 0
        for uid in ids:
            try:
                await context.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                )
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                fail += 1
        await status.edit_text(f"✅ Done.\n\nSent: {sent}\nFailed: {fail}")
        return

    if state == "add_channel":
        context.user_data.pop("admin_state", None)
        text = (update.message.text or "").strip()
        if not text.startswith("@") or len(text) < 3:
            await update.message.reply_text("❌ Format: @channelusername")
            return
        try:
            chat = await context.bot.get_chat(text)
            add_channel(text, chat.title or "")
            await update.message.reply_text(
                f"✅ Added <code>{text}</code> — {chat.title}",
                parse_mode="HTML",
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Fail: {e}")
        return

    if state == "ban_user":
        context.user_data.pop("admin_state", None)
        text = (update.message.text or "").strip()
        if not text.isdigit():
            await update.message.reply_text("❌ Numeric user ID din.")
            return
        uid = int(text)
        c = db()
        r = c.execute("SELECT banned FROM users WHERE user_id=?", (uid,)).fetchone()
        if not r:
            c.close()
            await update.message.reply_text("❌ User DB te nai.")
            return
        new_state = 0 if r["banned"] else 1
        c.execute("UPDATE users SET banned=? WHERE user_id=?", (new_state, uid))
        c.commit()
        c.close()
        await update.message.reply_text(
            f"{'🚫 Banned' if new_state else '✅ Unbanned'}: <code>{uid}</code>",
            parse_mode="HTML",
        )
        return


# ==================== MAIN ====================
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(check_join, pattern=r"^check_join$"))
    app.add_handler(CallbackQueryHandler(dl_audio, pattern=r"^dl_audio$"))
    app.add_handler(CallbackQueryHandler(dl_video, pattern=r"^dl_video$"))
    app.add_handler(
        CallbackQueryHandler(admin_router, pattern=r"^(admin_panel|adm_.+)$")
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("🎵 Music bot chalu...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()