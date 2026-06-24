import asyncio
import random
import string
import logging
import os
import json
import sqlite3
import time
import httpx
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, PreCheckoutQueryHandler, filters, ContextTypes
)

BOT_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID        = 5845336275
PREMIUM_STARS   = 50
PREMIUM_ORIGINAL_STARS = 100
FREE_REQUESTS   = 10
DB_PATH         = "bot/users.db"
CHANNELS_PATH   = "bot/channels.json"
DEFAULT_BATCH   = 5
REPORT_COOLDOWN = 2 * 3600   # 2 часа в секундах
REPORTS_PER_PAGE = 3

# Каналы с обязательной подпиской, добавленные по умолчанию при первом запуске
DEFAULT_CHANNELS = [
    {"username": "username_searcher", "title": None, "added_at": None},
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def init_db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            used        INTEGER DEFAULT 0,
            is_premium  INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            user_name   TEXT,
            text        TEXT NOT NULL,
            status      TEXT DEFAULT 'pending',
            admin_reply TEXT,
            created_at  REAL NOT NULL
        );
    """)
    con.commit()
    con.close()


# ── Каналы обязательной подписки (JSON) ────────────────────────────────────────

def load_channels() -> list[dict]:
    if not os.path.exists(CHANNELS_PATH):
        save_channels(DEFAULT_CHANNELS)
        return [c.copy() for c in DEFAULT_CHANNELS]
    try:
        with open(CHANNELS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_channels(channels: list[dict]):
    folder = os.path.dirname(CHANNELS_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(CHANNELS_PATH, "w", encoding="utf-8") as f:
        json.dump(channels, f, ensure_ascii=False, indent=2)


def add_channel(username: str, title: str | None = None) -> bool:
    username = username.lstrip("@").strip()
    channels = load_channels()
    if any(c["username"].lower() == username.lower() for c in channels):
        return False
    channels.append({"username": username, "title": title, "added_at": time.time()})
    save_channels(channels)
    return True


def remove_channel(username: str) -> bool:
    username = username.lstrip("@").strip()
    channels = load_channels()
    new_channels = [c for c in channels if c["username"].lower() != username.lower()]
    if len(new_channels) == len(channels):
        return False
    save_channels(new_channels)
    return True


# ── Users ─────────────────────────────────────────────────────────────────────

def get_user(user_id: int) -> dict:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT used, is_premium FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    con.close()
    if row is None:
        return {"used": 0, "premium": False}
    return {"used": row[0], "premium": bool(row[1])}


def inc_request(user_id: int):
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO users (user_id, used, is_premium) VALUES (?, 1, 0)
        ON CONFLICT(user_id) DO UPDATE SET used = used + 1
    """, (user_id,))
    con.commit()
    con.close()


def set_premium(user_id: int):
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO users (user_id, used, is_premium) VALUES (?, 0, 1)
        ON CONFLICT(user_id) DO UPDATE SET is_premium = 1
    """, (user_id,))
    con.commit()
    con.close()


# ── Reports ───────────────────────────────────────────────────────────────────

def get_last_report_time(user_id: int) -> float | None:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT MAX(created_at) FROM reports WHERE user_id = ?", (user_id,)
    ).fetchone()
    con.close()
    return row[0] if row and row[0] else None


def create_report(user_id: int, user_name: str | None, text: str) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "INSERT INTO reports (user_id, user_name, text, created_at) VALUES (?, ?, ?, ?)",
        (user_id, user_name, text, time.time())
    )
    rid = cur.lastrowid
    con.commit()
    con.close()
    return rid


def get_reports(status: str | None = None, limit: int = REPORTS_PER_PAGE, offset: int = 0) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    if status:
        rows = con.execute(
            "SELECT id, user_id, user_name, text, status, admin_reply, created_at "
            "FROM reports WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (status, limit, offset)
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, user_id, user_name, text, status, admin_reply, created_at "
            "FROM reports ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
    con.close()
    return [
        {"id": r[0], "user_id": r[1], "user_name": r[2], "text": r[3],
         "status": r[4], "admin_reply": r[5], "created_at": r[6]}
        for r in rows
    ]


def count_reports(status: str | None = None) -> int:
    con = sqlite3.connect(DB_PATH)
    if status:
        row = con.execute("SELECT COUNT(*) FROM reports WHERE status = ?", (status,)).fetchone()
    else:
        row = con.execute("SELECT COUNT(*) FROM reports").fetchone()
    con.close()
    return row[0] if row else 0


def get_report(report_id: int) -> dict | None:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT id, user_id, user_name, text, status, admin_reply, created_at "
        "FROM reports WHERE id = ?", (report_id,)
    ).fetchone()
    con.close()
    if not row:
        return None
    return {"id": row[0], "user_id": row[1], "user_name": row[2], "text": row[3],
            "status": row[4], "admin_reply": row[5], "created_at": row[6]}


def update_report(report_id: int, status: str, admin_reply: str | None = None):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE reports SET status = ?, admin_reply = ? WHERE id = ?",
        (status, admin_reply, report_id)
    )
    con.commit()
    con.close()


# ══════════════════════════════════════════════════════════════════════════════
# USERNAME GENERATORS
# ══════════════════════════════════════════════════════════════════════════════

VOWELS     = "aeiou"
CONSONANTS = "bcdfghjklmnpqrstvwxyz"
LETTERS    = string.ascii_lowercase

TYPE_META = {
    "clean":    ("🔤", "Чистые",     "только буквы a–z"),
    "pretty":   ("✦",  "Красивые",   "гласн/согл чередование"),
    "syllable": ("🎵", "Слоговые",   "легко произносимые"),
    "unique":   ("❋",  "Уникальные", "без повторов букв"),
    "mixed":    ("🎲", "Стандарт",   "буквы + цифры"),
}


def gen_clean(length: int) -> str:
    return random.choice(CONSONANTS) + "".join(random.choices(LETTERS, k=length - 1))

def gen_pretty(length: int) -> str:
    return "".join(random.choice(CONSONANTS if i % 2 == 0 else VOWELS) for i in range(length))

def gen_syllable(length: int) -> str:
    out = ""
    while len(out) < length:
        r = length - len(out)
        if r >= 3 and random.random() > 0.4:
            out += random.choice(CONSONANTS) + random.choice(VOWELS) + random.choice(CONSONANTS)
        elif r >= 2:
            out += random.choice(CONSONANTS) + random.choice(VOWELS)
        else:
            out += random.choice(LETTERS)
    return out[:length]

def gen_unique(length: int) -> str:
    pool = list(LETTERS)
    random.shuffle(pool)
    first = next(c for c in pool if c in CONSONANTS)
    rest = [c for c in pool if c != first][:length - 1]
    return first + "".join(rest)

def gen_mixed(length: int) -> str:
    return random.choice(LETTERS) + "".join(random.choices(LETTERS + string.digits, k=length - 1))

GENERATORS = {
    "clean": gen_clean, "pretty": gen_pretty, "syllable": gen_syllable,
    "unique": gen_unique, "mixed": gen_mixed,
}


# ══════════════════════════════════════════════════════════════════════════════
# USERNAME AVAILABILITY — двойная проверка (Bot API + t.me)
# ══════════════════════════════════════════════════════════════════════════════

async def is_taken_via_api(bot: telegram.Bot, username: str) -> bool | None:
    try:
        await bot.get_chat(f"@{username}")
        return True
    except telegram.error.BadRequest as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid" in msg or "chat not found" in msg:
            return None
        return True
    except telegram.error.RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return None
    except Exception:
        return None


async def is_taken_via_tme(client: httpx.AsyncClient, username: str) -> bool:
    try:
        r = await client.get(
            f"https://t.me/{username}", follow_redirects=True, timeout=7,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"}
        )
        html = r.text.lower()
        markers = ['tgme_page_title', '<meta property="og:title"',
                   'tgme_page_description', f'"@{username.lower()}"']
        return any(m.lower() in html for m in markers)
    except Exception:
        return True


async def check_username(bot: telegram.Bot, client: httpx.AsyncClient, username: str) -> bool:
    api = await is_taken_via_api(bot, username)
    if api is True:
        return False
    return not (await is_taken_via_tme(client, username))


async def find_usernames(bot: telegram.Bot, gen_fn, length: int, count: int) -> list[str]:
    found = []
    generated = [gen_fn(length) for _ in range(count * 20)]
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        for i in range(0, len(generated), 6):
            chunk = generated[i:i + 6]
            results = await asyncio.gather(*[check_username(bot, client, u) for u in chunk])
            for username, free in zip(chunk, results):
                if free:
                    found.append(username)
                if len(found) >= count:
                    return found
    return found


# ══════════════════════════════════════════════════════════════════════════════
# ОБЯЗАТЕЛЬНАЯ ПОДПИСКА
# ══════════════════════════════════════════════════════════════════════════════

async def get_unsubscribed_channels(bot: telegram.Bot, user_id: int) -> list[dict]:
    """Возвращает список каналов из обязательного списка, на которые user_id не подписан.
    Если бот не может проверить канал (не добавлен туда админом / канал недоступен),
    такой канал пропускается, чтобы не блокировать всех пользователей из-за ошибки конфигурации."""
    channels = load_channels()
    if not channels:
        return []
    unsubscribed = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=f"@{ch['username']}", user_id=user_id)
            if member.status in ("left", "kicked"):
                unsubscribed.append(ch)
        except Exception:
            continue
    return unsubscribed


async def ensure_subscribed(bot: telegram.Bot, user_id: int) -> list[dict] | None:
    """None — доступ разрешён (админ или подписка в порядке). Иначе — список каналов, на которые нужно подписаться."""
    if user_id == ADMIN_ID:
        return None
    unsub = await get_unsubscribed_channels(bot, user_id)
    return unsub if unsub else None


def subscribe_gate_text(channels: list[dict]) -> str:
    lines = [
        "⭕️ *Доступ ограничен* ⭕️",
        "━━━━━━━━━━━━━━━━━━━━\n",
        "_Чтобы использовать бота, подпишись на канал(ы):_\n",
    ]
    for c in channels:
        label = c.get("title") or f"@{c['username']}"
        lines.append(f"✧ {label}")
    lines.append("\n_После подписки нажми кнопку ниже_")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def subscribe_gate_keyboard(channels: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"📢 {c.get('title') or c['username']}",
                               url=f"https://t.me/{c['username']}")]
        for c in channels
    ]
    rows.append([InlineKeyboardButton("✅ Я подписался", callback_data="check_sub")])
    return InlineKeyboardMarkup(rows)


# ══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════════════════════

def main_keyboard(premium: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("✧ 5 символов", callback_data="len_5"),
            InlineKeyboardButton("✧ 6 символов", callback_data="len_6"),
        ],
        [
            InlineKeyboardButton("✧ 7 символов", callback_data="len_7"),
            InlineKeyboardButton("✧ 8 символов", callback_data="len_8"),
        ],
        [
            InlineKeyboardButton("👤 Профиль", callback_data="profile"),
            InlineKeyboardButton("🆘 Репорт", callback_data="report_start"),
        ],
    ]
    if not premium:
        rows.append([InlineKeyboardButton("⭐ Купить Premium", callback_data="buy_premium")])
    return InlineKeyboardMarkup(rows)


def type_keyboard(length: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        f"{icon} {label} — {desc}", callback_data=f"type_{length}_{key}"
    )] for key, (icon, label, desc) in TYPE_META.items()]
    rows.append([InlineKeyboardButton("‹ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def batch_keyboard(length: int, type_key: str) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(str(s), callback_data=f"batch_{length}_{type_key}_{s}")
           for s in [3, 5, 10, 15, 20]]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("‹ Назад", callback_data=f"len_{length}")]])


def result_keyboard(length: int, type_key: str, batch: int, batch_num: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✧ Ещё партия #{batch_num + 1} ✧",
                              callback_data=f"search_{length}_{type_key}_{batch}_{batch_num + 1}")],
        [InlineKeyboardButton("‹ Тип", callback_data=f"len_{length}"),
         InlineKeyboardButton("⌂ Меню", callback_data="back")],
    ])


# ── Admin keyboards ───────────────────────────────────────────────────────────

def admin_report_list_keyboard(page: int, total: int, filter_status: str) -> InlineKeyboardMarkup:
    pages = max(1, (total + REPORTS_PER_PAGE - 1) // REPORTS_PER_PAGE)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹", callback_data=f"rep_page_{page - 1}_{filter_status}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("›", callback_data=f"rep_page_{page + 1}_{filter_status}"))

    filters_row = []
    for s, label in [("pending", "🕐 Новые"), ("approved", "✅ Одобренные"),
                     ("declined", "❌ Отклонённые"), ("all", "📋 Все")]:
        mark = "·" if s == filter_status else ""
        filters_row.append(InlineKeyboardButton(f"{mark}{label}", callback_data=f"rep_filter_{s}"))

    return InlineKeyboardMarkup([filters_row[:2], filters_row[2:], nav] if nav else [filters_row[:2], filters_row[2:]])


def admin_single_report_keyboard(report_id: int, page: int, filter_status: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"rep_approve_{report_id}_{page}_{filter_status}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"rep_decline_{report_id}_{page}_{filter_status}"),
        ],
        [InlineKeyboardButton("💬 Ответить", callback_data=f"rep_reply_{report_id}_{page}_{filter_status}")],
        [InlineKeyboardButton("‹ К списку", callback_data=f"rep_page_{page}_{filter_status}")],
    ])


# ── Admin: обязательные каналы (/ad) ────────────────────────────────────────────

def ad_main_text() -> str:
    return (
        "⭕️ *Обязательные каналы* ⭕️\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "_Каналы, на которые должен быть подписан пользователь,_\n"
        "_чтобы пользоваться ботом_\n\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


def ad_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Список каналов", callback_data="ad_list")],
        [InlineKeyboardButton("➕ Добавить канал", callback_data="ad_add")],
        [InlineKeyboardButton("➖ Удалить канал", callback_data="ad_remove_menu")],
        [InlineKeyboardButton("📊 Статистика", callback_data="ad_stats")],
    ])


def ad_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("‹ Назад", callback_data="ad_back")]])


def ad_remove_keyboard(channels: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🗑 {c.get('title') or c['username']}", callback_data=f"ad_del_{c['username']}")]
        for c in channels
    ]
    rows.append([InlineKeyboardButton("‹ Назад", callback_data="ad_back")])
    return InlineKeyboardMarkup(rows)


def render_channels_list(channels: list[dict]) -> str:
    if not channels:
        return (
            "📋 *Список каналов* 📋\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Список пуст — подписка не требуется_"
        )
    lines = [f"📋 *Список каналов* ({len(channels)}) 📋\n━━━━━━━━━━━━━━━━━━━━\n"]
    for c in channels:
        title = c.get("title") or c["username"]
        lines.append(f"✧ *{title}*\n  `@{c['username']}`")
    return "\n".join(lines)


async def render_channels_stats(bot: telegram.Bot) -> str:
    channels = load_channels()
    if not channels:
        return (
            "📊 *Статистика* 📊\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Каналов нет_"
        )
    lines = [f"📊 *Статистика по каналам* ({len(channels)}) 📊\n━━━━━━━━━━━━━━━━━━━━\n"]
    for c in channels:
        title = c.get("title") or c["username"]
        try:
            count = await bot.get_chat_member_count(f"@{c['username']}")
            count_str = f"{count} подписчиков"
        except Exception:
            count_str = "недоступно _(бот не админ канала?)_"
        lines.append(f"✧ *{title}*\n  `@{c['username']}` · {count_str}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def welcome_text(premium: bool, used: int) -> str:
    badge = ("⭐ *Premium* — безлимитный доступ" if premium
             else f"_Бесплатно: {max(0, FREE_REQUESTS - used)}/{FREE_REQUESTS} запросов_")
    return (
        "✧ *Username Finder* ✧\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{badge}\n\n"
        "_Выбери длину юзернейма:_\n\n"
        "✧ *5 симв* — крайне редкие\n"
        "✧ *6 симв* — редкие\n"
        "✧ *7 симв* — периодически\n"
        "✧ *8 симв* — чаще свободны"
    )


def fmt_time_ago(ts: float) -> str:
    diff = int(time.time() - ts)
    if diff < 60:
        return f"{diff}с назад"
    if diff < 3600:
        return f"{diff // 60}м назад"
    if diff < 86400:
        return f"{diff // 3600}ч назад"
    return f"{diff // 86400}д назад"


def fmt_cooldown(last_ts: float) -> str:
    remaining = int(REPORT_COOLDOWN - (time.time() - last_ts))
    h, m = divmod(remaining // 60, 60)
    s = remaining % 60
    if h:
        return f"{h}ч {m}м"
    return f"{m}м {s}с"


STATUS_EMOJI = {"pending": "🕐", "approved": "✅", "declined": "❌"}


def render_report_list(reports: list[dict], page: int, total: int, filter_status: str) -> str:
    fs_label = {"pending": "Новые", "approved": "Одобренные",
                "declined": "Отклонённые", "all": "Все"}[filter_status]
    if not reports:
        return (
            f"✧ *Репорты — {fs_label}* ✧\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Репортов нет_"
        )
    lines = [f"✧ *Репорты — {fs_label}* ({total} шт) ✧\n━━━━━━━━━━━━━━━━━━━━\n"]
    for r in reports:
        emoji = STATUS_EMOJI.get(r["status"], "❓")
        uname = f"@{r['user_name']}" if r["user_name"] else f"id{r['user_id']}"
        preview = r["text"][:60] + ("…" if len(r["text"]) > 60 else "")
        lines.append(
            f"{emoji} *#{r['id']}* · _{uname}_ · _{fmt_time_ago(r['created_at'])}_\n"
            f"`{preview}`\n"
        )
    return "\n".join(lines)


def render_single_report(r: dict) -> str:
    emoji = STATUS_EMOJI.get(r["status"], "❓")
    uname = f"@{r['user_name']}" if r["user_name"] else f"id{r['user_id']}"
    reply_block = f"\n\n*Ответ:*\n_{r['admin_reply']}_" if r["admin_reply"] else ""
    return (
        f"✧ *Репорт #{r['id']}* {emoji} ✧\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*От:* {uname} (`{r['user_id']}`)\n"
        f"*Время:* _{fmt_time_ago(r['created_at'])}_\n"
        f"*Статус:* {r['status']}\n\n"
        f"*Текст:*\n{r['text']}"
        f"{reply_block}\n\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


# ══════════════════════════════════════════════════════════════════════════════
# HANDLERS — USER
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    uid = update.effective_user.id
    unsub = await ensure_subscribed(context.bot, uid)
    if unsub:
        await update.message.reply_text(
            subscribe_gate_text(unsub), parse_mode="Markdown",
            reply_markup=subscribe_gate_keyboard(unsub)
        )
        return
    user = get_user(uid)
    await update.message.reply_text(
        welcome_text(user["premium"], user["used"]),
        parse_mode="Markdown",
        reply_markup=main_keyboard(user["premium"])
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текстовый ввод для режимов: report, admin_reply"""
    uid = update.effective_user.id
    mode = context.user_data.get("mode")

    # ── Пользователь вводит текст репорта ──
    if mode == "awaiting_report":
        text = update.message.text.strip()
        if len(text) < 5:
            await update.message.reply_text(
                "✧ _Описание слишком короткое. Напиши подробнее (мин. 5 символов):_",
                parse_mode="Markdown"
            )
            return
        if len(text) > 1000:
            await update.message.reply_text(
                "✧ _Слишком длинное описание (макс. 1000 символов). Сократи:_",
                parse_mode="Markdown"
            )
            return
        context.user_data["report_text"] = text
        context.user_data["mode"] = "confirm_report"
        preview = text[:200] + ("…" if len(text) > 200 else "")
        await update.message.reply_text(
            "✧ *Подтверди репорт* ✧\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"_{preview}_\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "_Отправить?_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Отправить", callback_data="report_confirm"),
                 InlineKeyboardButton("❌ Отмена", callback_data="report_cancel")],
            ])
        )
        return

    # ── Админ вводит ответ на репорт ──
    if mode == "admin_reply" and uid == ADMIN_ID:
        reply_text = update.message.text.strip()
        report_id = context.user_data.get("admin_reply_id")
        page = context.user_data.get("admin_reply_page", 0)
        fs = context.user_data.get("admin_reply_filter", "pending")
        context.user_data.clear()

        if not report_id:
            await update.message.reply_text("_Ошибка: репорт не найден_", parse_mode="Markdown")
            return

        r = get_report(report_id)
        if not r:
            await update.message.reply_text("_Репорт не найден_", parse_mode="Markdown")
            return

        update_report(report_id, "approved", reply_text)

        # Уведомить пользователя
        try:
            uname_str = f"@{r['user_name']}" if r["user_name"] else "пользователю"
            await context.bot.send_message(
                chat_id=r["user_id"],
                text=(
                    "✧ *Ответ на твой репорт* ✧\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"*Твой баг #{report_id}:*\n_{r['text'][:100]}_\n\n"
                    f"*Ответ команды:*\n{reply_text}\n\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "_Спасибо за помощь в улучшении бота!_"
                ),
                parse_mode="Markdown"
            )
            notify = "✅ _Ответ сохранён и отправлен пользователю_"
        except Exception:
            notify = "✅ _Ответ сохранён (не удалось уведомить пользователя)_"

        await update.message.reply_text(notify, parse_mode="Markdown")
        # Показать обновлённый список
        await _send_report_list(update.message, page, fs, send_new=True)
        return

    # ── Админ вводит юзернейм нового обязательного канала ──
    if mode == "ad_awaiting_channel" and uid == ADMIN_ID:
        raw = update.message.text.strip()
        username = (
            raw.replace("https://t.me/", "")
               .replace("http://t.me/", "")
               .replace("t.me/", "")
               .lstrip("@")
               .strip()
        )
        if not username or not all(c.isalnum() or c == "_" for c in username):
            await update.message.reply_text(
                "_Некорректный юзернейм. Отправь, например,_ `@mychannel` _или_ `mychannel`",
                parse_mode="Markdown"
            )
            return
        context.user_data.clear()
        title = None
        try:
            chat = await context.bot.get_chat(f"@{username}")
            title = chat.title or chat.username
        except Exception:
            pass
        added = add_channel(username, title)
        if added:
            text = (
                "✅ *Канал добавлен*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"_{title or '@' + username}_\n\n"
                "⚠️ _Убедись, что бот добавлен администратором в этот канал —_\n"
                "_иначе проверка подписки не будет работать_\n"
                "━━━━━━━━━━━━━━━━━━━━"
            )
        else:
            text = "_Этот канал уже в списке_"
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=ad_main_keyboard())
        return

    # ── Обычное сообщение — показать меню ──
    if uid != ADMIN_ID:
        unsub = await get_unsubscribed_channels(context.bot, uid)
        if unsub:
            context.user_data.clear()
            await update.message.reply_text(
                subscribe_gate_text(unsub), parse_mode="Markdown",
                reply_markup=subscribe_gate_keyboard(unsub)
            )
            return
    user = get_user(uid)
    context.user_data.clear()
    await update.message.reply_text(
        welcome_text(user["premium"], user["used"]),
        parse_mode="Markdown",
        reply_markup=main_keyboard(user["premium"])
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    user = get_user(uid)
    data = query.data

    if data == "noop":
        return

    # ══ ОБЯЗАТЕЛЬНАЯ ПОДПИСКА ══

    if data == "check_sub":
        unsub = await get_unsubscribed_channels(context.bot, uid)
        if unsub:
            await query.answer("Подписка не найдена ⛔", show_alert=True)
            await query.edit_message_text(
                subscribe_gate_text(unsub), parse_mode="Markdown",
                reply_markup=subscribe_gate_keyboard(unsub)
            )
        else:
            await query.edit_message_text(
                welcome_text(user["premium"], user["used"]),
                parse_mode="Markdown", reply_markup=main_keyboard(user["premium"])
            )
        return

    if uid != ADMIN_ID:
        unsub = await get_unsubscribed_channels(context.bot, uid)
        if unsub:
            await query.edit_message_text(
                subscribe_gate_text(unsub), parse_mode="Markdown",
                reply_markup=subscribe_gate_keyboard(unsub)
            )
            return

    # ══ MAIN MENU ══

    if data == "back":
        context.user_data.clear()
        await query.edit_message_text(
            welcome_text(user["premium"], user["used"]),
            parse_mode="Markdown",
            reply_markup=main_keyboard(user["premium"])
        )
        return

    if data == "profile":
        if user["premium"]:
            status = "⭐ *Premium* — безлимитный доступ"
        else:
            left = max(0, FREE_REQUESTS - user["used"])
            status = f"_Бесплатный_ — осталось *{left}* из *{FREE_REQUESTS}* запросов"
        text = (
            "✧ *Профиль* ✧\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{status}\n\n"
            f"_Всего поисков:_ *{user['used']}*\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        rows = []
        if not user["premium"]:
            rows.append([InlineKeyboardButton("⭐ Купить Premium", callback_data="buy_premium")])
        rows.append([InlineKeyboardButton("‹ Назад", callback_data="back")])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "buy_premium":
        text = (
            "⭐ *Username Finder Premium* ⭐\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🎉 *Глобальное обновление — скидка 50%!*\n\n"
            "*Что входит:*\n"
            "✧ _Безлимитные поиски_\n"
            "✧ _Выбор размера партии_ (3 · 5 · 10 · 15 · 20)\n"
            "✧ _Все типы юзернеймов_\n"
            "✧ _Навсегда_\n\n"
            f"*Цена:* ~~{PREMIUM_ORIGINAL_STARS}~~ *{PREMIUM_STARS} ⭐ Stars*\n"
            f"_Экономия {PREMIUM_ORIGINAL_STARS - PREMIUM_STARS} Stars — только сейчас!_\n\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⭐ Оплатить {PREMIUM_STARS} Stars", callback_data="pay_stars")],
            [InlineKeyboardButton("‹ Назад", callback_data="back")],
        ]))
        return

    if data == "pay_stars":
        await context.bot.send_invoice(
            chat_id=uid,
            title="Username Finder Premium",
            description="Безлимитный поиск юзернеймов + выбор размера партии — навсегда",
            payload="premium_purchase",
            currency="XTR",
            prices=[LabeledPrice("Premium", PREMIUM_STARS)],
            provider_token="",
        )
        return

    # ══ REPORT — USER FLOW ══

    if data == "report_start":
        last = get_last_report_time(uid)
        if last and (time.time() - last) < REPORT_COOLDOWN:
            cd = fmt_cooldown(last)
            await query.edit_message_text(
                "✧ *Репорт* ✧\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"_Следующий репорт можно отправить через:_\n\n"
                f"*{cd}*\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "_Один репорт в 2 часа_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("‹ Назад", callback_data="back")]])
            )
            return
        context.user_data["mode"] = "awaiting_report"
        await query.edit_message_text(
            "✧ *Репорт на баг* ✧\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Опиши проблему подробно:_\n\n"
            "✧ Что делал?\n"
            "✧ Что пошло не так?\n"
            "✧ Что ожидал увидеть?\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "_Напиши описание следующим сообщением:_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✕ Отмена", callback_data="report_cancel")]])
        )
        return

    if data == "report_cancel":
        context.user_data.clear()
        await query.edit_message_text(
            welcome_text(user["premium"], user["used"]),
            parse_mode="Markdown",
            reply_markup=main_keyboard(user["premium"])
        )
        return

    if data == "report_confirm":
        report_text = context.user_data.get("report_text", "")
        context.user_data.clear()
        if not report_text:
            await query.edit_message_text("_Ошибка: текст репорта пуст_", parse_mode="Markdown")
            return

        tg_user = query.from_user
        user_name = tg_user.username
        rid = create_report(uid, user_name, report_text)

        # Уведомить админа
        uname_str = f"@{user_name}" if user_name else f"id{uid}"
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"⭕️ *Новый репорт #{rid}*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"*От:* {uname_str} (`{uid}`)\n\n"
                    f"*Текст:*\n{report_text}\n\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "_Открой /rep для управления_"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

        await query.edit_message_text(
            "✧ *Репорт отправлен!* ✧\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"_#{rid} · Спасибо за обратную связь!_\n\n"
            "_Команда рассмотрит баг и ответит если нужно._\n\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⌂ Меню", callback_data="back")]])
        )
        return

    # ══ SEARCH ══

    if data.startswith("len_"):
        length = int(data.split("_")[1])
        await query.edit_message_text(
            f"✧ *Длина: {length} символов* ✧\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Выбери тип юзернейма:_",
            parse_mode="Markdown",
            reply_markup=type_keyboard(length)
        )
        return

    if data.startswith("type_"):
        _, length_s, type_key = data.split("_", 2)
        length = int(length_s)
        if not user["premium"] and user["used"] >= FREE_REQUESTS:
            await query.edit_message_text(
                "✧ *Лимит исчерпан* ✧\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"_Бесплатный доступ: {FREE_REQUESTS} запросов._\n\n"
                "Купи Premium для безлимитного поиска!",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⭐ Купить Premium", callback_data="buy_premium")],
                    [InlineKeyboardButton("‹ Назад", callback_data="back")],
                ])
            )
            return
        if user["premium"]:
            icon, label, _ = TYPE_META[type_key]
            await query.edit_message_text(
                f"✧ *{icon} {label}* ✧\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"_Длина:_ *{length} символов*\n\n"
                "⭐ _Выбери размер партии:_",
                parse_mode="Markdown",
                reply_markup=batch_keyboard(length, type_key)
            )
        else:
            await _do_search(query, context, uid, length, type_key, DEFAULT_BATCH, 1)
        return

    if data.startswith("batch_"):
        _, length_s, type_key, batch_s = data.split("_", 3)
        await _do_search(query, context, uid, int(length_s), type_key, int(batch_s), 1)
        return

    if data.startswith("search_"):
        parts = data.split("_")
        length, type_key, batch, batch_num = int(parts[1]), parts[2], int(parts[3]), int(parts[4])
        if not user["premium"] and user["used"] >= FREE_REQUESTS:
            await query.edit_message_text(
                "✧ *Лимит исчерпан* ✧\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "_Бесплатный доступ закончился._\n\n"
                "Купи Premium для безлимитного поиска!",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⭐ Купить Premium", callback_data="buy_premium")],
                    [InlineKeyboardButton("‹ Назад", callback_data="back")],
                ])
            )
            return
        await _do_search(query, context, uid, length, type_key, batch, batch_num)
        return

    # ══ ADMIN PANEL ══

    if not data.startswith("rep_"):
        return

    if uid != ADMIN_ID:
        await query.answer("⛔ Нет доступа", show_alert=True)
        return

    if data.startswith("rep_page_") or data.startswith("rep_filter_"):
        if data.startswith("rep_page_"):
            _, _, page_s, fs = data.split("_", 3)
            page = int(page_s)
        else:
            _, _, fs = data.split("_", 2)
            page = 0
        total = count_reports(None if fs == "all" else fs)
        reports = get_reports(None if fs == "all" else fs, REPORTS_PER_PAGE, page * REPORTS_PER_PAGE)
        text = render_report_list(reports, page, total, fs)
        kbd = admin_report_list_keyboard(page, total, fs)

        # Добавить кнопки открытия отдельного репорта
        extra_rows = [[InlineKeyboardButton(f"#{r['id']} открыть",
                       callback_data=f"rep_open_{r['id']}_{page}_{fs}")] for r in reports]
        full_kbd = InlineKeyboardMarkup(extra_rows + kbd.inline_keyboard)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=full_kbd)
        return

    if data.startswith("rep_open_"):
        _, _, rid_s, page_s, fs = data.split("_", 4)
        rid, page = int(rid_s), int(page_s)
        r = get_report(rid)
        if not r:
            await query.answer("Репорт не найден", show_alert=True)
            return
        await query.edit_message_text(
            render_single_report(r), parse_mode="Markdown",
            reply_markup=admin_single_report_keyboard(rid, page, fs)
        )
        return

    if data.startswith("rep_approve_"):
        _, _, rid_s, page_s, fs = data.split("_", 4)
        rid, page = int(rid_s), int(page_s)
        r = get_report(rid)
        if not r:
            await query.answer("Репорт не найден", show_alert=True)
            return
        update_report(rid, "approved")
        try:
            await context.bot.send_message(
                chat_id=r["user_id"],
                text=(
                    "✧ *Твой репорт одобрен* ✧\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"*Баг #{rid}:*\n_{r['text'][:100]}_\n\n"
                    "_Команда подтвердила баг и работает над исправлением._\n"
                    "_Спасибо за помощь!_ ✧"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass
        await query.answer("✅ Одобрено")
        total = count_reports(None if fs == "all" else fs)
        reports = get_reports(None if fs == "all" else fs, REPORTS_PER_PAGE, page * REPORTS_PER_PAGE)
        text = render_report_list(reports, page, total, fs)
        kbd = admin_report_list_keyboard(page, total, fs)
        extra_rows = [[InlineKeyboardButton(f"#{r['id']} открыть",
                       callback_data=f"rep_open_{r['id']}_{page}_{fs}")] for r in reports]
        await query.edit_message_text(text, parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(extra_rows + kbd.inline_keyboard))
        return

    if data.startswith("rep_decline_"):
        _, _, rid_s, page_s, fs = data.split("_", 4)
        rid, page = int(rid_s), int(page_s)
        r = get_report(rid)
        if not r:
            await query.answer("Репорт не найден", show_alert=True)
            return
        update_report(rid, "declined")
        try:
            await context.bot.send_message(
                chat_id=r["user_id"],
                text=(
                    "✧ *По твоему репорту* ✧\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"*Баг #{rid}:*\n_{r['text'][:100]}_\n\n"
                    "_Команда рассмотрела репорт и отклонила его._\n"
                    "_Возможно, это не баг или уже известная ситуация._"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass
        await query.answer("❌ Отклонено")
        total = count_reports(None if fs == "all" else fs)
        reports = get_reports(None if fs == "all" else fs, REPORTS_PER_PAGE, page * REPORTS_PER_PAGE)
        text = render_report_list(reports, page, total, fs)
        kbd = admin_report_list_keyboard(page, total, fs)
        extra_rows = [[InlineKeyboardButton(f"#{rp['id']} открыть",
                       callback_data=f"rep_open_{rp['id']}_{page}_{fs}")] for rp in reports]
        await query.edit_message_text(text, parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(extra_rows + kbd.inline_keyboard))
        return

    if data.startswith("rep_reply_"):
        _, _, rid_s, page_s, fs = data.split("_", 4)
        rid, page = int(rid_s), int(page_s)
        context.user_data["mode"] = "admin_reply"
        context.user_data["admin_reply_id"] = int(rid_s)
        context.user_data["admin_reply_page"] = int(page_s)
        context.user_data["admin_reply_filter"] = fs
        await query.edit_message_text(
            f"✧ *Ответ на репорт #{rid}* ✧\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Напиши ответ следующим сообщением:_\n\n"
            "_(Ответ будет отправлен пользователю и репорт будет одобрен)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✕ Отмена", callback_data=f"rep_open_{rid}_{page}_{fs}")
            ]])
        )
        return

    # ══ ADMIN PANEL — ОБЯЗАТЕЛЬНЫЕ КАНАЛЫ (/ad) ══

    if not data.startswith("ad_"):
        return

    if uid != ADMIN_ID:
        await query.answer("⛔ Нет доступа", show_alert=True)
        return

    if data == "ad_back":
        context.user_data.clear()
        await query.edit_message_text(ad_main_text(), parse_mode="Markdown", reply_markup=ad_main_keyboard())
        return

    if data == "ad_list":
        channels = load_channels()
        await query.edit_message_text(
            render_channels_list(channels), parse_mode="Markdown", reply_markup=ad_back_keyboard()
        )
        return

    if data == "ad_add":
        context.user_data["mode"] = "ad_awaiting_channel"
        await query.edit_message_text(
            "➕ *Добавление канала* ➕\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Отправь юзернейм канала следующим сообщением_\n"
            "_(например_ `@mychannel` _или_ `mychannel`_)_\n\n"
            "⚠️ _Бот должен быть администратором канала,_\n"
            "_иначе проверка подписки не будет работать_\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✕ Отмена", callback_data="ad_back")]])
        )
        return

    if data == "ad_remove_menu":
        channels = load_channels()
        if not channels:
            await query.edit_message_text(
                "_Список каналов пуст_", parse_mode="Markdown", reply_markup=ad_back_keyboard()
            )
            return
        await query.edit_message_text(
            "➖ *Выбери канал для удаления:*", parse_mode="Markdown",
            reply_markup=ad_remove_keyboard(channels)
        )
        return

    if data.startswith("ad_del_"):
        username = data[len("ad_del_"):]
        removed = remove_channel(username)
        await query.answer("🗑 Удалено" if removed else "Не найдено", show_alert=not removed)
        channels = load_channels()
        if not channels:
            await query.edit_message_text(
                "_Список каналов пуст_", parse_mode="Markdown", reply_markup=ad_back_keyboard()
            )
            return
        await query.edit_message_text(
            "➖ *Выбери канал для удаления:*", parse_mode="Markdown",
            reply_markup=ad_remove_keyboard(channels)
        )
        return

    if data == "ad_stats":
        await query.edit_message_text("⏳ _Собираю статистику..._", parse_mode="Markdown")
        text = await render_channels_stats(context.bot)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=ad_back_keyboard())
        return


async def _do_search(query, context, uid, length, type_key, batch, batch_num):
    icon, label, _ = TYPE_META[type_key]
    await query.edit_message_text(
        f"✧ *Идёт поиск...* ✧\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"_{icon} {label} · {length} символов_\n"
        f"_Партия #{batch_num} · {batch} юзернеймов_\n\n"
        f"_Проверяю через Telegram API..._\n✧ ✧ ✧",
        parse_mode="Markdown"
    )
    found = await find_usernames(context.bot, GENERATORS[type_key], length, batch)
    inc_request(uid)
    user = get_user(uid)

    if found:
        lines = "\n".join([f"  ✧ `@{u}`" for u in found])
        text = (
            f"✧ *Найдено {len(found)}* ✧\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"_{icon} {label} · {length} симв · Партия #{batch_num}_\n\n"
            f"*Свободные юзернеймы:*\n{lines}\n\n"
            f"_Нажми на юзернейм чтобы скопировать_\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
    else:
        text = (
            f"✧ *Ничего не найдено* ✧\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"_{icon} {label} · {length} симв · Партия #{batch_num}_\n\n"
            f"_Свободных не найдено — попробуй ещё раз!_\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )

    if not user["premium"]:
        text += f"\n_Осталось запросов: {max(0, FREE_REQUESTS - user['used'])}/{FREE_REQUESTS}_"

    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=result_keyboard(length, type_key, batch, batch_num)
    )


# ══════════════════════════════════════════════════════════════════════════════
# HANDLERS — ADMIN /rep
# ══════════════════════════════════════════════════════════════════════════════

async def _send_report_list(msg, page: int, fs: str, send_new: bool = False):
    total = count_reports(None if fs == "all" else fs)
    reports = get_reports(None if fs == "all" else fs, REPORTS_PER_PAGE, page * REPORTS_PER_PAGE)
    text = render_report_list(reports, page, total, fs)
    kbd = admin_report_list_keyboard(page, total, fs)
    extra_rows = [[InlineKeyboardButton(f"#{r['id']} открыть",
                   callback_data=f"rep_open_{r['id']}_{page}_{fs}")] for r in reports]
    full_kbd = InlineKeyboardMarkup(extra_rows + kbd.inline_keyboard)
    if send_new:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=full_kbd)
    else:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=full_kbd)


async def rep_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await update.message.reply_text("⛔ _Нет доступа_", parse_mode="Markdown")
        return
    context.user_data.clear()
    await _send_report_list(update.message, 0, "pending", send_new=True)


# ══════════════════════════════════════════════════════════════════════════════
# HANDLERS — ADMIN /ad
# ══════════════════════════════════════════════════════════════════════════════

async def ad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await update.message.reply_text("⛔ _Нет доступа_", parse_mode="Markdown")
        return
    context.user_data.clear()
    await update.message.reply_text(ad_main_text(), parse_mode="Markdown", reply_markup=ad_main_keyboard())


# ══════════════════════════════════════════════════════════════════════════════
# PAYMENT HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.message.successful_payment.invoice_payload == "premium_purchase":
        set_premium(uid)
        await update.message.reply_text(
            "⭐ *Premium активирован!* ⭐\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "✧ _Безлимитные поиски — навсегда_\n"
            "✧ _Выбор размера партии доступен_\n\n"
            "_Возвращайся в меню — всё готово!_",
            parse_mode="Markdown",
            reply_markup=main_keyboard(premium=True)
        )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rep", rep_command))
    app.add_handler(CommandHandler("ad", ad_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    logger.info("✧ Username Finder Bot запущен ✧")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
