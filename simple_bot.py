import os, json, re, uuid, html, asyncio, time, logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from collections import Counter
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Try to import telegram - if it fails, work in demo mode
try:
    from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, constants, BotCommand, BotCommandScopeChat, BotCommandScopeDefault
    from telegram.ext import Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
    TELEGRAM_AVAILABLE = True
    print("✅ Telegram library loaded successfully")
except ImportError as e:
    print(f"⚠️  Telegram library not available: {e}")
    print("Working in demo mode - set BOT_TOKEN and install python-telegram-bot for full functionality")
    TELEGRAM_AVAILABLE = False
    
    # Create mock classes for demo mode
    class Update:
        def __init__(self): 
            self.effective_user = type('user', (), {'id': 12345, 'first_name': 'Demo', 'username': 'demo_user'})()
            self.effective_chat = type('chat', (), {'id': 11111, 'title': 'Demo Chat'})()
            self.message = type('message', (), {'reply_text': lambda text, **kwargs: print(f"Bot: {text}")})()
            self.callback_query = type('query', (), {'answer': lambda **kwargs: None, 'edit_message_text': lambda text, **kwargs: print(f"Bot: {text}")})()
    
    class InlineKeyboardMarkup:
        def __init__(self, keyboard): 
            self.keyboard = keyboard
    
    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None): 
            self.text = text
            self.callback_data = callback_data
    
    class constants:
        class ParseMode:
            HTML = "HTML"
    
    class ContextTypes:
        DEFAULT_TYPE = None

import templates_store

STATE_FILE = os.getenv("STATE_FILE", "state.json")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x)
MERCHANT_IDS = set(int(x) for x in os.getenv("MERCHANT_IDS", "").replace(" ", "").split(",") if x)
DEFAULT_TTL_MIN = int(os.getenv("DEFAULT_TTL_MIN", "15"))
PORT = int(os.getenv("PORT", "5000"))

# Предустановленные чаты для автоматического добавления при deploy/redeploy
DEFAULT_CHATS = {
    "-1003996635324": {"id": -1003996635324, "name": "OTC"},
    "-1002271241387": {"id": -1002271241387, "name": "акулы"},
    "-4991871961": {"id": -4991871961, "name": "Levon&MP"},
    "-4754013956": {"id": -4754013956, "name": "ZED"},
    "-1003778389969": {"id": -1003778389969, "name": "534 Levon Lux"},
    "-4969976093": {"id": -4969976093, "name": "ving"},
    "-1002035500063": {"id": -1002035500063, "name": "Levon & GRITHER TEAM"},
    "-4662200552": {"id": -4662200552, "name": "тесты"}
}

# Order creation session storage
user_sessions = {}

# HTTP server for Cloud Run deployment
async def handle_health_check(request):
    """Health check endpoint for Cloud Run."""
    return web.Response(text="OK")

async def handle_test_list_chats(request):
    """Test endpoint for list_chats functionality"""
    try:
        state = load_state()
        lines = []
        for chat_key, chat_info in state["chats"].items():
            lines.append(f"{chat_info['name']} (ID: {chat_info['id']})")
        result = "Chats: " + ", ".join(lines) if lines else "No chats"
        return web.Response(text=result)
    except Exception as e:
        return web.Response(text=f"Error: {str(e)}", status=500)

async def handle_webhook(request):
    """Handle Telegram webhook updates."""
    return web.Response(text="Webhook received")

async def start_http_server():
    """Start HTTP server for Cloud Run deployment."""
    app = web.Application()
    app.router.add_get('/', handle_health_check)
    app.router.add_get('/health', handle_health_check)
    app.router.add_get('/test/list_chats', handle_test_list_chats)
    app.router.add_post('/webhook', handle_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    # Слушаем только localhost: healthcheck доступен локально, но список чатов
    # (/test/list_chats) не торчит наружу на публичном IP сервера.
    host = os.getenv("HTTP_HOST", "127.0.0.1")
    site = web.TCPSite(runner, host, PORT)
    await site.start()
    print(f"🌐 HTTP server started on {host}:{PORT}")
    return runner

def escape_html(text: str) -> str:
    """Escape HTML characters in user-generated content to prevent parsing errors."""
    if not text:
        return ""
    return html.escape(str(text))

def format_ttl_display(ttl_min: int) -> str:
    """Форматирует TTL для красивого отображения"""
    if ttl_min == 1440:
        return "в течение дня"
    elif ttl_min == 120:
        return "2 часа"
    elif ttl_min == 60:
        return "1 час"
    elif ttl_min >= 60:
        hours = ttl_min // 60
        return f"{hours} час{'а' if hours in [2, 3, 4] else 'ов' if hours >= 5 else ''}"
    else:
        return f"{ttl_min} мин"

def safe_parse_mode():
    """Return HTML parse mode only if Telegram is available, otherwise None."""
    return constants.ParseMode.HTML if TELEGRAM_AVAILABLE else None

async def send_message_safe(bot, chat_id: int, text: str, **kwargs):
    """Send message with error handling for BadRequest and chat migration errors."""
    try:
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as e:
        error_str = str(e)
        # Handle group migrated to supergroup
        if "migrate" in error_str.lower() or "ChatMigrated" in type(e).__name__:
            new_chat_id = getattr(e, 'new_chat_id', None)
            if new_chat_id:
                print(f"  ↗️ Chat {chat_id} migrated to supergroup {new_chat_id}, retrying...")
                return await bot.send_message(chat_id=new_chat_id, text=text, **kwargs)
        # Handle HTML parse errors — retry without parse_mode
        if "BadRequest" in error_str and "parse_mode" in kwargs:
            kwargs_no_html = kwargs.copy()
            kwargs_no_html.pop("parse_mode", None)
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs_no_html)
        raise

async def edit_message_safe(bot, chat_id: int, message_id: int, text: str, **kwargs):
    """Edit message with error handling for BadRequest and chat migration errors."""
    try:
        return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs)
    except Exception as e:
        error_str = str(e)
        # Handle group migrated to supergroup
        if "migrate" in error_str.lower() or "ChatMigrated" in type(e).__name__:
            new_chat_id = getattr(e, 'new_chat_id', None)
            if new_chat_id:
                print(f"  ↗️ Chat {chat_id} migrated to {new_chat_id}, retrying edit...")
                return await bot.edit_message_text(chat_id=new_chat_id, message_id=message_id, text=text, **kwargs)
        # Handle HTML parse errors — retry without parse_mode
        if "BadRequest" in error_str and "parse_mode" in kwargs:
            kwargs_no_html = kwargs.copy()
            kwargs_no_html.pop("parse_mode", None)
            return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs_no_html)
        print(f"  ⚠️ edit_message_safe failed for chat {chat_id} msg {message_id}: {error_str}")
        raise

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        initial_state = {
            "admins": list(ADMIN_IDS), 
            "merchants": list(MERCHANT_IDS),
            "chats": DEFAULT_CHATS.copy(), 
            "orders": {},
            "user_settings": {},
            "user_names": {}
        }
        return initial_state
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        try: 
            data = json.load(f)
        except Exception: 
            data = {
                "admins": list(ADMIN_IDS), 
                "merchants": list(MERCHANT_IDS),
                "chats": DEFAULT_CHATS.copy(), 
                "orders": {},
                "user_settings": {},
                "user_names": {}
            }
    
    # Migrate old "broadcasts" to "orders" if needed
    if "broadcasts" in data:
        if "orders" not in data:
            data["orders"] = data["broadcasts"]
        else:
            data["orders"].update(data["broadcasts"])
        del data["broadcasts"]
    
    data["admins"] = list(set(data.get("admins", [])) | ADMIN_IDS)
    data["merchants"] = list(set(data.get("merchants", [])) | MERCHANT_IDS)
    
    # Migrate old chats format (list) to new format (dict)
    if "chats" in data and isinstance(data["chats"], list):
        old_chats = data["chats"]
        data["chats"] = {}
        for chat_id in old_chats:
            data["chats"][str(chat_id)] = {
                "id": chat_id,
                "name": f"Чат {chat_id}"
            }
    
    data.setdefault("chats", {})
    data.setdefault("orders", {})
    data.setdefault("user_settings", {})
    data.setdefault("user_names", {})
    data.setdefault("merchant_stats", {})
    data.setdefault("deals", [])
    data.setdefault("ratings", {})
    data.setdefault("templates", {})

    # Самолечение мигрировавших групп: если чат с известным именем висит под старым id,
    # переносим его на актуальный id из DEFAULT_CHATS (иначе ломается дедупликация отправки).
    default_by_name = {info["name"]: info for info in DEFAULT_CHATS.values()}
    for old_key in list(data["chats"].keys()):
        chat = data["chats"][old_key]
        target = default_by_name.get(chat.get("name"))
        if target and chat.get("id") != target["id"]:
            data["chats"].pop(old_key, None)
            data["chats"][str(target["id"])] = {"id": target["id"], "name": target["name"]}

    # Автоматически добавляем предустановленные чаты
    # Проверяем по имени, чтобы не дублировать мигрировавшие группы (старый ID vs новый)
    existing_names = {info.get("name", "") for info in data["chats"].values()}
    for chat_key, chat_info in DEFAULT_CHATS.items():
        if chat_key not in data["chats"] and chat_info["name"] not in existing_names:
            data["chats"][chat_key] = chat_info.copy()
    
    return data

def save_state(state: Dict[str, Any]) -> None:
    # Атомарная запись: пишем во временный файл рядом и заменяем им основной.
    # Защищает state.json от порчи при краше/ребуте во время записи.
    dir_name = os.path.dirname(os.path.abspath(STATE_FILE))
    tmp_path = os.path.join(dir_name, f".{os.path.basename(STATE_FILE)}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, STATE_FILE)

def _migrate_chat_id(state: Dict[str, Any], old_id: int, new_id: int) -> None:
    """Группа мигрировала в супергруппу — переносим запись чата на новый id.

    Иначе old_id из state["chats"] и new_id из msg.chat.id расходятся, и дедупликация
    отправки по чатам ломается (кнопка чата не исчезает, можно слать дубли)."""
    if not new_id or old_id == new_id:
        return
    chats = state.setdefault("chats", {})
    chat_info = chats.pop(str(old_id), None)
    if chat_info is None:
        chat_info = {"name": f"Чат {new_id}"}
    chat_info["id"] = new_id
    chats[str(new_id)] = chat_info

def _record_sent_message(state: Dict[str, Any], order: Dict[str, Any], requested_cid: int, msg) -> int:
    """Сохраняет отправленное сообщение в order["messages"]; при миграции группы
    чинит id чата в state, чтобы дальнейшая дедупликация работала. Возвращает реальный chat_id."""
    real_cid = msg.chat.id
    if real_cid != requested_cid:
        _migrate_chat_id(state, requested_cid, real_cid)
    order.setdefault("messages", []).append({"chat_id": real_cid, "message_id": msg.message_id})
    return real_cid

def is_admin(uid: int, state: Dict[str, Any]) -> bool:
    return uid in set(state.get("admins", []))

def is_merchant(uid: int, state: Dict[str, Any]) -> bool: 
    return uid in set(state.get("merchants", []))

def can_create_orders(uid: int, state: Dict[str, Any]) -> bool:
    return is_admin(uid, state) or is_merchant(uid, state)

def short_id(bid: str) -> str:
    return bid.split("-")[0]

# ─── Partner/Merchant stats helpers ───────────────────────────────────────

def now_utc() -> str:
    """Единый формат UTC timestamp для всего кода."""
    return datetime.now(timezone.utc).isoformat()

def increment_merchant_posted(state: Dict[str, Any], user_id: int) -> None:
    uid = str(user_id)
    state["merchant_stats"].setdefault(uid, {"total_posted": 0})
    state["merchant_stats"][uid]["total_posted"] += 1

def get_merchant_posted(state: Dict[str, Any], user_id: int) -> int:
    return state["merchant_stats"].get(str(user_id), {}).get("total_posted", 0)

def increment_partner_taken(state: Dict[str, Any], user_id: int) -> None:
    uid = str(user_id)
    state["ratings"].setdefault(uid, {
        "total_deals": 0, "total_taken": 0,
        "avg_score": None, "scores": [], "avg_completion_minutes": None
    })
    state["ratings"][uid]["total_taken"] += 1

def record_deal(state: Dict[str, Any], order_id: str, merchant_id: int, partner_id: int,
                amount, direction: str, taken_at: str, closed_at: str) -> None:
    uid = str(partner_id)
    state["ratings"].setdefault(uid, {
        "total_deals": 0, "total_taken": 0,
        "avg_score": None, "scores": [], "avg_completion_minutes": None
    })
    state["ratings"][uid]["total_deals"] += 1
    try:
        t1 = datetime.fromisoformat(taken_at.replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        minutes = round((t2 - t1).total_seconds() / 60)
        prev_avg = state["ratings"][uid]["avg_completion_minutes"]
        count = state["ratings"][uid]["total_deals"]
        if prev_avg is None:
            state["ratings"][uid]["avg_completion_minutes"] = minutes
        else:
            state["ratings"][uid]["avg_completion_minutes"] = round(
                (prev_avg * (count - 1) + minutes) / count
            )
    except Exception:
        pass

def record_rating(state: Dict[str, Any], partner_id: int, score: int) -> None:
    uid = str(partner_id)
    state["ratings"].setdefault(uid, {
        "total_deals": 0, "total_taken": 0,
        "avg_score": None, "scores": [], "avg_completion_minutes": None
    })
    state["ratings"][uid]["scores"].append(score)
    scores = state["ratings"][uid]["scores"]
    state["ratings"][uid]["avg_score"] = round(sum(scores) / len(scores), 1)

def get_partner_rating(state: Dict[str, Any], user_id: int) -> dict:
    return state["ratings"].get(str(user_id), {
        "total_deals": 0, "total_taken": 0,
        "avg_score": None, "scores": [], "avg_completion_minutes": None
    })

def format_rating_badge(state: Dict[str, Any], user_id: int) -> str:
    posted = get_merchant_posted(state, user_id)
    r = get_partner_rating(state, user_id)
    avg = r.get("avg_score")
    if avg is None:
        label = "🆕 Новичок"
    elif avg >= 4.8:
        label = "🔥 Легенда"
    elif avg >= 4.5:
        label = "💎 Топовый"
    elif avg >= 4.0:
        label = "✅ Надёжный"
    elif avg >= 3.5:
        label = "👍 Нормальный"
    else:
        label = "🐢 Осторожно"
    score_str = f"⭐️ {avg}" if avg is not None else "нет оценок"
    return f"{score_str} · {posted} заявок · {label}"

def find_user_by_username(state: Dict[str, Any], username: str) -> Optional[int]:
    username_lower = username.lower()
    for order in state.get("orders", {}).values():
        cb = order.get("claimed_by")
        if cb and cb.get("username", "").lower() == username_lower:
            return cb["id"]
    for order in state.get("orders", {}).values():
        if order.get("creator_username", "").lower() == username_lower:
            return order.get("creator_id")
    # Ищем по user_names
    for uid, info in state.get("user_names", {}).items():
        uname = info.get("username", "") if isinstance(info, dict) else ""
        if uname.lower() == username_lower:
            return int(uid)
    return None

def lookup_username(state: Dict[str, Any], user_id: int) -> str:
    for order in state.get("orders", {}).values():
        cb = order.get("claimed_by")
        if cb and cb.get("id") == user_id and cb.get("username"):
            return cb["username"]
        if order.get("creator_id") == user_id and order.get("creator_username"):
            return order["creator_username"]
    info = state.get("user_names", {}).get(str(user_id))
    if info:
        uname = info.get("username") if isinstance(info, dict) else None
        if uname:
            return uname
    return str(user_id)

# ─── End Partner/Merchant stats helpers ───────────────────────────────────

def build_order_control_keyboard(bid: str, in_template: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура управления заявкой в личке мерчанта (сводка после публикации).

    in_template=True — заявка уже сохранена в шаблон: кнопка «В шаблон» заменяется
    неактивной галочкой «✅ В шаблоне», остальные кнопки управления остаются.
    """
    tpl_button = (
        InlineKeyboardButton("✅ В шаблоне", callback_data="tpl:noop")
        if in_template else
        InlineKeyboardButton("💾 В шаблон", callback_data=f"tpl:save:{bid}")
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Напомнить о заявке", callback_data=f"remind_bot:{bid}")],
        [InlineKeyboardButton("📝 Редактировать сумму", callback_data=f"edit_amount:{bid}")],
        [InlineKeyboardButton("⏰ Продлить на 30 мин", callback_data=f"extend:{bid}")],
        [tpl_button],
        [InlineKeyboardButton("📤 Отправить в оставшиеся чаты", callback_data=f"send_remaining:{bid}")],
        [InlineKeyboardButton("🗑️ Закрыть заявку", callback_data=f"close:{bid}")]
    ])


def build_keyboard(bid: str, state: Dict[str, Any], chat_id: int = None, user_id: int = None):
    order = state["orders"].get(bid)
    if not order or order.get("expired", False): 
        return None
    
    buttons = []
    
    if not order.get("claimed_by"):
        buttons.append([InlineKeyboardButton("✅ Взять", callback_data=f"claim:{bid}")])
    elif order.get("claimed_by"):
        # Кнопка "Освободить" только для исполнителя заявки, админа или создателя
        claimer_id = order["claimed_by"]["id"]
        creator_id = order.get("creator_id")
        if user_id and (claimer_id == user_id or is_admin(user_id, state) or creator_id == user_id):
            buttons.append([InlineKeyboardButton("🔓 Освободить", callback_data=f"unclaim:{bid}")])
    
    # Кнопка "Закрыть заявку" только в приватных чатах (боте), не в рассылках
    if chat_id is None or chat_id > 0:  # Приватный чат или бот
        buttons.append([InlineKeyboardButton("❌ Закрыть заявку", callback_data=f"close:{bid}")])
    
    return InlineKeyboardMarkup(buttons)

def human_name(u) -> str:
    if hasattr(u, 'first_name'):
        parts = [p for p in [u.first_name, getattr(u, 'last_name', None)] if p]
        base = " ".join(parts) if parts else (getattr(u, 'username', None) or f"id:{u.id}")
        username = getattr(u, 'username', None)
        return f"{base} (@{username})" if username else base
    return str(u)


def save_user_name(user_id: int, user, state: Dict[str, Any]):
    """Сохраняет имя пользователя для отображения в заявках"""
    if user and user_id:
        # Формируем имя из доступных полей
        name_parts = []
        if hasattr(user, 'first_name') and user.first_name:
            name_parts.append(user.first_name.strip())
        if hasattr(user, 'last_name') and user.last_name:
            name_parts.append(user.last_name.strip())
        
        display_name = " ".join(name_parts) if name_parts else None
        
        # Если имени нет, используем username
        if not display_name and hasattr(user, 'username') and user.username:
            display_name = f"@{user.username}"
        
        # Сохраняем имя и username отдельно
        if display_name:
            state.setdefault("user_names", {})
            state["user_names"][str(user_id)] = {
                "name": display_name,
                "username": getattr(user, 'username', None)
            }

def get_creator_name(creator_id: int, state: Dict[str, Any]) -> str:
    """Получает имя создателя заявки"""
    # Проверяем сохраненные имена пользователей
    user_names = state.get("user_names", {})
    if str(creator_id) in user_names:
        user_info = user_names[str(creator_id)]
        # Поддержка старого формата (строка) и нового (объект)
        if isinstance(user_info, str):
            return user_info
        else:
            name = user_info.get("name", "")
            username = user_info.get("username")
            if username and not name.startswith("@"):
                return f"{name} (@{username})"
            return name
    
    # Если имени нет, показываем роль и ID
    if is_admin(creator_id, state):
        return f"Админ #{creator_id}"
    elif is_merchant(creator_id, state):
        return f"Мерчант #{creator_id}"
    else:
        return f"Пользователь #{creator_id}"

def render_message(bid: str, state: Dict[str, Any], chat_id: int = None, viewer_id: int = None) -> str:
    order = state["orders"][bid]
    
    # Status rendering with privacy control
    if order.get("expired"):
        status = "🔴 Статус: истёк срок"
    elif order.get("claimed_by"):
        claimer = order["claimed_by"]
        # Show executor details only to admins or users in the same chat where claim happened
        if viewer_id and (is_admin(viewer_id, state) or claimer.get("chat_id") == chat_id):
            status = f"🟡🟡🟡 Статус: взята — {escape_html(claimer['name'])}"
        else:
            status = "🟡🟡🟡 Статус: уже забрали"
    else:
        status = "🟢🟢🟢 Статус: свободна"
    
    # Используем только человекочитаемое время
    ttl_display = format_ttl_display(order["ttl_min"])
    
    # Получаем информацию о создателе
    creator_id = order.get("creator_id")
    creator_name = get_creator_name(creator_id, state) if creator_id else ""
    creator_info = f"👤 От: <b>{escape_html(creator_name)}</b>\n" if creator_name else ""
    
    # Format order text based on type - escape user-generated content
    if order.get("order_type") == "structured":
        direction_raw = order.get("direction", "")
        direction = escape_html(direction_raw)
        
        # Добавляем смайлик и делаем направление жирным
        if "Куплю" in direction_raw:
            direction_formatted = f"🟢 <b>{direction}</b>"
        else:  # Продам
            direction_formatted = f"🔴 <b>{direction}</b>"
        
        amount = order.get("amount", "")
        bank = escape_html(order.get("bank", ""))
        payments = order.get("payments", "")
        rate = escape_html(order.get("rate", ""))
        formatted_amount = f"{amount:,}" if isinstance(amount, int) else escape_html(str(amount))
        # ttl_display уже определен выше
        
        # Формируем текст с условным отображением платежей
        text_parts = [
            direction_formatted,
            f"💰 <b>Сумма:</b> {formatted_amount} RUB",
            f"🏦 <b>Банк:</b> {bank}"
        ]
        
        # Добавляем платежи только если поле есть и не пустое
        if payments:
            text_parts.append(f"💳 <b>Платежей:</b> {escape_html(payments)}")
            
        text_parts.append(f"📈 <b>Курс:</b> {rate}")
        # Удаляем дублированное поле ⏱ Срок, так как время показывается внизу
        
        text = "\n".join(text_parts)
    else:
        text = escape_html(order["text"])
    
    # Бейдж автора (рейтинг мерчанта) — creator_id уже взят выше
    badge_line = ""
    if creator_id:
        creator_username = order.get("creator_username") or lookup_username(state, creator_id)
        badge = format_rating_badge(state, creator_id)
        badge_line = f"\n\n👤 @{escape_html(str(creator_username))} · {badge}"

    return f"📣 <b>Заявка #{short_id(bid)}</b>\n{text}\n\n⏳ Актуально: ≈{escape_html(ttl_display)}\n{creator_info}{status}{badge_line}"


def get_user_timezone(user_id: int, state: Dict[str, Any]) -> int:
    """Получает часовой пояс пользователя (смещение в часах от UTC)"""
    user_settings = state.get("user_settings", {})
    return user_settings.get(str(user_id), {}).get("timezone", 0)  # По умолчанию UTC

def set_user_timezone(user_id: int, timezone_offset: int, state: Dict[str, Any]):
    """Устанавливает часовой пояс пользователя"""
    if "user_settings" not in state:
        state["user_settings"] = {}
    if str(user_id) not in state["user_settings"]:
        state["user_settings"][str(user_id)] = {}
    state["user_settings"][str(user_id)]["timezone"] = timezone_offset

def is_private_chat(update: Update) -> bool:
    """Проверяет, что команда выполняется в личном чате с ботом"""
    return update.effective_chat.id > 0

def check_private_chat_only(update: Update) -> bool:
    """Проверяет и отвечает, если команда выполняется не в личном чате"""
    if not is_private_chat(update):
        # В групповых чатах команды игнорируются (не отвечаем ничего)
        return False
    return True

async def check_user_in_chat(bot, chat_id: int, user_id: int) -> bool:
    """Проверяет, является ли пользователь участником чата"""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        # Проверяем, что пользователь не покинул чат и не заблокирован
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        # Если ошибка (например, чат не найден или бот не состоит в чате), считаем что нет доступа
        return False

async def get_user_chats(user_id: int, state: Dict[str, Any], bot=None) -> List[int]:
    """Get chats where user can send orders - only chats where user is actually a member"""
    available_chats = []
    
    # Админы видят все чаты, остальные только чаты где они состоят
    all_chats = [chat["id"] for chat in state["chats"].values()]
    
    if not bot:
        # Fallback для случаев без bot context - возвращаем все для админов и мерчантов
        if is_admin(user_id, state) or is_merchant(user_id, state):
            return all_chats
        return []
    
    # Для админов возвращаем все зарегистрированные чаты без проверки членства
    if is_admin(user_id, state):
        return all_chats
    
    # Для мерчантов и пользователей проверяем членство в каждом чате
    for chat_id in all_chats:
        if await check_user_in_chat(bot, chat_id, user_id):
            available_chats.append(chat_id)
    
    # Мерчанты могут создавать заявки
    if is_merchant(user_id, state):
        return available_chats
    return []  # Обычные пользователи не могут создавать заявки

# Command handlers
async def start(update: Update, context):
    if not check_private_chat_only(update):
        return

    state = load_state()
    uid = update.effective_user.id

    if not await check_not_banned(update, state):
        return

    save_user_name(uid, update.effective_user, state)
    save_state(state)

    if is_admin(uid, state):
        text = (
            "🔧 <b>Привет, админ!</b>\n"
            "Полный доступ к системе.\n\n"
            "📋 <b>Главные команды:</b>\n"
            "/create — создать заявку\n"
            "/stats — расширенная статистика\n"
            "/admins — управление пользователями\n"
            "/help — полный справочник"
        )
        role = "admin"
    elif can_create_orders(uid, state):
        text = (
            "💰 <b>Привет, мерчант!</b>\n"
            "Создавай заявки и управляй сделками.\n\n"
            "📋 <b>Главные команды:</b>\n"
            "/create — создать заявку\n"
            "/myorders — активные заявки\n"
            "/help — полный справочник"
        )
        role = "merchant"
    else:
        text = (
            "👋 <b>Привет!</b>\n"
            "Ты партнёр — берёшь заявки в группах.\n\n"
            "📋 <b>Главные команды:</b>\n"
            "/partner @username — карточка партнёра\n"
            "/history — история твоих сделок\n"
            "/timezone — настроить часовой пояс"
        )
        role = "partner"

    await update.message.reply_text(text, parse_mode=safe_parse_mode())
    await set_user_commands(context.bot, uid, role)

async def timezone_cmd(update: Update, context):
    """Установка часового пояса пользователя"""
    if not check_private_chat_only(update):
        return

    state = load_state()
    uid = update.effective_user.id

    if not await check_not_banned(update, state):
        return

    # Получаем аргументы команды с резервным способом парсинга
    command_text = update.message.text or ""
    parts = command_text.split()
    timezone_arg = None
    
    if context.args and len(context.args) > 0:
        timezone_arg = context.args[0]
    elif len(parts) > 1:
        timezone_arg = parts[1]
    
    if not timezone_arg:
        current_tz = get_user_timezone(uid, state)
        tz_names = {
            0: "UTC", 3: "МСК", 4: "GST (Дубай)", 5: "YEKT", 8: "IRKT", -5: "EST", -8: "PST"
        }
        current_name = tz_names.get(current_tz, f"GMT{'+' if current_tz >= 0 else ''}{current_tz}")
        
        await update.message.reply_text(
            f"🕐 Ваш текущий часовой пояс: {current_name} (UTC{'+' if current_tz >= 0 else ''}{current_tz})\n\n"
            "Чтобы изменить, используйте: /timezone <смещение>\n\n"
            "Примеры:\n"
            "/timezone 4 — Дубай/ОАЭ (GST)\n"
            "/timezone 3 — Москва (МСК)\n"
            "/timezone 5 — Екатеринбург (YEKT)\n"
            "/timezone 8 — Иркутск (IRKT)\n"
            "/timezone 0 — UTC\n"
            "/timezone -5 — Нью-Йорк (EST)\n"
            "/timezone -8 — Лос-Анджелес (PST)\n\n"
            "Диапазон: от -12 до +12 часов"
        )
        return
    
    try:
        timezone_offset = int(timezone_arg)
        if timezone_offset < -12 or timezone_offset > 12:
            raise ValueError("Часовой пояс должен быть от -12 до +12")
        
        set_user_timezone(uid, timezone_offset, state)
        save_state(state)
        
        tz_names = {
            0: "UTC", 1: "CET", 2: "EET", 3: "МСК", 4: "GST (Дубай)", 5: "YEKT", 
            6: "OMST", 7: "KRAT", 8: "IRKT", 9: "YAKT", 10: "VLAT", 11: "MAGT", 12: "PETT",
            -1: "GMT-1", -2: "GMT-2", -3: "GMT-3", -4: "GMT-4", -5: "EST", -6: "CST", 
            -7: "MST", -8: "PST", -9: "AKST", -10: "HST", -11: "GMT-11", -12: "GMT-12"
        }
        tz_name = tz_names.get(timezone_offset, f"GMT{'+' if timezone_offset >= 0 else ''}{timezone_offset}")
        
        await update.message.reply_text(
            f"✅ Часовой пояс установлен: {tz_name} (UTC{'+' if timezone_offset >= 0 else ''}{timezone_offset})\n\n"
            "Теперь все времена заявок будут показываться в вашем часовом поясе!"
        )
        
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат. Используйте число от -12 до +12.\n"
            "Пример: /timezone 3"
        )

async def help_cmd(update: Update, context):
    if not check_private_chat_only(update):
        return

    state = load_state()
    uid = update.effective_user.id

    if not await check_not_banned(update, state):
        return

    if is_admin(uid, state):
        text = (
            "📖 <b>Справка — Админ</b>\n\n"
            "📦 <b>Заявки:</b>\n"
            "/create — создать заявку\n"
            "/myorders — мои активные заявки\n\n"
            "👥 <b>Партнёры:</b>\n"
            "/partner @username — карточка партнёра\n"
            "/history [@username] — история сделок\n"
            "/stats — расширенная статистика\n\n"
            "🏗 <b>Чаты:</b>\n"
            "/register [название] — зарегистрировать чат\n"
            "/unregister — убрать чат\n"
            "/list — список чатов\n\n"
            "👮 <b>Управление:</b>\n"
            "/admins — список админов и мерчантов\n"
            "/merchants — список мерчантов\n"
            "/addmerchant &lt;user_id&gt; — добавить мерчанта\n"
            "/removemerchant &lt;user_id&gt; — убрать мерчанта\n"
            "/ban &lt;user_id&gt; &lt;причина&gt; — заблокировать\n"
            "/unban &lt;user_id&gt; — разблокировать\n"
            "/spamstats — статистика нарушений\n"
            "/setlimits &lt;type&gt; &lt;value&gt; — настройка лимитов\n\n"
            "⚙️ <b>Настройки:</b>\n"
            "/timezone [смещение] — часовой пояс\n"
            "/start — начало работы"
        )
        role = "admin"
    elif can_create_orders(uid, state):
        text = (
            "📖 <b>Справка — Мерчант</b>\n\n"
            "📦 <b>Заявки:</b>\n"
            "/create — создать заявку\n"
            "/myorders — мои активные заявки\n\n"
            "👥 <b>Партнёры:</b>\n"
            "/partner @username — карточка партнёра\n"
            "/history — история сделок\n\n"
            "⚙️ <b>Настройки:</b>\n"
            "/timezone [смещение] — часовой пояс\n"
            "/start — начало работы"
        )
        role = "merchant"
    else:
        text = (
            "📖 <b>Справка — Партнёр</b>\n\n"
            "/partner @username — карточка партнёра\n"
            "/history — история твоих сделок\n"
            "/timezone [смещение] — настроить часовой пояс\n"
            "/start — начало работы"
        )
        role = "partner"

    await update.message.reply_text(text, parse_mode=safe_parse_mode())
    await set_user_commands(context.bot, uid, role)


async def myorders_cmd(update: Update, context):
    """Показывает заявки пользователя"""
    if not check_private_chat_only(update):
        return

    state = load_state()
    uid = update.effective_user.id

    if not await check_not_banned(update, state):
        return

    my_orders = [(bid, order) for bid, order in state["orders"].items() 
                 if order.get("creator_id") == uid]
    
    if not my_orders:
        await update.message.reply_text("📝 У вас нет созданных заявок.")
        return
    
    lines = ["📋 <b>Ваши заявки:</b>\n"]
    for bid, order in my_orders[-10:]:  # Последние 10
        status = "🟢" if not order.get("expired") and not order.get("closed") else "🔴"
        claimed = " (взята)" if order.get("claimed_by") else ""
        
        if order.get("order_type") == "structured":
            amount = f"{order['amount']:,}" if isinstance(order['amount'], int) else order['amount']
            lines.append(f"{status} #{short_id(bid)} - {amount} RUB{claimed}")
        else:
            text_preview = order.get("text", "")[:30] + "..." if len(order.get("text", "")) > 30 else order.get("text", "")
            lines.append(f"{status} #{short_id(bid)} - {text_preview}{claimed}")
    
    await update.message.reply_text("\n".join(lines), parse_mode=safe_parse_mode())


async def admins_cmd(update: Update, context):
    """Показывает список админов и мерчантов (только для админов)"""
    if not check_private_chat_only(update):
        return

    state = load_state()
    uid = update.effective_user.id

    if not await check_not_banned(update, state):
        return

    if not is_admin(uid, state):
        await update.message.reply_text("❌ Только админы могут видеть список админов и мерчантов.")
        return
    
    admin_ids = state.get("admins", [])
    merchant_ids = state.get("merchants", [])
    
    lines = ["👥 <b>Список админов и мерчантов:</b>\n"]
    
    if admin_ids:
        lines.append("🔧 <b>Админы:</b>")
        for admin_id in admin_ids:
            lines.append(f"• ID: {admin_id}")
    else:
        lines.append("🔧 <b>Админы:</b> нет")
    
    if merchant_ids:
        lines.append("\n💼 <b>Мерчанты:</b>")
        for merchant_id in merchant_ids:
            lines.append(f"• ID: {merchant_id}")
    else:
        lines.append("\n💼 <b>Мерчанты:</b> нет")
    
    lines.append(f"\n📊 <b>Всего:</b> {len(admin_ids)} админов, {len(merchant_ids)} мерчантов")
    
    await update.message.reply_text("\n".join(lines), parse_mode=safe_parse_mode())

# Административные инструменты
def log_antispam_event(uid: int, event_type: str, order_id: str, state: dict):
    """Логируем антиспам события"""
    if "antispam_logs" not in state:
        state["antispam_logs"] = []
    
    log_entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "user_id": uid,
        "event_type": event_type,
        "order_id": order_id,
        "user_name": state.get("user_names", {}).get(str(uid), f"User {uid}")
    }
    
    state["antispam_logs"].append(log_entry)
    
    # Оставляем только последние 100 записей
    if len(state["antispam_logs"]) > 100:
        state["antispam_logs"] = state["antispam_logs"][-100:]

async def notify_admins_suspicious_activity(context, uid: int, event_type: str, order_id: str, state: dict):
    """Уведомляем админов о подозрительной активности"""
    if not TELEGRAM_AVAILABLE or not context:
        return
        
    admin_ids = state.get("admins", [])
    user_name = state.get("user_names", {}).get(str(uid), f"User {uid}")
    
    message = f"🚨 <b>Подозрительная активность</b>\n\n" \
              f"👤 Пользователь: {escape_html(user_name)} (ID: {uid})\n" \
              f"📝 Событие: {event_type}\n" \
              f"🆔 Заявка: #{short_id(order_id)}\n" \
              f"🕐 Время: {datetime.now().strftime('%H:%M:%S')}"
    
    for admin_id in admin_ids:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=message,
                parse_mode=safe_parse_mode()
            )
        except Exception:
            pass

def is_user_banned(uid: int, state: dict) -> bool:
    """Проверяет заблокирован ли пользователь"""
    banned_users = state.get("banned_users", {})
    return str(uid) in banned_users

async def check_not_banned(update, state) -> bool:
    """Returns False and replies if user is banned. Use in command handlers."""
    uid = update.effective_user.id
    if is_user_banned(uid, state):
        reason = state.get("banned_users", {}).get(str(uid), {}).get("reason", "Нарушение правил")
        await update.effective_message.reply_text(
            f"❌ Вы заблокированы.\n\nПричина: {escape_html(reason)}",
            parse_mode=safe_parse_mode()
        )
        return False
    return True

async def ban_user_cmd(update: Update, context):
    """Заблокировать пользователя (только для админов)"""
    if not check_private_chat_only(update):
        return

    state = load_state()
    uid = update.effective_user.id

    if not await check_not_banned(update, state):
        return

    if not is_admin(uid, state):
        await update.message.reply_text("❌ Только админы могут блокировать пользователей.")
        return
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Использование: /ban <user_id> <причина>\n\n"
            "Пример: /ban 123456789 Спам напоминаниями"
        )
        return
    
    try:
        target_uid = int(context.args[0])
        reason = " ".join(context.args[1:])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID пользователя.")
        return
    
    if target_uid == uid:
        await update.message.reply_text("❌ Нельзя заблокировать самого себя.")
        return
    
    if is_admin(target_uid, state):
        await update.message.reply_text("❌ Нельзя заблокировать админа.")
        return
    
    # Добавляем в список заблокированных
    if "banned_users" not in state:
        state["banned_users"] = {}
    
    state["banned_users"][str(target_uid)] = {
        "reason": reason,
        "banned_by": uid,
        "banned_at": datetime.now().isoformat(timespec="seconds"),
        "banned_by_name": state.get("user_names", {}).get(str(uid), f"Admin {uid}")
    }
    
    save_state(state)
    
    target_name = state.get("user_names", {}).get(str(target_uid), f"User {target_uid}")
    
    await update.message.reply_text(
        f"✅ Пользователь {escape_html(target_name)} (ID: {target_uid}) заблокирован.\n\n"
        f"📝 Причина: {escape_html(reason)}",
        parse_mode=safe_parse_mode()
    )

async def unban_user_cmd(update: Update, context):
    """Разблокировать пользователя (только для админов)"""
    if not check_private_chat_only(update):
        return

    state = load_state()
    uid = update.effective_user.id

    if not await check_not_banned(update, state):
        return

    if not is_admin(uid, state):
        await update.message.reply_text("❌ Только админы могут разблокировать пользователей.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "❌ Использование: /unban <user_id>\n\n"
            "Пример: /unban 123456789"
        )
        return
    
    try:
        target_uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID пользователя.")
        return
    
    banned_users = state.get("banned_users", {})
    
    if str(target_uid) not in banned_users:
        await update.message.reply_text("❌ Пользователь не заблокирован.")
        return
    
    # Удаляем из списка заблокированных
    del banned_users[str(target_uid)]
    save_state(state)
    
    target_name = state.get("user_names", {}).get(str(target_uid), f"User {target_uid}")
    
    await update.message.reply_text(
        f"✅ Пользователь {escape_html(target_name)} (ID: {target_uid}) разблокирован.",
        parse_mode=safe_parse_mode()
    )

async def spam_stats_cmd(update: Update, context):
    """Показать статистику нарушений (только для админов)"""
    if not check_private_chat_only(update):
        return

    state = load_state()
    uid = update.effective_user.id

    if not await check_not_banned(update, state):
        return

    if not is_admin(uid, state):
        await update.message.reply_text("❌ Только админы могут видеть статистику нарушений.")
        return
    
    banned_users = state.get("banned_users", {})
    antispam_logs = state.get("antispam_logs", [])
    
    # Статистика за последние 24 часа
    now = datetime.now()
    day_ago = now - timedelta(hours=24)
    
    recent_events = []
    for log in antispam_logs:
        try:
            log_time = datetime.fromisoformat(log["timestamp"])
            if log_time > day_ago:
                recent_events.append(log)
        except:
            continue
    
    lines = ["📊 <b>Статистика антиспам системы</b>\n"]
    
    # Заблокированные пользователи
    lines.append(f"🚫 <b>Заблокированных пользователей:</b> {len(banned_users)}")
    if banned_users:
        lines.append("")
        for user_id, ban_info in list(banned_users.items())[:5]:  # Показываем последние 5
            user_name = state.get("user_names", {}).get(user_id, f"User {user_id}")
            lines.append(f"• {escape_html(user_name)} (ID: {user_id})")
            lines.append(f"  Причина: {escape_html(ban_info['reason'])}")
        
        if len(banned_users) > 5:
            lines.append(f"  ... и еще {len(banned_users) - 5}")
    
    lines.append(f"\n📈 <b>События за 24 часа:</b> {len(recent_events)}")
    
    # Группируем события по типам
    event_counts = {}
    for event in recent_events:
        event_type = event.get("event_type", "unknown")
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
    
    if event_counts:
        lines.append("")
        for event_type, count in event_counts.items():
            event_names = {
                "reminder_limit": "Превышение лимитов напоминаний",
                "suspicious_activity": "Подозрительная активность"
            }
            name = event_names.get(event_type, event_type)
            lines.append(f"• {name}: {count}")
    
    # Последние события
    if recent_events:
        lines.append(f"\n🔍 <b>Последние события:</b>")
        for event in recent_events[-5:]:  # Показываем последние 5
            user_name = event.get("user_name", f"User {event.get('user_id', 'Unknown')}")
            timestamp = event.get("timestamp", "")
            try:
                time_str = datetime.fromisoformat(timestamp).strftime("%H:%M")
            except:
                time_str = timestamp
            lines.append(f"• {time_str} - {escape_html(user_name)}")
    
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=safe_parse_mode()
    )

async def set_limits_cmd(update: Update, context):
    """Настройка лимитов системы (только для админов)"""
    if not check_private_chat_only(update):
        return

    state = load_state()
    uid = update.effective_user.id

    if not await check_not_banned(update, state):
        return

    if not is_admin(uid, state):
        await update.message.reply_text("❌ Только админы могут настраивать лимиты.")
        return
    
    if not context.args or len(context.args) != 2:
        current_limits = state.get("system_limits", {
            "reminder_per_hour": 5,
            "reminder_interval_min": 10
        })
        
        await update.message.reply_text(
            f"⚙️ <b>Текущие лимиты системы:</b>\n\n"
            f"🔔 Напоминаний в час: {current_limits.get('reminder_per_hour', 5)}\n"
            f"⏰ Интервал между напоминаниями: {current_limits.get('reminder_interval_min', 10)} мин\n\n"
            f"❓ <b>Использование:</b> /setlimits &lt;type&gt; &lt;value&gt;\n\n"
            f"<b>Доступные типы:</b>\n"
            f"• reminder_per_hour - максимум напоминаний в час\n"
            f"• reminder_interval_min - минимальный интервал в минутах\n\n"
            f"<b>Примеры:</b>\n"
            f"/setlimits reminder_per_hour 3\n"
            f"/setlimits reminder_interval_min 15",
            parse_mode=safe_parse_mode()
        )
        return
    
    limit_type = context.args[0]
    try:
        value = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Значение должно быть числом.")
        return
    
    if limit_type not in ["reminder_per_hour", "reminder_interval_min"]:
        await update.message.reply_text("❌ Неизвестный тип лимита.")
        return
    
    if value <= 0:
        await update.message.reply_text("❌ Значение должно быть положительным.")
        return
    
    # Устанавливаем лимиты
    if "system_limits" not in state:
        state["system_limits"] = {}
    
    state["system_limits"][limit_type] = value
    save_state(state)
    
    limit_names = {
        "reminder_per_hour": "Напоминаний в час",
        "reminder_interval_min": "Интервал между напоминаниями (мин)"
    }
    
    await update.message.reply_text(
        f"✅ {limit_names[limit_type]} установлен: {value}",
        parse_mode=safe_parse_mode()
    )

async def register_chat(update: Update, context):
    state = load_state()
    uid = update.effective_user.id
    if not await check_not_banned(update, state):
        return
    if not is_admin(uid, state):
        await update.message.reply_text("Только админы могут регистрировать чаты.")
        return
    
    cid = update.effective_chat.id
    chat_key = str(cid)
    
    # Получаем название чата из команды или используем стандартное
    command_text = update.message.text or ""
    parts = command_text.split(" ", 1)
    if len(parts) > 1:
        chat_name = parts[1].strip()
    else:
        chat_name = update.effective_chat.title or f"Чат {cid}"
    
    if chat_key not in state["chats"]:
        state["chats"][chat_key] = {
            "id": cid,
            "name": chat_name
        }
        save_state(state)
        await update.message.reply_text(f"✅ Чат зарегистрирован: '{chat_name}' (ID: {cid})")
    else: 
        # Обновляем название если указано новое
        if len(parts) > 1:
            state["chats"][chat_key]["name"] = chat_name
            save_state(state)
            await update.message.reply_text(f"✅ Название чата обновлено: '{chat_name}' (ID: {cid})")
        else:
            await update.message.reply_text(f"Чат уже в списке как '{state['chats'][chat_key]['name']}'")

async def unregister_chat(update: Update, context):
    state = load_state()
    uid = update.effective_user.id
    if not await check_not_banned(update, state):
        return
    if not is_admin(uid, state):
        await update.message.reply_text("Только админы могут убирать чаты.")
        return
    
    cid = update.effective_chat.id
    chat_key = str(cid)
    
    if chat_key in state["chats"]:
        chat_name = state["chats"][chat_key]["name"]
        del state["chats"][chat_key]
        save_state(state)
        await update.message.reply_text(f"❌ Чат удалён: '{chat_name}' (ID: {cid})")
    else: 
        await update.message.reply_text("Этого чата нет в списке.")

async def list_chats(update: Update, context):
    state = load_state()
    uid = update.effective_user.id
    if not await check_not_banned(update, state):
        return
    if not is_admin(uid, state):
        await update.message.reply_text("Только админы могут видеть список чатов.")
        return
    
    if not state["chats"]:
        await update.message.reply_text("📋 Список целевых чатов пуст.")
        return
        
    lines = []
    for chat_key, chat_info in state["chats"].items():
        lines.append(f"📍 {chat_info['name']} (ID: {chat_info['id']})")
    
    await update.message.reply_text("📋 Целевые чаты:\n" + "\n".join(lines))

async def create_order_start(update: Update, context):
    if not check_private_chat_only(update):
        return
        
    state = load_state()
    uid = update.effective_user.id
    
    if not await check_not_banned(update, state):
        return

    # Сохраняем имя пользователя
    save_user_name(uid, update.effective_user, state)
    save_state(state)
    
    if not can_create_orders(uid, state):
        await update.message.reply_text("Только админы и мерчанты могут создавать заявки.")
        return
    
    templates = templates_store.list_templates(state, uid)
    if templates:
        user_sessions[uid] = {
            "step": "choose_template",
            "data": {
                "creator_id": uid,
                "creator_username": getattr(update.effective_user, 'username', None) or str(uid),
            }
        }
        buttons = [
            [InlineKeyboardButton(f"📋 {t['name']}", callback_data=f"tpl:use:{i}")]
            for i, t in enumerate(templates)
        ]
        buttons.append([InlineKeyboardButton("➕ Новую с нуля", callback_data="tpl:new")])
        buttons.append([InlineKeyboardButton("✖️ Отмена", callback_data="tpl:cancel")])
        await update.message.reply_text(
            "🏗 Создать заявку\n\nВыбери шаблон или создай новую:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # Нет шаблонов — обычный флоу с выбора направления
    user_sessions[uid] = {
        "step": "direction",
        "data": {
            "creator_id": uid,
            "creator_username": getattr(update.effective_user, 'username', None) or str(uid),
        }
    }
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Куплю RUB - отдам USDT", callback_data="dir:buy_rub")],
        [InlineKeyboardButton("Продам RUB - возьму USDT", callback_data="dir:sell_rub")]
    ])
    await update.message.reply_text(
        "🏗 Создание заявки\n\nВыберите направление:",
        reply_markup=keyboard
    )


async def handle_template_callback(update: Update, context, state: Dict[str, Any], data: str):
    q = update.callback_query
    uid = q.from_user.id
    parts = data.split(":")  # tpl:use:0 / tpl:new / tpl:save:{bid} / ...
    action = parts[1] if len(parts) > 1 else ""

    if action == "new":
        user_sessions[uid] = {
            "step": "direction",
            "data": {
                "creator_id": uid,
                "creator_username": q.from_user.username or str(uid),
            }
        }
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Куплю RUB - отдам USDT", callback_data="dir:buy_rub")],
            [InlineKeyboardButton("Продам RUB - возьму USDT", callback_data="dir:sell_rub")]
        ])
        await q.edit_message_text("🏗 Создание заявки\n\nВыберите направление:", reply_markup=keyboard)
        return

    if action == "use":
        try:
            index = int(parts[2])
        except (IndexError, ValueError):
            await q.edit_message_text("❌ Шаблон не найден. Открой /create заново.")
            return
        tpl = templates_store.get_template(state, uid, index)
        if not tpl:
            await q.edit_message_text("❌ Шаблон не найден. Открой /create заново.")
            return
        # Защита (М3): шаблон без обязательного поля сломал бы finalize/schedule_expiration.
        required = ("direction", "bank", "rate", "ttl_min")
        if any(tpl.get(f) is None for f in required):
            await q.edit_message_text(
                "❌ Шаблон повреждён (не хватает полей). Удали его через /templates и создай заново."
            )
            return
        user_sessions[uid] = {
            "step": "template_amount",
            "data": {
                "creator_id": uid,
                "creator_username": q.from_user.username or str(uid),
                "direction": tpl.get("direction"),
                "bank": tpl.get("bank"),
                "payments": tpl.get("payments", ""),
                "rate": tpl.get("rate"),
                "ttl_min": tpl.get("ttl_min"),
            }
        }
        await q.edit_message_text(
            f"📋 Шаблон «{escape_html(tpl['name'])}»\n\n💰 Введи сумму в RUB (только число):",
            parse_mode=safe_parse_mode()
        )
        return

    if action == "noop":
        # Кнопка-индикатор «✅ В шаблоне» — нажатие ничего не делает.
        return

    if action == "save":
        bid = parts[2] if len(parts) > 2 else ""
        order = state["orders"].get(bid)
        if not order:
            await q.message.reply_text("❌ Заявка закрыта, шаблон не сохранить.")
            return
        snapshot = templates_store.snapshot_from_order(order)
        user_sessions[uid] = {
            "step": "template_name",
            "data": {"tpl_snapshot": snapshot, "src_bid": bid},
        }
        # НЕ трогаем карточку заявки (её кнопки управления должны остаться) —
        # запрашиваем имя отдельным сообщением.
        await q.message.reply_text("💾 Введи имя шаблона (до 30 символов):")
        return

    if action == "overwrite":
        session = user_sessions.get(uid)
        if not session or "pending_name" not in session.get("data", {}):
            await q.edit_message_text("❌ Нечего перезаписывать. Начни заново через заявку.")
            return
        name = session["data"]["pending_name"]
        snapshot = session["data"].get("tpl_snapshot", {})
        src_bid = session["data"].get("src_bid")
        templates_store.add_template(state, uid, name, snapshot, overwrite=True)
        save_state(state)
        del user_sessions[uid]
        await _mark_order_in_template(context, state, src_bid)
        await q.edit_message_text(
            f"✅ Шаблон «{escape_html(name)}» перезаписан.", parse_mode=safe_parse_mode()
        )
        return

    if action == "cancel":
        user_sessions.pop(uid, None)
        await q.edit_message_text("✖️ Отменено.")
        return

    await handle_template_manage_callback(update, context, state, data, action, parts)


async def _mark_order_in_template(context, state, bid):
    """Ставит галочку «✅ В шаблоне» на карточке заявки в личке, сохраняя кнопки.

    Карточка заявки живёт по order['bot_message_id']/['bot_chat_id']. Если заявка
    уже закрыта или id нет — молча пропускаем (не критично)."""
    if not bid:
        return
    order = state.get("orders", {}).get(bid)
    if not order:
        return
    msg_id = order.get("bot_message_id")
    chat_id = order.get("bot_chat_id")
    if not msg_id or not chat_id:
        return
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=msg_id,
            reply_markup=build_order_control_keyboard(bid, in_template=True),
        )
    except Exception:
        pass


async def schedule_expiration(context, bid: str, ttl_min: int):
    if hasattr(context, 'job_queue') and context.job_queue:
        context.job_queue.run_once(expire_job, when=timedelta(minutes=ttl_min), data={"bid": bid}, name=f"expire:{bid}")

async def schedule_auto_delete(context, bid: str):
    """Планирует автоматическое удаление истёкшей заявки через 5 минут"""
    if hasattr(context, 'job_queue') and context.job_queue:
        context.job_queue.run_once(delete_expired_order_job, when=timedelta(minutes=5), data={"bid": bid}, name=f"autodelete:{bid}")

def cancel_auto_delete_job(context, bid: str):
    """Отменяет запланированное автоудаление заявки"""
    if hasattr(context, 'job_queue') and context.job_queue:
        try:
            jobs = context.job_queue.get_jobs_by_name(f"autodelete:{bid}")
            for job in jobs:
                job.schedule_removal()
        except Exception:
            pass

async def _close_order_from_chats(bid: str, state: dict, bot, skip_bot_msg: bool = False) -> None:
    """Удаляет сообщения заявки из всех групповых чатов, из личного чата бота и убирает её из стейта."""
    order = state["orders"].get(bid)
    if not order:
        logger.error(f"[_close_order_from_chats] order {bid[:8]} NOT in state — already removed")
        return
    msgs = order.get("messages", [])
    logger.error(f"[_close_order_from_chats] bid={bid[:8]} messages={len(msgs)} bot_msg={order.get('bot_message_id')} bot_chat={order.get('bot_chat_id')}")
    for msg in msgs:
        try:
            await bot.delete_message(
                chat_id=msg["chat_id"],
                message_id=msg["message_id"]
            )
            logger.error(f"  ✅ deleted group msg {msg['message_id']} in chat {msg['chat_id']}")
        except Exception as e:
            logger.error(f"  ⚠️ delete group msg {msg['message_id']} chat {msg['chat_id']} failed: {e}")
    bot_message_id = order.get("bot_message_id")
    bot_chat_id = order.get("bot_chat_id")
    if bot_message_id and bot_chat_id and not skip_bot_msg:
        try:
            await bot.delete_message(chat_id=bot_chat_id, message_id=bot_message_id)
            logger.error(f"  ✅ deleted bot msg {bot_message_id} in chat {bot_chat_id}")
        except Exception as e:
            logger.error(f"  ⚠️ delete bot msg {bot_message_id} chat {bot_chat_id} failed: {e}")
    del state["orders"][bid]
    save_state(state)
    logger.error(f"[_close_order_from_chats] order {bid[:8]} removed from state")


async def _auto_close_confirmed_order_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фолбэк: удаляет заявку через 15 мин после подтверждения, если мерчант не оценил."""
    bid = context.job.data["bid"]
    state = load_state()
    if bid not in state["orders"]:
        return  # Уже удалена (мерчант поставил оценку)
    await _close_order_from_chats(bid, state, context.bot)


async def _force_close_claimed_expired_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Принудительно закрывает заявку через 10 мин после TTL если никто не среагировал."""
    bid = context.job.data["bid"]
    state = load_state()
    order = state["orders"].get(bid)
    if not order:
        return
    if order.get("closed"):
        return

    creator_id = order.get("creator_id")
    partner_info = order.get("claimed_by") or {}
    partner_id = partner_info.get("id")

    await _close_order_from_chats(bid, state, context.bot)

    if creator_id:
        try:
            await context.bot.send_message(
                chat_id=creator_id,
                text=f"🗑️ Заявка №{short_id(bid)} принудительно закрыта (10 мин без действий)."
            )
        except Exception:
            pass

    if partner_id:
        try:
            await context.bot.send_message(
                chat_id=partner_id,
                text=(
                    f"🗑️ Заявка №{short_id(bid)} закрыта автоматически.\n"
                    f"Если перевод был сделан — свяжитесь с мерчантом в личке."
                )
            )
        except Exception:
            pass


async def delete_expired_order_job(ctx):
    """Удаляет истёкшую заявку из всех чатов через 5 минут после истечения"""
    bid = ctx.job.data["bid"]
    state = load_state()
    order = state["orders"].get(bid)

    logger.error(f"[autodelete] bid={bid[:8]} order_found={order is not None} expired={order.get('expired') if order else 'N/A'} claimed={bool(order.get('claimed_by')) if order else 'N/A'}")

    if not order:
        return
    if not order.get("expired"):
        return
    if order.get("closed"):
        return

    creator_id = order.get("creator_id")

    if not order.get("claimed_by"):
        # Свободная заявка — удаляем через единую функцию (правильные chat_id, bot_message_id)
        await _close_order_from_chats(bid, state, ctx.bot)
    else:
        # Взятая заявка — удаляем из чатов, но оставляем в стейте с флагом
        for msg in order.get("messages", []):
            try:
                await ctx.bot.delete_message(
                    chat_id=msg["chat_id"],
                    message_id=msg["message_id"]
                )
            except Exception:
                pass
        bot_message_id = order.get("bot_message_id")
        bot_chat_id = order.get("bot_chat_id")
        if bot_message_id and bot_chat_id:
            try:
                await ctx.bot.delete_message(chat_id=bot_chat_id, message_id=bot_message_id)
            except Exception:
                pass
        order["auto_deleted"] = True
        save_state(state)

    if creator_id and TELEGRAM_AVAILABLE:
        try:
            await ctx.bot.send_message(
                chat_id=creator_id,
                text=f"🗑️ Заявка №{short_id(bid)} автоматически удалена из всех чатов (прошло 5 минут после истечения срока).",
                parse_mode=safe_parse_mode()
            )
        except Exception:
            pass

async def expire_job(ctx):
    bid = ctx.job.data["bid"]
    state = load_state()
    order = state["orders"].get(bid)
    if not order or order.get("expired") or order.get("closed"): 
        return
    
    order["expired"] = True
    save_state(state)
    
    # Проверяем, находится ли заявка "в работе"
    is_claimed = order.get("claimed_by") is not None
    
    if is_claimed:
        # Заявка "в работе" - обновляем сообщения в чатах без кнопок, но оставляем статус "🟡 В работе"
        for msg in order.get("messages", []):
            try:
                await edit_message_safe(
                    ctx.bot,
                    chat_id=msg["chat_id"],
                    message_id=msg["message_id"],
                    text=render_message(bid, state, msg["chat_id"], None),
                    parse_mode=safe_parse_mode(),
                    reply_markup=None  # Удаляем все кнопки
                )
            except Exception:
                pass

        # Уведомляем партнёра
        partner_info = order.get("claimed_by") or {}
        partner_id = partner_info.get("id")
        if partner_id and TELEGRAM_AVAILABLE:
            try:
                await ctx.bot.send_message(
                    chat_id=partner_id,
                    text=(
                        f"⏳ Время по заявке №{short_id(bid)} истекло.\n"
                        f"Если вы уже сделали перевод — нажмите Выполнено.\n"
                        f"Если нет — свяжитесь с мерчантом в личке."
                    )
                )
            except Exception:
                pass

        # Отправляем уведомление создателю с кнопкой "Выполнена"
        creator_id = order.get("creator_id")
        if creator_id and TELEGRAM_AVAILABLE:
            try:
                expire_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Выполнена", callback_data=f"confirm|{bid}|{partner_id}")]
                ])
                reply_to = order.get("bot_message_id")
                await ctx.bot.send_message(
                    chat_id=creator_id,
                    text=f"⏳ Заявка №{short_id(bid)} истекла по времени (в работе).\n⏱️ Автозакрытие через 10 минут.",
                    reply_markup=expire_keyboard,
                    parse_mode=safe_parse_mode(),
                    reply_to_message_id=reply_to if reply_to else None
                )
            except Exception:
                pass

        # Принудительное закрытие через 10 минут если никто не среагировал
        if hasattr(ctx, 'job_queue') and ctx.job_queue:
            ctx.job_queue.run_once(
                _force_close_claimed_expired_job,
                when=600,
                data={"bid": bid},
                name=f"forceclose:{bid}"
            )
    else:
        # Обычная логика для свободных заявок
        # Обновляем сообщения в чатах - меняем статус и удаляем кнопки
        for msg in order.get("messages", []):
            try:
                await edit_message_safe(
                    ctx.bot,
                    chat_id=msg["chat_id"], 
                    message_id=msg["message_id"],
                    text=render_message(bid, state, msg["chat_id"], None),
                    parse_mode=safe_parse_mode(),
                    reply_markup=None  # Удаляем все кнопки
                )
            except Exception: 
                pass
        
        # Отправляем уведомление создателю заявки
        creator_id = order.get("creator_id")
        if creator_id and TELEGRAM_AVAILABLE:
            try:
                expire_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("↻ Переопубликовать", callback_data=f"repub:{bid}")],
                    [InlineKeyboardButton("❌ Закрыть", callback_data=f"close_expired:{bid}")]
                ])
                
                # Отправляем уведомление ответом на исходную заявку
                reply_to = order.get("bot_message_id")
                await ctx.bot.send_message(
                    chat_id=creator_id,
                    text=f"⌛️ Заявка №{short_id(bid)} истекла.\n⏱️ Автоудаление через 5 минут.",
                    reply_markup=expire_keyboard,
                    parse_mode=safe_parse_mode(),
                    reply_to_message_id=reply_to if reply_to else None
                )
            except Exception:
                pass
        
        await schedule_auto_delete(ctx, bid)

async def on_callback(update: Update, context):
    state = load_state()
    q = update.callback_query

    uid = q.from_user.id
    data = q.data or ""

    # Hard ban — blocked users cannot interact with any button
    if is_user_banned(uid, state):
        await q.answer("❌ Вы заблокированы.", show_alert=True)
        return

    # Handle done/confirm/dispute/rate callbacks (new deal flow)
    if data.startswith("done_"):
        await q.answer()
        await handle_done(update, context, state)
        return
    if data.startswith("confirm|"):
        await q.answer()
        await handle_confirm(update, context, state)
        return
    if data.startswith("dispute|"):
        await q.answer()
        await handle_dispute(update, context, state)
        return
    if data.startswith("rate|"):
        await q.answer()
        await handle_rate(update, context, state)
        return

    # Handle order creation flow (only creation-flow callbacks, not editing_amount)
    SESSION_PREFIXES = ("dir:", "bank:", "rate:", "ttl:", "chat:", "payments:")
    is_creation_session = uid in user_sessions and user_sessions[uid].get("step") is not None
    if is_creation_session and (any(data.startswith(p) for p in SESSION_PREFIXES) or data == "back"):
        await handle_order_creation(update, context, state)
        return

    # Отвечаем на callback query только для действий с заявками
    try:
        await q.answer()
    except Exception:
        pass  # Игнорируем ошибки timeout или устаревших query

    if data.startswith("tpl:"):
        await handle_template_callback(update, context, state, data)
        return

    # Обрабатываем кнопку назад при редактировании суммы
    if data.startswith("back_to_control:") and uid in user_sessions and user_sessions[uid].get("state") == "editing_amount":
        bid = data.replace("back_to_control:", "")
        del user_sessions[uid]
        
        # Возвращаемся к управлению заявкой
        control_keyboard = build_order_control_keyboard(bid)
        
        await q.edit_message_text(
            "❌ Редактирование суммы отменено.",
            reply_markup=control_keyboard
        )
        return

    # close_done:{bid}|{partner_id}
    if data.startswith("close_done:"):
        rest = data[len("close_done:"):]
        if "|" in rest:
            bid, partner_id_str = rest.split("|", 1)
            try:
                partner_id = int(partner_id_str)
            except ValueError:
                await q.answer("❌ Ошибка данных.")
                return
            await handle_close_done(update, context, state, bid, partner_id)
        return

    # close_cancel:{bid}
    if data.startswith("close_cancel:"):
        bid = data[len("close_cancel:"):]
        await handle_close_cancel(update, context, state, bid)
        return

    # Handle order actions (claim, unclaim, remind, close, remind_bot, edit_amount, send_remaining, send_all_remaining, send_to_chat, back_to_control, repub, close_expired, close_claimed_expired)
    m = re.match(r"^(claim|unclaim|remind|close|remind_bot|edit_amount|send_remaining|send_all_remaining|send_to_chat|back_to_control|repub|close_expired|close_claimed_expired):(.+)$", data)
    if not m: 
        return
    
    action, rest = m.group(1), m.group(2)
    
    # Для send_to_chat нужно извлечь bid и chat_id отдельно
    if action == "send_to_chat":
        parts = rest.split(":")
        if len(parts) >= 2:
            bid = parts[0]
            try:
                chat_id = int(parts[1])
            except ValueError:
                await q.answer("❌ Ошибка в данных кнопки!")
                return
        else:
            await q.answer("❌ Неверные данные кнопки!")
            return
    else:
        bid = rest
    
    order = state["orders"].get(bid)

    if not order:
        try:
            await q.edit_message_text("❌ Заявка не найдена.")
        except Exception:
            pass
        return

    if action == "remind":
        await handle_remind_order(update, context, state, bid)
        return
    
    if action == "close":
        await handle_close_order(update, context, state, bid)
        return
    
    if action == "remind_bot":
        await handle_remind_from_bot(update, context, state, bid)
        return
    
    if action == "edit_amount":
        await handle_edit_amount(update, context, state, bid)
        return

    if action == "extend":
        await handle_extend_ttl(update, context, state, bid)
        return

    if action == "send_remaining":
        await handle_send_remaining(update, context, state, bid)
        return
    
    if action == "send_all_remaining":
        await handle_send_all_remaining(update, context, state, bid)
        return
    
    if action == "send_to_chat":
        try:
            await handle_send_to_chat(update, context, state, bid, chat_id)
        except Exception as e:
            await q.answer(f"❌ Ошибка отправки: {str(e)}")
        return
    
    if action == "back_to_control":
        await handle_back_to_control(update, context, state, bid)
        return
    
    if action == "repub":
        await handle_republish_order(update, context, state, bid)
        return
    
    if action == "close_expired":
        await handle_close_expired_order(update, context, state, bid)
        return
    
    if action == "close_claimed_expired":
        await handle_close_claimed_expired_order(update, context, state, bid)
        return
    
    if order.get("expired"): 
        await q.answer("Срок заявки истёк.", show_alert=True)
        return
    
    user = q.from_user
    chat_id = q.message.chat.id
    
    if action == "claim":
        if order.get("claimed_by"): 
            await q.answer("Уже взяли.")
            return
        
        # Проверяем, что пользователь не создатель заявки
        creator_id = order.get("creator_id")
        if creator_id == user.id:
            username = user.username
            mention = f"@{username}" if username else user.first_name or "Пользователь"
            error_msg = await q.message.reply_text(f"{mention}, вы не можете взять в работу свою же заявку.")
            
            # Удаляем сообщение через 3 минуты
            if hasattr(context, 'job_queue') and context.job_queue:
                context.job_queue.run_once(
                    delete_message_job, 
                    180,  # 3 минуты
                    data={"chat_id": q.message.chat.id, "message_id": error_msg.message_id}
                )
            return
        
        taken_at = now_utc()
        order["claimed_by"] = {
            "id": user.id,
            "name": human_name(user),
            "username": getattr(user, 'username', None),
            "ts": taken_at,
            "chat_id": chat_id
        }
        order["taken_at"] = taken_at

        # Счётчик взятых заявок для completion rate
        increment_partner_taken(state, user.id)

        # Отправляем уведомление в чат где взяли заявку
        username = user.username
        mention = f"@{username}" if username else escape_html(human_name(user))
        try:
            claim_msg = await q.message.reply_text(f"{mention} взял заявку в работу ⚡️")

            # Удаляем сообщение через 3 минуты
            if hasattr(context, 'job_queue') and context.job_queue:
                context.job_queue.run_once(
                    delete_message_job,
                    180,
                    data={"chat_id": q.message.chat.id, "message_id": claim_msg.message_id}
                )
        except Exception:
            pass

        # Notify creator with deeplink to partner
        creator_id = order.get("creator_id")
        partner_username = getattr(user, 'username', None)
        if creator_id and TELEGRAM_AVAILABLE and context:
            try:
                deeplink_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        f"💬 Написать @{partner_username}",
                        url=f"https://t.me/{partner_username}"
                    )]
                ]) if partner_username else None

                await send_message_safe(
                    context.bot,
                    chat_id=creator_id,
                    text=f"✅ Заявку #{short_id(bid)} взял в работу: {escape_html(human_name(user))}",
                    parse_mode=safe_parse_mode(),
                    reply_markup=deeplink_keyboard
                )
            except Exception:
                pass

        # Отправляем партнёру панель управления с кнопкой "Выполнено"
        if TELEGRAM_AVAILABLE and context:
            order_num = short_id(bid)
            amount = order.get("amount", "")
            bank = order.get("bank", "")
            direction = order.get("direction", "")
            creator_username = order.get("creator_username", "мерчант")

            partner_panel_text = (
                f"✅ Ты взял заявку #{order_num} в работу\n"
                f"💰 {amount} ₽ · {escape_html(str(bank))} · {escape_html(str(direction))}\n"
                f"👤 Мерчант: @{escape_html(str(creator_username))}"
            )
            partner_panel_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Выполнено", callback_data=f"done_{bid}"),
                    InlineKeyboardButton("↩️ Освободить", callback_data=f"unclaim:{bid}"),
                ]
            ])
            try:
                partner_panel_msg = await context.bot.send_message(
                    chat_id=user.id,
                    text=partner_panel_text,
                    reply_markup=partner_panel_keyboard
                )
                order["partner_panel_msg_id"] = partner_panel_msg.message_id
            except Exception:
                pass
    
    elif action == "unclaim":
        claimer = order.get("claimed_by")
        if not claimer: 
            await q.answer("Уже свободна.")
            return
        
        creator_id = order.get("creator_id")
        if user.id != claimer.get("id") and not is_admin(user.id, state) and user.id != creator_id:
            # Отправляем сообщение об ошибке с автоудалением через 1 минуту
            error_msg = await q.message.reply_text(f"❌ @{escape_html(user.username) if user.username else escape_html(human_name(user))} попытался освободить заявку, но может это делать только исполнитель или создатель или админ.")
            
            # Удаляем сообщение об ошибке через 1 минуту
            if hasattr(context, 'job_queue') and context.job_queue:
                context.job_queue.run_once(
                    delete_message_job, 
                    60,  # 1 минута
                    data={"chat_id": q.message.chat.id, "message_id": error_msg.message_id}
                )
            await q.answer("Нет доступа", show_alert=False)
            return
        
        # Удаляем панель управления у партнёра (кнопки «Выполнено» и «Освободить»)
        partner_panel_msg_id = order.get("partner_panel_msg_id")
        partner_id = claimer.get("id")
        if partner_panel_msg_id and partner_id and TELEGRAM_AVAILABLE and context:
            try:
                await context.bot.edit_message_text(
                    chat_id=partner_id,
                    message_id=partner_panel_msg_id,
                    text=(
                        f"↩️ Ты освободил заявку #{short_id(bid)}\n"
                        f"💰 {order.get('amount', '')} ₽ · {escape_html(str(order.get('bank', '')))}"
                    ),
                    reply_markup=None
                )
            except Exception:
                pass
        order["partner_panel_msg_id"] = None

        order["claimed_by"] = None

        # Уведомляем создателя заявки что она освобождена
        creator_id = order.get("creator_id")
        unclaimer_username = user.username
        unclaimer_mention = f"@{unclaimer_username}" if unclaimer_username else escape_html(human_name(user))
        if creator_id and creator_id != user.id and TELEGRAM_AVAILABLE and context:
            try:
                await context.bot.send_message(
                    chat_id=creator_id,
                    text=(
                        f"↩️ Заявка #{short_id(bid)} освобождена\n"
                        f"{unclaimer_mention} освободил вашу заявку."
                    ),
                    parse_mode=safe_parse_mode()
                )
            except Exception:
                pass

        # Уведомляем во всех чатах что заявка снова свободна
        await send_release_notification(context, bid, order, state)
    
    save_state(state)
    
    # Update all messages
    for msg in order.get("messages", []):
        try:
            if TELEGRAM_AVAILABLE and context:
                result = await edit_message_safe(
                    context.bot,
                    chat_id=msg["chat_id"], 
                    message_id=msg["message_id"],
                    text=render_message(bid, state, msg["chat_id"], uid), 
                    reply_markup=build_keyboard(bid, state, msg["chat_id"], uid),
                    parse_mode=safe_parse_mode(),
                    disable_web_page_preview=True
                )
                # Если чат мигрировал — обновляем сохранённый chat_id и реестр чатов
                if result and result.chat.id != msg["chat_id"]:
                    _migrate_chat_id(state, msg["chat_id"], result.chat.id)
                    msg["chat_id"] = result.chat.id
        except Exception as e:
            print(f"  ⚠️ Failed to update message in chat {msg['chat_id']}: {e}")
    # Сохраняем обновлённые chat_id (на случай миграции чата)
    save_state(state)

# ─── Job helpers for auto-delete ──────────────────────────────────────────

async def _delete_msg_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await context.bot.delete_message(
            context.job.data["chat_id"],
            context.job.data["msg_id"]
        )
    except Exception:
        pass

async def _remove_reply_markup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=context.job.data["chat_id"],
            message_id=context.job.data["msg_id"],
            reply_markup=None
        )
    except Exception:
        pass

# ─── New callbacks: done / confirm / dispute / rate ────────────────────────

async def handle_done(update: Update, context, state: Dict[str, Any]) -> None:
    """Партнёр нажал 'Выполнено' — отправляем мерчанту запрос подтверждения."""
    q = update.callback_query
    bid = q.data.replace("done_", "", 1)
    order = state["orders"].get(bid)
    if not order:
        await q.edit_message_text("❌ Заявка не найдена.")
        return

    partner_username = getattr(q.from_user, 'username', None) or f"id{q.from_user.id}"
    merchant_id = order.get("creator_id")

    confirm_text = (
        f"🔔 @{partner_username} говорит что выполнил заявку #{short_id(bid)}\n"
        f"💰 {order.get('amount', '')} ₽ · {escape_html(str(order.get('bank', '')))}"
    )
    confirm_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm|{bid}|{q.from_user.id}"),
            InlineKeyboardButton("❌ Оспорить", callback_data=f"dispute|{bid}|{q.from_user.id}"),
        ]
    ])
    try:
        await context.bot.send_message(
            chat_id=merchant_id,
            text=confirm_text,
            reply_markup=confirm_keyboard
        )
    except Exception:
        await q.edit_message_text("❌ Не удалось связаться с мерчантом.")
        return

    # Временное уведомление партнёру — удаляем через 3 мин
    wait_msg = await context.bot.send_message(
        chat_id=q.from_user.id,
        text="⏳ Ожидаем подтверждения от мерчанта..."
    )
    if context.job_queue:
        context.job_queue.run_once(
            _delete_msg_job, when=180,
            data={"chat_id": wait_msg.chat_id, "msg_id": wait_msg.message_id}
        )

    await q.edit_message_reply_markup(reply_markup=None)

    # Запланировать напоминание мерчанту через 5 мин и авто-подтверждение через 15 мин
    if context.job_queue:
        context.job_queue.run_once(
            _remind_merchant_confirm_job,
            when=300,
            data={"bid": bid, "partner_id": q.from_user.id},
            name=f"remind_confirm:{bid}"
        )
        context.job_queue.run_once(
            _auto_confirm_job,
            when=900,
            data={"bid": bid, "partner_id": q.from_user.id},
            name=f"auto_confirm:{bid}"
        )


async def _do_confirm_deal(
    bid: str,
    partner_id: int,
    merchant_id: int,
    state: dict,
    bot,
    job_queue,
    merchant_chat_id=None,
    merchant_msg_id=None,
) -> None:
    """Ядро логики подтверждения сделки — используется и вручную и авто."""
    order = state["orders"].get(bid)
    if not order:
        return

    closed_at = now_utc()
    taken_at = order.get("taken_at", closed_at)

    record_deal(
        state,
        order_id=bid,
        merchant_id=merchant_id,
        partner_id=partner_id,
        amount=order.get("amount", 0),
        direction=order.get("direction", ""),
        taken_at=taken_at,
        closed_at=closed_at,
    )
    save_state(state)

    partner_username = order.get("claimed_by", {}).get("username") or f"id{partner_id}"
    rating_text = f"✅ Сделка подтверждена!\nОцените @{partner_username}:"
    rating_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⭐️{i}", callback_data=f"rate|{partner_id}|{bid}|{i}")
        for i in range(1, 6)
    ]])

    if merchant_chat_id and merchant_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=merchant_chat_id,
                message_id=merchant_msg_id,
                text=rating_text,
                reply_markup=rating_keyboard
            )
        except Exception:
            await bot.send_message(
                chat_id=merchant_id,
                text=rating_text,
                reply_markup=rating_keyboard
            )
    else:
        await bot.send_message(
            chat_id=merchant_id,
            text=rating_text,
            reply_markup=rating_keyboard
        )

    try:
        ok_msg = await bot.send_message(
            chat_id=partner_id,
            text=f"✅ Мерчант подтвердил сделку по заявке #{short_id(bid)}"
        )
        if job_queue:
            job_queue.run_once(
                _delete_msg_job, when=180,
                data={"chat_id": ok_msg.chat_id, "msg_id": ok_msg.message_id}
            )
    except Exception as e:
        logger.warning("Не удалось уведомить партнёра %s: %s", partner_id, e)

    if job_queue:
        job_queue.run_once(
            _auto_close_confirmed_order_job, when=900,
            data={"bid": bid},
            name=f"autoclose_confirmed:{bid}"
        )
        jobs = job_queue.get_jobs_by_name(f"forceclose:{bid}")
        for job in jobs:
            job.schedule_removal()


async def _remind_merchant_confirm_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Напоминает мерчанту через 5 мин что партнёр ждёт подтверждения."""
    data = context.job.data
    bid, partner_id = data["bid"], data["partner_id"]
    state = load_state()
    order = state["orders"].get(bid)
    if not order or not order.get("claimed_by"):
        return

    merchant_id = order.get("creator_id")
    partner_info = order.get("claimed_by") or {}
    partner_username = partner_info.get("username") or f"id{partner_id}"

    if merchant_id:
        try:
            await context.bot.send_message(
                chat_id=merchant_id,
                text=(
                    f"🔔 Напоминание: @{partner_username} ждёт вашего подтверждения "
                    f"по заявке №{short_id(bid)}.\n"
                    f"Через 10 минут сделка будет подтверждена автоматически."
                )
            )
        except Exception:
            pass


async def _auto_confirm_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Авто-подтверждение сделки через 15 мин если мерчант не среагировал."""
    data = context.job.data
    bid, partner_id = data["bid"], data["partner_id"]
    state = load_state()
    order = state["orders"].get(bid)
    if not order:
        return

    merchant_id = order.get("creator_id")
    await _do_confirm_deal(
        bid=bid,
        partner_id=partner_id,
        merchant_id=merchant_id,
        state=state,
        bot=context.bot,
        job_queue=context.job_queue,
        merchant_chat_id=None,
        merchant_msg_id=None,
    )


async def handle_confirm(update: Update, context, state: Dict[str, Any]) -> None:
    """Мерчант подтвердил выполнение — записываем сделку, просим оценку."""
    q = update.callback_query
    _, bid, partner_id_str = q.data.split("|", 2)
    partner_id = int(partner_id_str)

    # Отменяем напоминание и авто-подтверждение
    if context.job_queue:
        for jname in [f"remind_confirm:{bid}", f"auto_confirm:{bid}"]:
            jobs = context.job_queue.get_jobs_by_name(jname)
            for job in jobs:
                job.schedule_removal()

    fresh_state = load_state()
    if not fresh_state["orders"].get(bid):
        await q.edit_message_text("❌ Заявка не найдена.")
        return

    await _do_confirm_deal(
        bid=bid,
        partner_id=partner_id,
        merchant_id=q.from_user.id,
        state=fresh_state,
        bot=context.bot,
        job_queue=context.job_queue,
        merchant_chat_id=q.message.chat_id,
        merchant_msg_id=q.message.message_id,
    )


async def handle_dispute(update: Update, context, state: Dict[str, Any]) -> None:
    """Мерчант оспорил — уведомляем партнёра, возвращаем кнопку 'Выполнено'."""
    q = update.callback_query
    _, bid, partner_id_str = q.data.split("|", 2)
    partner_id = int(partner_id_str)

    # Отменяем напоминание и авто-подтверждение
    if context.job_queue:
        for jname in [f"remind_confirm:{bid}", f"auto_confirm:{bid}"]:
            jobs = context.job_queue.get_jobs_by_name(jname)
            for job in jobs:
                job.schedule_removal()

    order = state["orders"].get(bid)
    partner_username = (
        order.get("claimed_by", {}).get("username") or f"id{partner_id}"
    ) if order else f"id{partner_id}"

    await q.edit_message_text(
        f"❌ Сделка оспорена. Свяжитесь с @{partner_username} в личке."
    )

    try:
        dispute_msg = await context.bot.send_message(
            chat_id=partner_id,
            text=f"❌ Мерчант оспорил сделку по заявке #{short_id(bid)}.\nСвяжитесь в личке для разбора."
        )
        if context.job_queue:
            context.job_queue.run_once(
                _delete_msg_job, when=180,
                data={"chat_id": dispute_msg.chat_id, "msg_id": dispute_msg.message_id}
            )
    except Exception as e:
        logger.warning("Не удалось уведомить партнёра %s об оспаривании: %s", partner_id, e)

    if order:
        partner_panel_msg_id = order.get("partner_panel_msg_id")
        if partner_panel_msg_id:
            try:
                panel_keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Выполнено", callback_data=f"done_{bid}"),
                        InlineKeyboardButton("↩️ Освободить", callback_data=f"unclaim:{bid}"),
                    ]
                ])
                await context.bot.edit_message_reply_markup(
                    chat_id=partner_id,
                    message_id=partner_panel_msg_id,
                    reply_markup=panel_keyboard
                )
            except Exception:
                pass


async def handle_rate(update: Update, context, state: Dict[str, Any]) -> None:
    """Мерчант поставил оценку партнёру."""
    q = update.callback_query
    parts = q.data.split("|")

    if len(parts) == 4:
        # Новый формат: rate|partner_id|bid|score
        _, partner_id_str, bid, score_str = parts
        partner_id, score = int(partner_id_str), int(score_str)
    else:
        # Старый формат: rate|partner_id|score (backward compat)
        _, partner_id_str, score_str = parts
        partner_id, score = int(partner_id_str), int(score_str)
        bid = None

    record_rating(state, partner_id, score)
    save_state(state)

    await q.edit_message_text(f"✅ Оценка {score}⭐️ сохранена!")

    if context.job_queue:
        context.job_queue.run_once(
            _delete_msg_job, when=180,
            data={"chat_id": q.message.chat_id, "msg_id": q.message.message_id}
        )

    # Удаляем заявку из чатов и стейта (только новый формат)
    if bid:
        if context.job_queue:
            jobs = context.job_queue.get_jobs_by_name(f"autoclose_confirmed:{bid}")
            for job in jobs:
                job.schedule_removal()
        fresh_state = load_state()
        await _close_order_from_chats(bid, fresh_state, context.bot)

# ─── /partner command ──────────────────────────────────────────────────────

async def cmd_partner(update: Update, context) -> None:
    """/partner @username или /partner <user_id> — карточка партнёра."""
    if not check_private_chat_only(update):
        return
    state = load_state()
    user_id = update.effective_user.id
    if not await check_not_banned(update, state):
        return
    if not (is_merchant(user_id, state) or is_admin(user_id, state)):
        await update.message.reply_text("❌ Команда недоступна.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Использование: /partner @username или /partner <user_id>")
        return

    target = args[0].lstrip("@")
    target_id = int(target) if target.isdigit() else find_user_by_username(state, target)
    target_username = target

    if not target_id:
        await update.message.reply_text(
            f"❌ Пользователь @{escape_html(target)} не найден в боте.",
            parse_mode=safe_parse_mode()
        )
        return

    r = get_partner_rating(state, target_id)
    total = r["total_deals"]
    taken = r["total_taken"]
    avg = r["avg_score"]
    avg_time = r["avg_completion_minutes"]
    scores_count = len(r["scores"])

    completion = f"✅ {round(total / taken * 100)}%" if taken > 0 else "—"
    avg_str = f"⭐️ {avg} (на основе {scores_count} оценок)" if avg is not None else "Оценок пока нет"
    time_str = f"⚡️ {avg_time} мин" if avg_time is not None else "—"

    text = (
        f"👤 @{escape_html(target_username)}\n"
        f"Выполнено сделок: {total}\n"
        f"Completion rate: {completion}\n"
        f"Среднее время: {time_str}\n"
        f"Средняя оценка: {avg_str}"
    )
    await update.message.reply_text(text, parse_mode=safe_parse_mode())

# ─── /history command ──────────────────────────────────────────────────────

async def cmd_history(update: Update, context) -> None:
    """/history — своя история. /history @username — для админа."""
    if not check_private_chat_only(update):
        return
    state = load_state()
    user_id = update.effective_user.id
    if not await check_not_banned(update, state):
        return
    args = context.args

    if args and is_admin(user_id, state):
        target = args[0].lstrip("@")
        target_username = target
        target_id = int(target) if target.isdigit() else find_user_by_username(state, target)
        if not target_id:
            await update.message.reply_text(
                f"❌ Пользователь @{escape_html(target)} не найден.",
                parse_mode=safe_parse_mode()
            )
            return
    elif args:
        await update.message.reply_text("❌ Только админы могут смотреть историю других пользователей.")
        return
    else:
        target_id = user_id
        target_username = getattr(update.effective_user, 'username', None) or str(user_id)

    posted = get_merchant_posted(state, target_id)
    r = get_partner_rating(state, target_id)

    as_merchant = [d for d in state["deals"] if d["merchant_id"] == target_id]
    as_partner = [d for d in state["deals"] if d["partner_id"] == target_id]

    lines = [f"📊 История @{escape_html(str(target_username))}\n"]

    if posted > 0 or as_merchant:
        lines.append("— Как мерчант —")
        lines.append(f"📤 Размещено заявок: {posted}")
        lines.append(f"✅ Успешных сделок: {len(as_merchant)}")
        if as_merchant:
            partner_counts = Counter(d["partner_id"] for d in as_merchant)
            top = partner_counts.most_common(5)
            partner_list = [f"@{lookup_username(state, pid)} ({cnt})" for pid, cnt in top]
            lines.append(f"👥 Партнёры: {', '.join(partner_list)}")
        lines.append("")

    if as_partner or r["total_taken"] > 0:
        lines.append("— Как партнёр —")
        lines.append(f"✅ Выполнено сделок: {r['total_deals']}")
        completion = f"{round(r['total_deals'] / r['total_taken'] * 100)}%" if r["total_taken"] > 0 else "—"
        lines.append(f"📈 Completion rate: {completion}")
        avg_time = r["avg_completion_minutes"]
        lines.append(f"⚡️ Среднее время: {f'{avg_time} мин' if avg_time else '—'}")
        if as_partner:
            merchant_counts = Counter(d["merchant_id"] for d in as_partner)
            top = merchant_counts.most_common(5)
            merchant_list = [f"@{lookup_username(state, mid)} ({cnt})" for mid, cnt in top]
            lines.append(f"🤝 Мерчанты: {', '.join(merchant_list)}")

    if not as_merchant and not as_partner and posted == 0:
        lines.append("Сделок пока нет.")

    await update.message.reply_text("\n".join(lines), parse_mode=safe_parse_mode())

# ─── /stats command ────────────────────────────────────────────────────────

async def templates_cmd(update: Update, context) -> None:
    """/templates — просмотр/переименование/удаление шаблонов."""
    if not check_private_chat_only(update):
        return
    state = load_state()
    uid = update.effective_user.id
    if not await check_not_banned(update, state):
        return
    if not can_create_orders(uid, state):
        await update.message.reply_text("❌ Команда для мерчантов и админов.")
        return
    await _render_templates_list(update.message.reply_text, state, uid)


async def _render_templates_list(send_func, state, uid):
    templates = templates_store.list_templates(state, uid)
    if not templates:
        await send_func("У тебя пока нет шаблонов.\nСоздай заявку и нажми «💾 В шаблон».")
        return
    lines = ["📋 <b>Твои шаблоны:</b>\n"]
    buttons = []
    for i, t in enumerate(templates):
        lines.append(f"{i + 1}. {escape_html(t['name'])} — {escape_html(str(t.get('direction', '')))}")
        # Имя — широкой кнопкой во всю ширину ряда (видно целиком, нажатие = переименовать),
        # удаление — отдельным рядом с номером из списка выше.
        buttons.append([InlineKeyboardButton(f"✏️ {t['name']}", callback_data=f"tpl:rename:{i}")])
        buttons.append([InlineKeyboardButton(f"🗑 Удалить №{i + 1}", callback_data=f"tpl:del:{i}")])
    await send_func(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), parse_mode=safe_parse_mode()
    )


async def cmd_stats(update: Update, context) -> None:
    """/stats — расширенная статистика для админов с топом партнёров."""
    if not check_private_chat_only(update):
        return
    state = load_state()
    user_id = update.effective_user.id
    if not await check_not_banned(update, state):
        return
    if not is_admin(user_id, state):
        await update.message.reply_text("❌ Команда только для админов.")
        return

    total_orders = len(state.get("orders", {}))
    active_orders = sum(1 for o in state["orders"].values() if not o.get("expired") and not o.get("claimed_by"))
    claimed_orders = sum(1 for o in state["orders"].values() if o.get("claimed_by") and not o.get("expired"))
    total_deals = len(state.get("deals", []))

    stats_text = (
        f"📊 <b>Статистика</b>\n\n"
        f"Всего заявок: {total_orders}\n"
        f"Активных: {active_orders}\n"
        f"В работе: {claimed_orders}\n"
        f"Завершённых сделок: {total_deals}\n"
    )

    ratings = state.get("ratings", {})
    if ratings:
        sorted_partners = sorted(
            ratings.items(),
            key=lambda x: x[1].get("total_deals", 0),
            reverse=True
        )[:10]

        top_lines = ["\n🏆 <b>Топ партнёров:</b>"]
        for i, (uid, r) in enumerate(sorted_partners, 1):
            uname = lookup_username(state, int(uid))
            deals = r.get("total_deals", 0)
            taken = r.get("total_taken", 0)
            avg = r.get("avg_score")
            completion = f"✅ {round(deals / taken * 100)}%" if taken > 0 else "—"
            score_str = f"⭐️ {avg}" if avg is not None else "—"
            top_lines.append(f"{i}. @{escape_html(str(uname))} — {deals} сделок · {completion} · {score_str}")

        stats_text += "\n".join(top_lines)

    await update.message.reply_text(stats_text, parse_mode=safe_parse_mode())

# ─── Merchant management commands ─────────────────────────────────────────

async def cmd_addmerchant(update: Update, context) -> None:
    """/addmerchant <user_id> — добавить мерчанта (только админы)."""
    if not check_private_chat_only(update):
        return
    state = load_state()
    uid = update.effective_user.id
    if not await check_not_banned(update, state):
        return
    if not is_admin(uid, state):
        await update.message.reply_text("❌ Только для админов.")
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /addmerchant <user_id>\nНапример: /addmerchant 123456789")
        return

    new_id = int(args[0])
    if new_id in set(state["merchants"]):
        await update.message.reply_text(f"ℹ️ {new_id} уже является мерчантом.")
        return

    state["merchants"].append(new_id)
    save_state(state)
    await update.message.reply_text(f"✅ Мерчант {new_id} добавлен.")
    await set_user_commands(context.bot, new_id, "merchant")


async def cmd_removemerchant(update: Update, context) -> None:
    """/removemerchant <user_id> — убрать мерчанта (только админы)."""
    if not check_private_chat_only(update):
        return
    state = load_state()
    uid = update.effective_user.id
    if not await check_not_banned(update, state):
        return
    if not is_admin(uid, state):
        await update.message.reply_text("❌ Только для админов.")
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /removemerchant <user_id>\nНапример: /removemerchant 123456789")
        return

    target_id = int(args[0])
    if target_id not in set(state["merchants"]):
        await update.message.reply_text(f"ℹ️ {target_id} не является мерчантом.")
        return

    state["merchants"] = [m for m in state["merchants"] if m != target_id]
    save_state(state)
    await update.message.reply_text(f"✅ Мерчант {target_id} удалён.")
    await set_user_commands(context.bot, target_id, "partner")


async def cmd_listmerchants(update: Update, context) -> None:
    """/merchants — список мерчантов (только админы)."""
    if not check_private_chat_only(update):
        return
    state = load_state()
    uid = update.effective_user.id
    if not await check_not_banned(update, state):
        return
    if not is_admin(uid, state):
        await update.message.reply_text("❌ Только для админов.")
        return

    merchants = state.get("merchants", [])
    if not merchants:
        await update.message.reply_text("Мерчантов пока нет.")
        return

    lines = [f"👤 {mid}" for mid in merchants]
    await update.message.reply_text("🛒 Мерчанты:\n" + "\n".join(lines))

# ─── End new commands ──────────────────────────────────────────────────────

async def handle_order_creation(update: Update, context, state: Dict[str, Any]):
    q = update.callback_query
    uid = q.from_user.id
    session = user_sessions[uid]
    data = q.data or ""

    if session["step"] == "direction":
        if data.startswith("dir:"):
            direction = "Куплю RUB - отдам USDT" if data == "dir:buy_rub" else "Продам RUB - возьму USDT"
            session["data"]["direction"] = direction
            session["step"] = "amount"
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="back")]
            ])
            await q.edit_message_text(
                "💰 Введите сумму в RUB (только число, например: 150000):",
                parse_mode=safe_parse_mode(),
                reply_markup=keyboard
            )
    
    elif session["step"] == "amount":
        if data == "back":
            # Возвращаемся к выбору направления
            session["step"] = "direction"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Куплю RUB - отдам USDT", callback_data="dir:buy_rub")],
                [InlineKeyboardButton("Продам RUB - возьму USDT", callback_data="dir:sell_rub")]
            ])
            
            await q.edit_message_text(
                "🏗 Создание заявки\n\nВыберите направление:",
                reply_markup=keyboard
            )
            await q.answer()
    
    elif session["step"] == "bank":
        if data == "bank:next":
            # Переходим к выбору количества платежей
            session["data"]["bank"] = " + ".join(session["data"]["banks"]) 
            session["step"] = "payments"
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 1 платеж", callback_data="payments:1")],
                [InlineKeyboardButton("💰 До 2 платежей", callback_data="payments:2")],
                [InlineKeyboardButton("💰 До 3 платежей", callback_data="payments:3")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back")]
            ])
            
            await q.edit_message_text(
                "💳 В сколько платежей:",
                reply_markup=keyboard
            )
            await q.answer()
        elif data.startswith("bank:"):
            banks = {
                "bank:sber": "СБЕР",
                "bank:tinkoff": "ТИНЬКОФФ", 
                "bank:alfa": "АЛЬФА",
                "bank:sbp": "СБП"
            }
            
            # Инициализируем список банков если его нет
            if "banks" not in session["data"]:
                session["data"]["banks"] = []
            
            selected_bank = banks.get(data, "")
            if selected_bank:
                if selected_bank in session["data"]["banks"]:
                    # Убираем банк если он уже выбран
                    session["data"]["banks"].remove(selected_bank)
                else:
                    # Добавляем банк если его нет
                    session["data"]["banks"].append(selected_bank)
                    # Ограничиваем максимум 2 банка
                    if len(session["data"]["banks"]) > 2:
                        session["data"]["banks"] = session["data"]["banks"][-2:]
            
            # Обновляем клавиатуру с отметками выбранных банков
            selected_banks = session["data"]["banks"]
            buttons = []
            for bank_key, bank_name in banks.items():
                mark = "✅ " if bank_name in selected_banks else ""
                buttons.append([InlineKeyboardButton(f"{mark}{bank_name}", callback_data=bank_key)])
            
            # Кнопки управления
            control_buttons = []
            if len(selected_banks) > 0:
                control_buttons.append([InlineKeyboardButton("➡️ Далее", callback_data="bank:next")])
            control_buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
            
            keyboard = InlineKeyboardMarkup(buttons + control_buttons)
            
            bank_text = f"Выбрано банков: {len(selected_banks)}/2\n" + (f"📝 {', '.join(selected_banks)}" if selected_banks else "")
            
            try:
                await q.edit_message_text(
                    f"🏦 Выберите банк(и) - можно до 2х:\n\n{bank_text}",
                    reply_markup=keyboard
                )
            except Exception as e:
                if "not modified" in str(e).lower():
                    # Сообщение не изменилось, просто отвечаем на callback
                    await q.answer()
                else:
                    # Другая ошибка, пробуем без изменения текста
                    await q.answer("Обновлено!")
        elif data == "back":
            session["step"] = "amount"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="back")]
            ])
            await q.edit_message_text(
                "💰 Введите сумму в RUB (только число, например: 150000):",
                parse_mode=safe_parse_mode(),
                reply_markup=keyboard
            )
            await q.answer()
    
    elif session["step"] == "payments":
        if data.startswith("payments:"):
            if data == "payments:1":
                session["data"]["payments"] = "1 платеж"
            elif data == "payments:2":
                session["data"]["payments"] = "до 2 платежей"
            else:  # payments:3
                session["data"]["payments"] = "до 3 платежей"
            
            session["step"] = "rate"
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Байбит средний", callback_data="rate:bybit")],
                [InlineKeyboardButton("Курс Grinex", callback_data="rate:grinex")],
                [InlineKeyboardButton("Установить свой", callback_data="rate:custom")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back")]
            ])
            
            await q.edit_message_text(
                "📈 Выберите курс:",
                reply_markup=keyboard
            )
            await q.answer()
        elif data == "back":
            session["step"] = "bank"
            # Восстанавливаем интерфейс выбора банков
            banks = {
                "bank:sber": "СБЕР",
                "bank:tinkoff": "ТИНЬКОФФ", 
                "bank:alfa": "АЛЬФА",
                "bank:sbp": "СБП"
            }
            
            selected_banks = session["data"].get("banks", [])
            buttons = []
            for bank_key, bank_name in banks.items():
                mark = "✅ " if bank_name in selected_banks else ""
                buttons.append([InlineKeyboardButton(f"{mark}{bank_name}", callback_data=bank_key)])
            
            control_buttons = []
            if len(selected_banks) > 0:
                control_buttons.append([InlineKeyboardButton("➡️ Далее", callback_data="bank:next")])
            control_buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
            
            keyboard = InlineKeyboardMarkup(buttons + control_buttons)
            bank_text = f"Выбрано банков: {len(selected_banks)}/2\n" + (f"📝 {', '.join(selected_banks)}" if selected_banks else "")
            
            await q.edit_message_text(
                f"🏦 Выберите банк(и) - можно до 2х:\n\n{bank_text}",
                reply_markup=keyboard
            )
            await q.answer()
    
    elif session["step"] == "rate":
        if data.startswith("rate:"):
            if data == "rate:bybit":
                session["data"]["rate"] = "байбит средний"
                session["step"] = "ttl"
                await show_ttl_selection(q)
            elif data == "rate:grinex":
                session["data"]["rate"] = "курс Grinex"
                session["step"] = "ttl"
                await show_ttl_selection(q)
            else:  # rate:custom
                session["step"] = "custom_rate"
                await q.edit_message_text("📈 Введите свой курс (например: 81):\n\nДля возврата введите /back")
        elif data == "back":
            session["step"] = "payments"
            # Возвращаемся к выбору количества платежей
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 1 платеж", callback_data="payments:1")],
                [InlineKeyboardButton("💰 До 2 платежей", callback_data="payments:2")],
                [InlineKeyboardButton("💰 До 3 платежей", callback_data="payments:3")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back")]
            ])
            
            await q.edit_message_text(
                "💳 В сколько платежей:",
                reply_markup=keyboard
            )
            await q.answer()
    
    elif session["step"] == "ttl":
        if data.startswith("ttl:"):
            try:
                ttl_val = int(data.split(":", 1)[1])
            except (ValueError, IndexError):
                ttl_val = 30
            session["data"]["ttl_min"] = ttl_val
            session["step"] = "chats"
            await show_chat_selection(q, context, uid, state)
        elif data == "back":
            session["step"] = "rate"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Байбит средний", callback_data="rate:bybit")],
                [InlineKeyboardButton("Курс Grinex", callback_data="rate:grinex")],
                [InlineKeyboardButton("Установить свой", callback_data="rate:custom")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back")]
            ])
            await q.edit_message_text(
                "📈 Выберите курс:",
                reply_markup=keyboard
            )

    elif session["step"] == "chats":
        if data.startswith("chat:"):
            if data == "chat:all":
                session["data"]["target_chats"] = await get_user_chats(uid, state, context.bot)
            else:
                chat_id = int(data.replace("chat:", ""))
                session["data"]["target_chats"] = [chat_id]
            
            await finalize_order_creation(q, context, uid, state)
        elif data == "back":
            session["step"] = "ttl"
            await show_ttl_selection(q)

async def show_ttl_selection(q):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("10 минут", callback_data="ttl:10")],
        [InlineKeyboardButton("30 минут", callback_data="ttl:30")],
        [InlineKeyboardButton("1 час", callback_data="ttl:60")],
        [InlineKeyboardButton("2 часа", callback_data="ttl:120")],
        [InlineKeyboardButton("3 часа", callback_data="ttl:180")],
        [InlineKeyboardButton("В течение дня", callback_data="ttl:1440")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back")]
    ])
    
    await q.edit_message_text(
        "⏰ Выберите срок актуальности:",
        reply_markup=keyboard
    )

def build_chat_keyboard(available_chats, state, include_back=True):
    """Клавиатура выбора целевых чатов: 'Все чаты' + по чату + опц. 'Назад'."""
    buttons = [[InlineKeyboardButton("Все чаты", callback_data="chat:all")]]
    for chat_id in available_chats[:30]:
        chat_name = state["chats"].get(str(chat_id), {}).get("name", f"Чат {chat_id}")
        buttons.append([InlineKeyboardButton(f"📍 {chat_name}", callback_data=f"chat:{chat_id}")])
    if include_back:
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(buttons)

async def show_chat_selection(q, context, uid: int, state: Dict[str, Any]):
    available_chats = await get_user_chats(uid, state, context.bot)
    
    # Проверяем что есть доступные чаты
    if len(available_chats) == 0:
        await q.edit_message_text(
            "❌ <b>Нет доступных чатов!</b>\n\n"
            "Возможные причины:\n"
            "• Админ ещё не зарегистрировал чаты командой /register\n"
            "• Вы не являетесь участником зарегистрированных чатов\n\n"
            "💡 <b>Что делать:</b>\n"
            "1️⃣ Обратитесь к админу для регистрации чатов\n"
            "2️⃣ Убедитесь что вы участник нужных чатов",
            parse_mode="HTML"
        )
        # Очищаем сессию
        if uid in user_sessions:
            del user_sessions[uid]
        return
    
    if len(available_chats) == 1:
        user_sessions[uid]["data"]["target_chats"] = available_chats
        await finalize_order_creation(q, context, uid, state)
        return
    
    keyboard = build_chat_keyboard(available_chats, state, include_back=True)
    await q.edit_message_text(
        "🎯 Выберите целевые чаты:",
        reply_markup=keyboard
    )

async def finalize_order_creation(q, context, uid: int, state: Dict[str, Any]):
    session = user_sessions[uid]
    order_data = session["data"]
    
    # Create order
    bid = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    
    creator_username = order_data.get("creator_username", str(uid))
    order = {
        "direction": order_data["direction"],
        "amount": order_data["amount"],
        "bank": order_data["bank"],
        "payments": order_data.get("payments", ""),
        "rate": order_data["rate"],
        "created_at": created_at,
        "ttl_min": order_data["ttl_min"],
        "messages": [],
        "claimed_by": None,
        "expired": False,
        "creator_id": uid,
        "creator_username": creator_username,
        "order_type": "structured"
    }

    state["orders"][bid] = order
    increment_merchant_posted(state, uid)
    save_state(state)
    
    # Send to target chats
    target_chats = order_data.get("target_chats", [])
    ok = fail = 0
    sent_chat_names = []
    failed_chats = []
    
    print(f"📤 Sending order {short_id(bid)} to {len(target_chats)} chats: {target_chats}")
    
    for cid in target_chats:
        try:
            if TELEGRAM_AVAILABLE and context:
                print(f"  → Sending to chat {cid}...")
                
                # Проверяем миграцию группы в супергруппу
                actual_cid = cid
                msg = await send_message_safe(
                    context.bot,
                    chat_id=actual_cid,
                    text=render_message(bid, state, actual_cid, uid),
                    reply_markup=build_keyboard(bid, state, actual_cid, uid),
                    parse_mode=safe_parse_mode(),
                    disable_web_page_preview=True
                )
                # При миграции группы msg.chat.id != actual_cid — хелпер чинит state и отдаёт реальный id
                actual_cid = _record_sent_message(state, order, actual_cid, msg)
                print(f"  ✅ Sent to chat {msg.chat.id}, message_id: {msg.message_id}")
            
            # Получаем название чата для отображения
            chat_name = state["chats"].get(str(actual_cid), {}).get("name", f"Чат {actual_cid}")
            sent_chat_names.append(chat_name)
            ok += 1
        except Exception as e:
            error_text = str(e)
            print(f"  ❌ Failed to send to chat {cid}: {error_text}")
            chat_name = state["chats"].get(str(cid), {}).get("name", f"Чат {cid}")
            failed_chats.append(f"❌ {chat_name}: {error_text}")
            fail += 1
    
    save_state(state)
    if TELEGRAM_AVAILABLE and context:
        await schedule_expiration(context, bid, order["ttl_min"])
    
    # Clean up session
    del user_sessions[uid]
    
    # Show summary with close button
    formatted_amount = f"{order_data['amount']:,}" if isinstance(order_data['amount'], int) else order_data['amount']
    
    # Формируем список отправленных чатов
    chats_info = ""
    if sent_chat_names:
        chats_list = "\n".join([f"📍 {name}" for name in sent_chat_names])
        chats_info = f"\n\nОтправлено в чаты:\n{chats_list}"
    
    payments_line = f"Платежей: {order_data['payments']}\n" if order_data.get('payments') else ""
    
    errors_info = ""
    if failed_chats:
        errors_info = "\n\n⚠️ Ошибки:\n" + "\n".join(failed_chats)
    
    summary = (
        f"✅ Заявка #{short_id(bid)} создана!\n\n"
        f"Направление: {order_data['direction']}\n"
        f"Сумма: {formatted_amount} RUB\n"
        f"Банк: {order_data['bank']}\n"
        f"{payments_line}"
        f"Курс: {order_data['rate']}\n"
        f"Срок: {escape_html(format_ttl_display(order_data['ttl_min']))}\n\n"
        f"Отправлено в чатов: {ok}, ошибок: {fail}{chats_info}{errors_info}"
    )
    
    # Кнопки для управления заявкой
    control_keyboard = build_order_control_keyboard(bid)
    
    # Отправляем сводное сообщение и сохраняем его ID для дальнейших операций
    bot_message = await q.edit_message_text(summary, reply_markup=control_keyboard)
    
    # Сохраняем ID сообщения в боте
    state["orders"][bid]["bot_message_id"] = bot_message.message_id
    state["orders"][bid]["bot_chat_id"] = q.message.chat.id
    save_state(state)

async def handle_remind_order(update: Update, context, state: Dict[str, Any], old_bid: str):
    q = update.callback_query
    uid = q.from_user.id
    
    old_order = state["orders"].get(old_bid)
    if not old_order:
        await q.answer("Заявка не найдена.", show_alert=True)
        return
    
    # Check permissions
    if old_order.get("creator_id") != uid and not is_admin(uid, state):
        await q.answer("Напомнить может только создатель заявки или админ.", show_alert=True)
        return
    
    # Проверяем ограничения на напоминания
    now = datetime.now()
    
    # Инициализируем данные для отслеживания напоминаний
    if "reminders" not in state:
        state["reminders"] = {}
    
    if old_bid not in state["reminders"]:
        state["reminders"][old_bid] = []
    
    # Получаем историю напоминаний для этой заявки
    reminder_times = []
    hour_ago = now - timedelta(hours=1)
    
    for reminder_time_str in state["reminders"][old_bid]:
        try:
            reminder_time = datetime.fromisoformat(reminder_time_str)
            if reminder_time > hour_ago:
                reminder_times.append(reminder_time)
        except:
            continue
    
    # Обновляем список, оставляя только последние записи
    state["reminders"][old_bid] = [t.isoformat(timespec="seconds") for t in reminder_times]
    
    # Проверка: не более 5 напоминаний в час
    if len(reminder_times) >= 5:
        await q.answer()  # Закрываем callback query
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="❌ Не более 5 напоминаний на одну заявку в час."
            )
        except Exception:
            pass
        return
    
    # Проверка: минимум 10 минут между напоминаниями
    if reminder_times:
        last_reminder = max(reminder_times)
        time_since_last = now - last_reminder
        
        if time_since_last < timedelta(minutes=10):
            await q.answer()  # Закрываем callback query
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text="❌ Интервал между напоминаниями минимум 10 минут."
                )
            except Exception:
                pass
            return
    
    # Записываем время текущего напоминания
    state["reminders"][old_bid].append(now.isoformat(timespec="seconds"))
    
    # Сохраняем изменения в state
    save_state(state)
    
    # Create new order based on old one
    new_bid = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    
    new_order = old_order.copy()
    new_order.update({
        "created_at": created_at,
        "messages": [],
        "claimed_by": None,
        "expired": False
    })
    
    state["orders"][new_bid] = new_order
    
    # Delete old messages
    for msg in old_order.get("messages", []):
        try:
            if TELEGRAM_AVAILABLE and context:
                await context.bot.delete_message(
                    chat_id=msg["chat_id"],
                    message_id=msg["message_id"]
                )
        except Exception:
            pass
    
    # Mark old order as expired
    old_order["expired"] = True
    
    # Send new messages
    target_chats = [msg["chat_id"] for msg in old_order.get("messages", [])]
    ok = fail = 0
    
    for cid in target_chats:
        try:
            if TELEGRAM_AVAILABLE and context:
                msg = await send_message_safe(
                    context.bot,
                    chat_id=cid,
                    text=render_message(new_bid, state, cid, uid),
                    reply_markup=build_keyboard(new_bid, state, cid, uid),
                    parse_mode=safe_parse_mode(),
                    disable_web_page_preview=True
                )
                _record_sent_message(state, new_order, cid, msg)
            ok += 1
        except Exception:
            fail += 1
    
    save_state(state)
    
    if TELEGRAM_AVAILABLE and context:
        await schedule_expiration(context, new_bid, new_order["ttl_min"])
    
    await q.answer(f"Заявка #{short_id(new_bid)} создана на основе старой!")

async def handle_message(update: Update, context):
    """Handle text messages for number input in order creation"""
    if not check_private_chat_only(update):
        return
        
    uid = update.effective_user.id
    
    if uid not in user_sessions:
        return
    
    session = user_sessions[uid]
    text = update.message.text.strip()
    
    # Обрабатываем редактирование суммы
    if session.get("state") == "editing_amount":
        state = load_state()
        await handle_amount_edit_input(update, context, uid, state)
        return
    
    if session["step"] == "amount":
        try:
            amount = int(text.replace(",", "").replace(" ", ""))
            if amount <= 0:
                raise ValueError("Amount must be positive")
            
            session["data"]["amount"] = amount
            session["step"] = "bank"
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("СБЕР", callback_data="bank:sber")],
                [InlineKeyboardButton("ТИНЬКОФФ", callback_data="bank:tinkoff")],
                [InlineKeyboardButton("АЛЬФА", callback_data="bank:alfa")],
                [InlineKeyboardButton("СБП", callback_data="bank:sbp")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back")]
            ])
            
            await update.message.reply_text(
                "🏦 Выберите банк(и) - можно до 2х:\n\nВыбрано банков: 0/2",
                reply_markup=keyboard
            )
        except ValueError:
            await update.message.reply_text("❌ Неверный формат. Введите сумму числом (например: 150000)\n\nДля возврата к выбору направления введите /back")
    
    elif session["step"] == "custom_rate":
        try:
            rate = float(text.replace(",", "."))
            if rate <= 0:
                raise ValueError("Rate must be positive")
            
            session["data"]["rate"] = f"{rate}"
            session["step"] = "ttl"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("10 минут", callback_data="ttl:10")],
                [InlineKeyboardButton("30 минут", callback_data="ttl:30")],
                [InlineKeyboardButton("1 час", callback_data="ttl:60")],
                [InlineKeyboardButton("2 часа", callback_data="ttl:120")],
                [InlineKeyboardButton("3 часа", callback_data="ttl:180")],
                [InlineKeyboardButton("В течение дня", callback_data="ttl:1440")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back")]
            ])
            await update.message.reply_text(
                "⏰ Выберите срок актуальности:",
                reply_markup=keyboard
            )
        except ValueError:
            await update.message.reply_text("❌ Неверный формат курса. Введите число (например: 81 или 81.5)\n\nДля возврата введите /back")

    elif session["step"] == "template_amount":
        try:
            amount = int(text.replace(",", "").replace(" ", ""))
            if amount <= 0:
                raise ValueError("Amount must be positive")
        except ValueError:
            await update.message.reply_text(
                "❌ Неверный формат. Введите сумму числом (например: 150000)"
            )
            return
        session["data"]["amount"] = amount
        state = load_state()
        available_chats = await get_user_chats(uid, state, context.bot)
        if len(available_chats) == 0:
            await update.message.reply_text(
                "❌ Нет доступных чатов. Обратитесь к админу для регистрации чатов."
            )
            del user_sessions[uid]
            return
        session["step"] = "chats"
        keyboard = build_chat_keyboard(available_chats, state, include_back=False)
        await update.message.reply_text("🎯 Выберите целевые чаты:", reply_markup=keyboard)
        return

    elif session["step"] == "template_name":
        ok, res = templates_store.validate_name(text)
        if not ok:
            msg = "❌ Имя не может быть пустым." if res == "empty" else "❌ Имя слишком длинное (макс. 30 символов)."
            await update.message.reply_text(msg + " Введи имя ещё раз:")
            return
        snapshot = session["data"].get("tpl_snapshot", {})
        state = load_state()
        ok_add, code = templates_store.add_template(state, uid, res, snapshot, overwrite=False)
        if not ok_add and code == "exists":
            session["data"]["pending_name"] = res
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("♻️ Перезаписать", callback_data="tpl:overwrite")],
                [InlineKeyboardButton("✖️ Отмена", callback_data="tpl:cancel")],
            ])
            await update.message.reply_text(
                f"⚠️ Шаблон «{escape_html(res)}» уже есть. Перезаписать?",
                reply_markup=keyboard, parse_mode=safe_parse_mode()
            )
            return
        if not ok_add and code == "limit":
            await update.message.reply_text(
                f"❌ Достигнут лимит {templates_store.MAX_TEMPLATES} шаблонов. Удали лишний через /templates."
            )
            del user_sessions[uid]
            return
        save_state(state)
        src_bid = session["data"].get("src_bid")
        del user_sessions[uid]
        await _mark_order_in_template(context, state, src_bid)
        await update.message.reply_text(
            f"✅ Шаблон «{escape_html(res)}» сохранён. Теперь /create предложит его.",
            parse_mode=safe_parse_mode()
        )
        return

    elif session["step"] == "template_rename":
        state = load_state()
        index = session["data"].get("rename_index")
        ok, code = templates_store.rename_template(state, uid, index, text)
        if not ok:
            msgs = {
                "empty": "❌ Имя не может быть пустым.",
                "too_long": "❌ Имя слишком длинное (макс. 30 символов).",
                "exists": "❌ Шаблон с таким именем уже есть.",
                "not_found": "❌ Шаблон не найден.",
            }
            await update.message.reply_text(msgs.get(code, "❌ Ошибка.") + " Попробуй ещё раз или /templates.")
            if code == "not_found":
                del user_sessions[uid]
            return
        save_state(state)
        del user_sessions[uid]
        await update.message.reply_text("✅ Шаблон переименован.")
        return

    # Обработка команды /back для возврата к предыдущему шагу
    if text == "/back" and uid in user_sessions:
        session = user_sessions[uid]
        if session["step"] == "amount":
            session["step"] = "direction"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Куплю RUB - отдам USDT", callback_data="dir:buy_rub")],
                [InlineKeyboardButton("Продам RUB - возьму USDT", callback_data="dir:sell_rub")]
            ])
            await update.message.reply_text(
                "🏗 Создание заявки\n\nВыберите направление:",
                reply_markup=keyboard
            )
        elif session["step"] == "custom_rate":
            session["step"] = "rate"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Байбит средний", callback_data="rate:bybit")],
                [InlineKeyboardButton("Курс Grinex", callback_data="rate:grinex")],
                [InlineKeyboardButton("Установить свой", callback_data="rate:custom")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back")]
            ])
            await update.message.reply_text(
                "📈 Выберите курс:",
                reply_markup=keyboard
            )

async def delete_message_job(context):
    """Удаляет сообщение через заданное время"""
    job_data = context.job.data
    try:
        await context.bot.delete_message(
            chat_id=job_data["chat_id"],
            message_id=job_data["message_id"]
        )
    except Exception:
        pass


async def handle_close_order(update: Update, context, state: Dict[str, Any], bid: str):
    """Обрабатывает закрытие заявки создателем или админом"""
    q = update.callback_query
    uid = q.from_user.id
    order = state["orders"].get(bid)

    if not order:
        try:
            await q.edit_message_text("❌ Заявка не найдена.")
        except Exception:
            pass
        return

    creator_id = order.get("creator_id")

    # Проверяем права доступа: создатель или админ
    if uid != creator_id and not is_admin(uid, state):
        chat_id = q.message.chat.id if q.message else None
        if chat_id and TELEGRAM_AVAILABLE and context:
            username = q.from_user.username
            user_mention = f"@{username}" if username else q.from_user.first_name
            try:
                error_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{user_mention} закрыть заявку может только создатель заявки или админ.",
                    reply_to_message_id=q.message.message_id
                )
                if hasattr(context, 'job_queue') and context.job_queue:
                    context.job_queue.run_once(
                        delete_message_job,
                        when=timedelta(minutes=2),
                        data={"chat_id": chat_id, "message_id": error_msg.message_id}
                    )
            except Exception:
                pass
        return

    # Если заявка в работе — завершаем сделку
    claimed = order.get("claimed_by")
    if claimed:
        partner_id = claimed.get("id")

        # Убираем кнопки у партнёра
        partner_panel_msg_id = order.get("partner_panel_msg_id")
        if partner_panel_msg_id and partner_id:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=partner_id,
                    message_id=partner_panel_msg_id,
                    reply_markup=None
                )
            except Exception:
                pass

        # Отменяем авто-задания
        if context.job_queue:
            for jname in [f"remind_confirm:{bid}", f"auto_confirm:{bid}",
                          f"forceclose:{bid}", f"autodelete:{bid}",
                          f"autoclose_confirmed:{bid}"]:
                for job in context.job_queue.get_jobs_by_name(jname):
                    job.schedule_removal()

        # Записываем сделку и показываем форму оценки создателю
        await _do_confirm_deal(
            bid=bid,
            partner_id=partner_id,
            merchant_id=uid,
            state=state,
            bot=context.bot,
            job_queue=context.job_queue,
            merchant_chat_id=q.message.chat_id,
            merchant_msg_id=q.message.message_id,
        )

        # Удаляем заявку из групповых чатов (skip_bot_msg=True — q.message будет обработан _do_confirm_deal)
        await _close_order_from_chats(bid, state, context.bot, skip_bot_msg=True)
        return

    # Заявка свободна: сначала редактируем q.message (снимаем кнопки), потом удаляем из групп
    logger.error(f"[close] bid={bid[:8]} free order close, messages={len(order.get('messages', []))}")
    try:
        await q.edit_message_text("🗑️ Заявка закрыта и удалена из всех чатов.")
    except Exception as e:
        logger.error(f"[close] edit_message_text failed: {e}")

    # Удаляем только из групповых чатов (bot_message = q.message, уже отредактирован)
    await _close_order_from_chats(bid, state, context.bot, skip_bot_msg=True)

async def handle_close_done(update: Update, context, state: Dict[str, Any], bid: str, partner_id: int):
    """Создатель закрыл заявку как выполненную — записываем сделку и просим оценку."""
    q = update.callback_query
    uid = q.from_user.id
    order = state["orders"].get(bid)
    if not order:
        await q.edit_message_text("❌ Заявка не найдена.")
        return

    # Отменяем авто-задания если есть
    if context.job_queue:
        for jname in [f"remind_confirm:{bid}", f"auto_confirm:{bid}", f"forceclose:{bid}", f"autodelete:{bid}"]:
            for job in context.job_queue.get_jobs_by_name(jname):
                job.schedule_removal()

    await _do_confirm_deal(
        bid=bid,
        partner_id=partner_id,
        merchant_id=uid,
        state=state,
        bot=context.bot,
        job_queue=context.job_queue,
        merchant_chat_id=q.message.chat_id,
        merchant_msg_id=q.message.message_id,
    )

    # Уведомляем партнёра
    try:
        await context.bot.send_message(
            chat_id=partner_id,
            text=f"✅ Мерчант закрыл заявку #{short_id(bid)} как выполненную."
        )
    except Exception:
        pass


async def handle_close_cancel(update: Update, context, state: Dict[str, Any], bid: str):
    """Создатель отменил заявку (в работе, но без подтверждения выполнения)."""
    q = update.callback_query
    uid = q.from_user.id
    order = state["orders"].get(bid)
    if not order:
        await q.edit_message_text("❌ Заявка не найдена.")
        return

    claimed = order.get("claimed_by") or {}
    partner_id = claimed.get("id")

    # Отменяем авто-задания
    if context.job_queue:
        for jname in [f"remind_confirm:{bid}", f"auto_confirm:{bid}", f"forceclose:{bid}", f"autodelete:{bid}"]:
            for job in context.job_queue.get_jobs_by_name(jname):
                job.schedule_removal()

    # Удаляем панель партнёра
    partner_panel_msg_id = order.get("partner_panel_msg_id")
    if partner_panel_msg_id and partner_id:
        try:
            await context.bot.delete_message(chat_id=partner_id, message_id=partner_panel_msg_id)
        except Exception:
            pass

    # Уведомляем партнёра об отмене
    if partner_id:
        try:
            await context.bot.send_message(
                chat_id=partner_id,
                text=f"❌ Мерчант отменил заявку #{short_id(bid)}."
            )
        except Exception:
            pass

    # Закрываем заявку
    await _close_order_from_chats(bid, state, context.bot)

    try:
        await q.edit_message_text("🗑️ Заявка отменена и удалена из всех чатов.")
    except Exception:
        pass
    await q.answer("✅ Заявка отменена.")


async def handle_remind_from_bot(update: Update, context, state: Dict[str, Any], bid: str):
    """Обрабатывает напоминание о заявке из бота"""
    q = update.callback_query
    uid = q.from_user.id
    order = state["orders"].get(bid)
    
    if not order:
        await q.answer("Заявка не найдена.", show_alert=True)
        return
    
    creator_id = order.get("creator_id")
    if uid != creator_id:
        await q.answer("Напомнить о заявке может только её создатель.", show_alert=True)
        return

    if order.get("expired") or order.get("closed"):
        await q.answer("Нельзя напомнить о закрытой или истёкшей заявке.", show_alert=True)
        return
    
    # Проверяем что заявка свободна (не взята никем)
    if order.get("claimed_by"):
        await q.answer()  # Закрываем callback query
        await context.bot.send_message(
            chat_id=uid,
            text="❌ Напоминать можно только о свободных заявках."
        )
        return
    
    # Проверяем ограничения на напоминания
    now = datetime.now()
    
    # Инициализируем данные для отслеживания напоминаний
    if "reminders" not in state:
        state["reminders"] = {}
    
    if bid not in state["reminders"]:
        state["reminders"][bid] = []
    
    # Получаем историю напоминаний для этой заявки
    reminder_times = []
    hour_ago = now - timedelta(hours=1)
    
    for reminder_time_str in state["reminders"][bid]:
        try:
            reminder_time = datetime.fromisoformat(reminder_time_str)
            if reminder_time > hour_ago:
                reminder_times.append(reminder_time)
        except:
            continue
    
    # Обновляем список, оставляя только последние записи
    state["reminders"][bid] = [t.isoformat(timespec="seconds") for t in reminder_times]
    
    # Получаем динамические лимиты
    system_limits = state.get("system_limits", {})
    max_reminders = system_limits.get("reminder_per_hour", 5)
    min_interval = system_limits.get("reminder_interval_min", 10)
    
    # Проверка: не более N напоминаний в час
    if len(reminder_times) >= max_reminders:
        await q.answer()  # Закрываем callback query
        
        # Логируем событие
        log_antispam_event(uid, "reminder_limit", bid, state)
        
        # Уведомляем админов о подозрительной активности
        await notify_admins_suspicious_activity(context, uid, "Превышение лимита напоминаний", bid, state)
        
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"❌ Не более {max_reminders} напоминаний на одну заявку в час."
            )
        except Exception:
            pass
        return
    
    # Проверка: минимум N минут между напоминаниями
    if reminder_times:
        last_reminder = max(reminder_times)
        time_since_last = now - last_reminder
        
        if time_since_last < timedelta(minutes=min_interval):
            await q.answer()  # Закрываем callback query
            
            # Логируем событие
            log_antispam_event(uid, "reminder_interval", bid, state)
            
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"❌ Интервал между напоминаниями минимум {min_interval} минут."
                )
            except Exception:
                pass
            return
    
    # Записываем время текущего напоминания
    state["reminders"][bid].append(now.isoformat(timespec="seconds"))
    save_state(state)
    
    # Отправляем напоминания во все чаты где есть сообщения этой заявки
    reminder_text = "🔔 Актуально, если есть что пиши!"
    sent_count = 0
    
    for msg in order.get("messages", []):
        try:
            if TELEGRAM_AVAILABLE and context:
                reminder_msg = await context.bot.send_message(
                    chat_id=msg["chat_id"],
                    text=reminder_text,
                    reply_to_message_id=msg["message_id"],
                    parse_mode=safe_parse_mode()
                )
                
                # Удаляем уведомление через 3 минуты
                if hasattr(context, 'job_queue') and context.job_queue:
                    context.job_queue.run_once(
                        delete_message_job, 
                        180,  # 3 минуты
                        data={"chat_id": msg["chat_id"], "message_id": reminder_msg.message_id}
                    )
                sent_count += 1
        except Exception:
            pass  # Не удалось отправить в этот чат
    
    await q.answer(f"✅ Напоминание отправлено в {sent_count} чатов!")

async def handle_send_remaining(update: Update, context, state: Dict[str, Any], bid: str):
    """Показывает список оставшихся чатов для выбора"""
    q = update.callback_query
    uid = q.from_user.id
    
    # Проверяем что заявка существует
    order = state["orders"].get(bid)
    if not order:
        await q.answer("❌ Заявка не найдена!")
        return
        
    # Проверяем права
    creator_id = order.get("creator_id", 0)
    if uid != creator_id and not is_admin(uid, state):
        await q.answer("❌ Только создатель заявки может отправлять в оставшиеся чаты!")
        return
    
    if order.get("expired") or order.get("closed"):
        await q.answer("❌ Нельзя отправлять закрытую или истёкшую заявку!")
        return
    
    # Проверяем что заявка свободна (не взята в работу)
    if order.get("claimed_by"):
        await q.answer()  # Закрываем callback query
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="❌ Отправить можно только свободные заявки."
            )
        except Exception:
            pass
        return
    
    # Получаем все доступные чаты
    available_chats = await get_user_chats(uid, state, context.bot)
    if not available_chats:
        await q.answer("❌ Нет доступных чатов!")
        return
    
    # Находим чаты, в которые заявка уже отправлена
    sent_chats = {msg["chat_id"] for msg in order.get("messages", [])}
    
    # Находим оставшиеся чаты
    remaining_chats = [cid for cid in available_chats if cid not in sent_chats]
    
    if not remaining_chats:
        await q.answer("✅ Заявка уже отправлена во все доступные чаты!")
        return
    
    # Создаем кнопки для выбора чатов
    buttons = []
    if len(remaining_chats) > 1:
        buttons.append([InlineKeyboardButton("📤 Отправить во все оставшиеся", callback_data=f"send_all_remaining:{bid}")])
    
    # Добавляем кнопки для отдельных чатов
    for chat_id in remaining_chats[:30]:  # Ограничиваем до 30 чатов для UI
        chat_key = str(chat_id)
        chat_name = state["chats"].get(chat_key, {}).get("name", f"Чат {chat_id}")
        buttons.append([InlineKeyboardButton(f"📍 {chat_name}", callback_data=f"send_to_chat:{bid}:{chat_id}")])
    
    # Кнопка "Назад"
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data=f"back_to_control:{bid}")])
    
    keyboard = InlineKeyboardMarkup(buttons)
    
    await q.edit_message_text(
        f"📤 Выберите куда отправить заявку #{short_id(bid)}:\n\n"
        f"Доступно {len(remaining_chats)} чатов для отправки:",
        reply_markup=keyboard
    )

async def handle_send_all_remaining(update: Update, context, state: Dict[str, Any], bid: str):
    """Отправляет заявку во все оставшиеся чаты"""
    q = update.callback_query
    uid = q.from_user.id
    order = state["orders"].get(bid)
    
    if not order:
        await q.answer("❌ Заявка не найдена!")
        return
    
    # Получаем оставшиеся чаты
    available_chats = await get_user_chats(uid, state, context.bot)
    sent_chats = {msg["chat_id"] for msg in order.get("messages", [])}
    remaining_chats = [cid for cid in available_chats if cid not in sent_chats]
    
    if not remaining_chats:
        await q.answer("✅ Нет чатов для отправки!")
        return
    
    # Отправляем во все оставшиеся чаты
    ok = fail = 0
    for cid in remaining_chats:
        try:
            if TELEGRAM_AVAILABLE and context:
                msg = await send_message_safe(
                    context.bot,
                    chat_id=cid,
                    text=render_message(bid, state, cid, uid),
                    reply_markup=build_keyboard(bid, state, cid, uid),
                    parse_mode=safe_parse_mode(),
                    disable_web_page_preview=True
                )
                _record_sent_message(state, order, cid, msg)
                ok += 1
        except Exception:
            fail += 1
    
    save_state(state)
    await q.answer(f"✅ Отправлено в {ok} чатов!")
    
    # Возвращаемся к панели управления
    await handle_back_to_control(update, context, state, bid)

async def handle_send_to_chat(update: Update, context, state: Dict[str, Any], bid: str, chat_id: int):
    """Отправляет заявку в конкретный чат"""
    q = update.callback_query
    uid = q.from_user.id
    order = state["orders"].get(bid)
    
    if not order:
        await q.answer("❌ Заявка не найдена!")
        return
    
    try:
        if TELEGRAM_AVAILABLE and context:
            msg = await send_message_safe(
                context.bot,
                chat_id=chat_id,
                text=render_message(bid, state, chat_id, uid),
                reply_markup=build_keyboard(bid, state, chat_id, uid),
                parse_mode=safe_parse_mode(),
                disable_web_page_preview=True
            )
            chat_id = _record_sent_message(state, order, chat_id, msg)
            save_state(state)

            chat_name = state["chats"].get(str(chat_id), {}).get("name", f"Чат {chat_id}")
            await q.answer(f"✅ Отправлено в {chat_name}!")
        else:
            await q.answer("❌ Telegram недоступен!")
    except Exception as e:
        await q.answer(f"❌ Ошибка отправки: {str(e)[:50]}")
        return
    
    # Проверяем остались ли еще чаты для отправки
    available_chats = await get_user_chats(uid, state, context.bot)
    sent_chats = {msg["chat_id"] for msg in order.get("messages", [])}
    remaining_chats = [cid for cid in available_chats if cid not in sent_chats]
    
    # Если чатов не осталось - возвращаемся к панели управления
    if not remaining_chats:
        await handle_back_to_control(update, context, state, bid)
    else:
        # Иначе обновляем список доступных чатов
        await handle_send_remaining(update, context, state, bid)

async def handle_back_to_control(update: Update, context, state: Dict[str, Any], bid: str):
    """Возвращает к панели управления заявкой"""
    q = update.callback_query
    order = state["orders"].get(bid)
    
    if not order:
        await q.answer("❌ Заявка не найдена!")
        return
    
    # Восстанавливаем текст с информацией о заявке
    if order.get("order_type") == "structured":
        formatted_amount = f"{order['amount']:,}" if isinstance(order['amount'], int) else order['amount']
        
        # Формируем список чатов из сообщений заявки
        sent_chats = []
        for msg in order.get('messages', []):
            chat_id = msg.get('chat_id')
            if chat_id:
                chat_name = state["chats"].get(str(chat_id), {}).get("name", f"Чат {chat_id}")
                sent_chats.append(chat_name)
        
        chats_info = ""
        if sent_chats:
            chats_list = "\n".join([f"📍 {name}" for name in sent_chats])
            chats_info = f"\n\nОтправлено в чаты:\n{chats_list}"
        
        payments_line = f"Платежей: {order['payments']}\n" if order.get('payments') else ""
        summary = (
            f"✅ Заявка #{short_id(bid)} создана!\n\n"
            f"Направление: {order['direction']}\n"
            f"Сумма: {formatted_amount} RUB\n"
            f"Банк: {order['bank']}\n"
            f"{payments_line}"
            f"Курс: {order['rate']}\n"
            f"Срок: {format_ttl_display(order['ttl_min'])}\n\n"
            f"Отправлено в чатов: {len(order.get('messages', []))}, ошибок: 0{chats_info}"
        )
    else:
        # Для нестандартных заявок тоже добавляем список чатов
        sent_chats = []
        for msg in order.get('messages', []):
            chat_id = msg.get('chat_id')
            if chat_id:
                chat_name = state["chats"].get(str(chat_id), {}).get("name", f"Чат {chat_id}")
                sent_chats.append(chat_name)
        
        chats_info = ""
        if sent_chats:
            chats_list = "\n".join([f"📍 {name}" for name in sent_chats])
            chats_info = f"\n\nОтправлено в чаты:\n{chats_list}"
        
        summary = (
            f"Заявка #{short_id(bid)} создана!\n\n"
            f"Отправлено в чатов: {len(order.get('messages', []))}, ошибок: 0{chats_info}"
        )
    
    # Кнопки управления
    control_keyboard = build_order_control_keyboard(bid)
    
    await q.edit_message_text(summary, reply_markup=control_keyboard)

MAX_TTL_EXTENSIONS = 3


async def handle_extend_ttl(update: Update, context, state: Dict[str, Any], bid: str):
    """Продлевает срок активной заявки на 30 минут (до MAX_TTL_EXTENSIONS раз).

    on_callback уже вызвал q.answer() до роутинга — повторный alert не сработает,
    поэтому подтверждение и ошибки шлём отдельным сообщением в личку мерчанта."""
    q = update.callback_query
    uid = q.from_user.id
    order = state["orders"].get(bid)

    if not order:
        await q.message.reply_text("❌ Заявка не найдена.")
        return
    if order.get("expired"):
        await q.message.reply_text("⏰ Заявка уже истекла — используй «Переопубликовать».")
        return
    if uid != order.get("creator_id") and not is_admin(uid, state):
        await q.message.reply_text("❌ Продлить может только создатель заявки.")
        return
    used = order.get("extensions", 0)
    if used >= MAX_TTL_EXTENSIONS:
        await q.message.reply_text(f"❌ Достигнут лимит продлений ({MAX_TTL_EXTENSIONS}).")
        return

    now = datetime.now(timezone.utc)
    # Новый дедлайн = текущий дедлайн истечения + 30 мин. Берём время из job'а expire.
    base_deadline = None
    if hasattr(context, 'job_queue') and context.job_queue:
        for j in context.job_queue.get_jobs_by_name(f"expire:{bid}"):
            nt = getattr(j, "next_t", None)
            if nt is not None:
                base_deadline = nt
            j.schedule_removal()
    if base_deadline is None:
        base_deadline = now
    delay = (base_deadline + timedelta(minutes=30) - now).total_seconds()
    if delay < 60:
        delay = 30 * 60
    if hasattr(context, 'job_queue') and context.job_queue:
        context.job_queue.run_once(expire_job, when=delay, data={"bid": bid}, name=f"expire:{bid}")

    order["extensions"] = used + 1
    order["ttl_min"] = order.get("ttl_min", 0) + 30
    save_state(state)

    # Обновляем сообщения заявки во всех групповых чатах (новый срок в карточке)
    creator = order.get("creator_id")
    for msg in order.get("messages", []):
        try:
            await edit_message_safe(
                context.bot,
                chat_id=msg["chat_id"],
                message_id=msg["message_id"],
                text=render_message(bid, state, msg["chat_id"], creator),
                reply_markup=build_keyboard(bid, state, msg["chat_id"], creator),
                parse_mode=safe_parse_mode(),
                disable_web_page_preview=True,
            )
        except Exception:
            pass

    remaining = MAX_TTL_EXTENSIONS - order["extensions"]
    await q.message.reply_text(
        f"✅ Заявка продлена на 30 минут. Новый срок: {format_ttl_display(order['ttl_min'])}. "
        f"Осталось продлений: {remaining}."
    )


async def handle_edit_amount(update: Update, context, state: Dict[str, Any], bid: str):
    """Обрабатывает редактирование суммы заявки"""
    q = update.callback_query
    uid = q.from_user.id
    order = state["orders"].get(bid)

    if not order:
        await q.answer("Заявка не найдена.", show_alert=True)
        return
    
    creator_id = order.get("creator_id")
    if uid != creator_id:
        await q.answer("Редактировать сумму может только создатель заявки.", show_alert=True)
        return
    
    if order.get("expired") or order.get("closed"):
        await q.answer("Нельзя редактировать закрытую или истёкшую заявку.", show_alert=True)
        return
    
    # Проверяем что заявка свободна (не взята в работу)
    if order.get("claimed_by"):
        await q.answer()  # Закрываем callback query
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="❌ Редактировать можно только свободные заявки."
            )
        except Exception:
            pass
        return
    
    # Проверяем что это структурированная заявка с суммой
    if order.get("order_type") != "structured" or "amount" not in order:
        await q.answer("Можно редактировать только структурированные заявки с суммой.", show_alert=True)
        return
    
    # Начинаем сессию редактирования суммы
    user_sessions[uid] = {
        "state": "editing_amount",
        "order_id": bid,
        "original_amount": order["amount"]
    }
    
    current_amount = f"{order['amount']:,}" if isinstance(order['amount'], int) else str(order['amount'])
    # Добавляем кнопку "Назад" для возврата к управлению заявкой
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад", callback_data=f"back_to_control:{bid}")]
    ])
    await q.edit_message_text(
        f"📝 Редактирование суммы заявки #{short_id(bid)}\n\n"
        f"Текущая сумма: {current_amount} RUB\n\n"
        f"Введите новую сумму (только число, например: 50000):",
        reply_markup=keyboard
    )

async def handle_amount_edit_input(update: Update, context, uid: int, state: Dict[str, Any]):
    """Обрабатывает ввод новой суммы"""
    session = user_sessions[uid]
    bid = session["order_id"]
    order = state["orders"][bid]
    
    try:
        # Парсим новую сумму
        text = update.message.text.strip().replace(",", "").replace(" ", "")
        new_amount = int(text)
        
        if new_amount <= 0:
            await update.message.reply_text("❌ Сумма должна быть больше 0. Попробуйте ещё раз:")
            return
        
        # Обновляем сумму в заявке
        old_amount = order["amount"]
        order["amount"] = new_amount
        save_state(state)
        
        # Обновляем все сообщения заявки в чатах
        for msg in order.get("messages", []):
            try:
                if TELEGRAM_AVAILABLE and context:
                    await edit_message_safe(
                        context.bot,
                        chat_id=msg["chat_id"],
                        message_id=msg["message_id"],
                        text=render_message(bid, state, msg["chat_id"], uid),
                        reply_markup=build_keyboard(bid, state, msg["chat_id"], uid),
                        parse_mode=safe_parse_mode(),
                        disable_web_page_preview=True
                    )
            except Exception:
                pass
        
        # Отправляем уведомления во все чаты
        notification_text = "💰 Сумма заявки изменилась, проверь вдруг подойдет"
        sent_count = 0
        
        for msg in order.get("messages", []):
            try:
                if TELEGRAM_AVAILABLE and context:
                    notification_msg = await context.bot.send_message(
                        chat_id=msg["chat_id"],
                        text=notification_text,
                        reply_to_message_id=msg["message_id"],
                        parse_mode=safe_parse_mode()
                    )
                    
                    # Удаляем уведомление через 3 минуты
                    if hasattr(context, 'job_queue') and context.job_queue:
                        context.job_queue.run_once(
                            delete_message_job, 
                            180,  # 3 минуты
                            data={"chat_id": msg["chat_id"], "message_id": notification_msg.message_id}
                        )
                    sent_count += 1
            except Exception:
                pass
        
        # Завершаем сессию
        del user_sessions[uid]
        
        formatted_old = f"{old_amount:,}" if isinstance(old_amount, int) else str(old_amount)
        formatted_new = f"{new_amount:,}" if isinstance(new_amount, int) else str(new_amount)
        
        # Сообщение с кнопками управления
        success_text = (
            f"✅ Сумма заявки #{short_id(bid)} изменена!\n\n"
            f"Было: {formatted_old} RUB\n"
            f"Стало: {formatted_new} RUB\n\n"
            f"Уведомления отправлены в {sent_count} чатов."
        )
        
        # Восстанавливаем кнопки управления
        control_keyboard = build_order_control_keyboard(bid)
        
        await update.message.reply_text(success_text, reply_markup=control_keyboard)
        
    except ValueError:
        await update.message.reply_text("❌ Неверный формат суммы. Введите число (например: 50000):")

async def send_release_notification(context, bid: str, order: Dict[str, Any], state: Dict[str, Any]):
    """Отправляет уведомление о том, что заявка снова свободна"""
    if not TELEGRAM_AVAILABLE or not context:
        return
        
    notification_text = f"🔔 Заявка #{short_id(bid)} снова свободна, присмотрись!"
    
    # Отправляем уведомления во все чаты где есть сообщения этой заявки
    for msg in order.get("messages", []):
        try:
            # Отправляем ответом на оригинальную заявку
            notification_msg = await context.bot.send_message(
                chat_id=msg["chat_id"],
                text=notification_text,
                reply_to_message_id=msg["message_id"],
                parse_mode=safe_parse_mode()
            )
            
            # Удаляем уведомление через 3 минуты
            if hasattr(context, 'job_queue') and context.job_queue:
                context.job_queue.run_once(
                    delete_message_job, 
                    180,  # 3 минуты
                    data={"chat_id": msg["chat_id"], "message_id": notification_msg.message_id}
                )
        except Exception:
            pass

async def show_ttl_selection_message(update: Update):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("10 минут", callback_data="ttl:10")],
        [InlineKeyboardButton("30 минут", callback_data="ttl:30")],
        [InlineKeyboardButton("1 час", callback_data="ttl:60")],
        [InlineKeyboardButton("2 часа", callback_data="ttl:120")],
        [InlineKeyboardButton("3 часа", callback_data="ttl:180")],
        [InlineKeyboardButton("В течение дня", callback_data="ttl:1440")]
    ])
    
    await update.message.reply_text(
        "⏰ Выберите срок актуальности:",
        reply_markup=keyboard
    )

async def unknown(update: Update, context):
    if not check_private_chat_only(update):
        return
    state = load_state()
    if not await check_not_banned(update, state):
        return
    await update.message.reply_text("Неизвестная команда. Наберите /help.")

async def set_user_commands(bot, user_id: int, role: str):
    """Set personalized command menu for user based on their role."""
    partner_commands = [
        BotCommand("start", "Начало работы"),
        BotCommand("help", "Справка"),
        BotCommand("partner", "Карточка партнёра"),
        BotCommand("history", "История сделок"),
        BotCommand("timezone", "Часовой пояс"),
    ]
    merchant_commands = [
        BotCommand("create", "Создать заявку"),
        BotCommand("myorders", "Мои активные заявки"),
        BotCommand("templates", "Мои шаблоны заявок"),
        BotCommand("partner", "Карточка партнёра"),
        BotCommand("history", "История сделок"),
        BotCommand("timezone", "Часовой пояс"),
        BotCommand("start", "Начало работы"),
        BotCommand("help", "Справка"),
    ]
    admin_commands = [
        BotCommand("create", "Создать заявку"),
        BotCommand("myorders", "Мои активные заявки"),
        BotCommand("templates", "Мои шаблоны заявок"),
        BotCommand("partner", "Карточка партнёра"),
        BotCommand("history", "История сделок"),
        BotCommand("timezone", "Часовой пояс"),
        BotCommand("stats", "Расширенная статистика"),
        BotCommand("admins", "Список админов и мерчантов"),
        BotCommand("merchants", "Список мерчантов"),
        BotCommand("addmerchant", "Добавить мерчанта"),
        BotCommand("removemerchant", "Убрать мерчанта"),
        BotCommand("register", "Зарегистрировать чат"),
        BotCommand("unregister", "Убрать чат"),
        BotCommand("list", "Список чатов"),
        BotCommand("ban", "Заблокировать пользователя"),
        BotCommand("unban", "Разблокировать пользователя"),
        BotCommand("spamstats", "Статистика нарушений"),
        BotCommand("setlimits", "Настройка лимитов"),
        BotCommand("start", "Начало работы"),
        BotCommand("help", "Справка"),
    ]
    commands_map = {
        "admin": admin_commands,
        "merchant": merchant_commands,
        "partner": partner_commands,
    }
    commands = commands_map.get(role, partner_commands)
    try:
        await bot.set_my_commands(
            commands,
            scope=BotCommandScopeChat(chat_id=user_id)
        )
    except Exception:
        pass  # Non-critical

async def startup_expired_cleanup(bot, job_queue) -> None:
    """При старте бота удаляет из чатов все заявки с истёкшим сроком, которые не были удалены
    (например, если бот перезапускался пока задача авто-удаления висела в очереди)."""
    state = load_state()
    to_clean = [
        bid for bid, order in state["orders"].items()
        if order.get("expired")
        and not order.get("auto_deleted")
        and not order.get("claimed_by")
    ]
    if not to_clean:
        return

    logger.error(f"[startup_cleanup] found {len(to_clean)} stale expired order(s) to remove")
    for bid in to_clean:
        fresh = load_state()
        order = fresh["orders"].get(bid)
        if not order:
            continue
        creator_id = order.get("creator_id")
        await _close_order_from_chats(bid, fresh, bot)
        logger.error(f"[startup_cleanup] cleaned up expired order {short_id(bid)}")
        if creator_id:
            try:
                await bot.send_message(
                    chat_id=creator_id,
                    text=f"🗑️ Заявка №{short_id(bid)} удалена (истекла пока бот был недоступен).",
                    parse_mode=safe_parse_mode()
                )
            except Exception:
                pass


async def main():
    # Always start HTTP server for Cloud Run deployment
    http_runner = await start_http_server()
    
    # Check if we have a valid token and Telegram is available
    # Enable full Telegram bot functionality
    FORCE_HTTP_ONLY = False
    
    if not BOT_TOKEN or not TELEGRAM_AVAILABLE or FORCE_HTTP_ONLY:
        if not BOT_TOKEN:
            print("ERROR: BOT_TOKEN is not set.")
        if not TELEGRAM_AVAILABLE:
            print("ERROR: python-telegram-bot not installed properly.")
            
        print("Running in HTTP-only mode (demo)...")
        
        # Demo mode - show functionality once
        try:
            from bot_fixed import simulate_order_creation, simulate_claim_order, simulate_remind_order, simulate_list_chats
            print("\n🤖 Демонстрация функциональности бота")
            simulate_list_chats()
            bid = simulate_order_creation()
            simulate_claim_order(bid)
            simulate_remind_order(bid)
        except ImportError:
            print("Demo functions not available")
        
        # Keep HTTP server running for Cloud Run
        print("🌐 HTTP server running for Cloud Run deployment...")
        print("Bot will only respond to HTTP requests on port 5000")
        try:
            while True:
                await asyncio.sleep(3600)  # Sleep for 1 hour, keep server alive
        except KeyboardInterrupt:
            print("Shutting down...")
        finally:
            await http_runner.cleanup()
        return
    
    def _build_app():
        _app = (ApplicationBuilder()
                .token(BOT_TOKEN)
                .connect_timeout(60)
                .read_timeout(60)
                .write_timeout(60)
                .pool_timeout(60)
                .build())
        _app.add_handler(CommandHandler("start", start))
        _app.add_handler(CommandHandler("help", help_cmd))
        _app.add_handler(CommandHandler("register", register_chat))
        _app.add_handler(CommandHandler("unregister", unregister_chat))
        _app.add_handler(CommandHandler("list", list_chats))
        _app.add_handler(CommandHandler("create", create_order_start))
        _app.add_handler(CommandHandler("timezone", timezone_cmd))
        _app.add_handler(CommandHandler("myorders", myorders_cmd))
        _app.add_handler(CommandHandler("admins", admins_cmd))
        _app.add_handler(CommandHandler("ban", ban_user_cmd))
        _app.add_handler(CommandHandler("unban", unban_user_cmd))
        _app.add_handler(CommandHandler("spamstats", spam_stats_cmd))
        _app.add_handler(CommandHandler("setlimits", set_limits_cmd))
        _app.add_handler(CommandHandler("partner", cmd_partner))
        _app.add_handler(CommandHandler("history", cmd_history))
        _app.add_handler(CommandHandler("stats", cmd_stats))
        _app.add_handler(CommandHandler("addmerchant", cmd_addmerchant))
        _app.add_handler(CommandHandler("removemerchant", cmd_removemerchant))
        _app.add_handler(CommandHandler("merchants", cmd_listmerchants))
        _app.add_handler(CommandHandler("templates", templates_cmd))
        _app.add_handler(CallbackQueryHandler(on_callback))
        _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        _app.add_handler(MessageHandler(filters.COMMAND, unknown))

        async def ptb_error_handler(update, context):
            logger.error(f"PTB unhandled exception: {context.error}", exc_info=context.error)
            if update and hasattr(update, 'callback_query') and update.callback_query:
                try:
                    await update.callback_query.edit_message_text("❌ Внутренняя ошибка. Попробуйте ещё раз.")
                except Exception:
                    pass

        _app.add_error_handler(ptb_error_handler)
        return _app

    default_commands = [
        BotCommand("start", "Начало работы"),
        BotCommand("help", "Справка"),
    ]

    retry_delay = 15
    while True:
        app = _build_app()
        try:
            logger.error(f"[main] Connecting to Telegram...")
            await app.initialize()
            await app.bot.delete_webhook(read_timeout=30, connect_timeout=30)
            await app.start()
            await app.bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())
            # Обновляем персональные меню известных мерчантов/админов, чтобы новые
            # команды (например /templates) появлялись без ручного /start.
            try:
                _menu_state = load_state()
                _admin_ids = set(_menu_state.get("admins", []))
                _merchant_ids = set(_menu_state.get("merchants", []))
                for _mid in _admin_ids:
                    await set_user_commands(app.bot, _mid, "admin")
                for _mid in _merchant_ids - _admin_ids:
                    await set_user_commands(app.bot, _mid, "merchant")
                logger.error(f"[main] refreshed menus: {len(_admin_ids)} admins, {len(_merchant_ids - _admin_ids)} merchants")
            except Exception as e:
                logger.error(f"[main] menu refresh failed: {e}")
            await startup_expired_cleanup(app.bot, app.job_queue)
            logger.error("[main] Starting polling...")
            await app.updater.start_polling()
            logger.error("[main] Bot is running")
            retry_delay = 15
            await asyncio.Event().wait()
            break

        except asyncio.CancelledError:
            logger.error("[main] Cancelled — shutting down")
            try:
                await app.updater.stop()
            except Exception:
                pass
            try:
                await app.stop()
                await app.shutdown()
            except Exception:
                pass
            break

        except Exception as e:
            logger.error(f"[main] Bot error: {e}. Retrying in {retry_delay}s...")
            try:
                await app.updater.stop()
            except Exception:
                pass
            try:
                await app.stop()
                await app.shutdown()
            except Exception:
                pass
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)

    await http_runner.cleanup()

async def handle_republish_order(update: Update, context, state: Dict[str, Any], bid: str):
    """Переопубликовывает истёкшую заявку"""
    q = update.callback_query
    uid = q.from_user.id

    order = state["orders"].get(bid)
    if not order:
        logger.error(f"[repub] bid={bid[:8]} — order NOT in state")
        try:
            await q.edit_message_text("❌ Заявка не найдена.")
        except Exception:
            pass
        return

    creator_id = order.get("creator_id")
    if uid != creator_id and not is_admin(uid, state):
        try:
            await q.edit_message_text("❌ Только создатель может переопубликовать заявку.")
        except Exception:
            pass
        return

    if not order.get("expired"):
        logger.error(f"[repub] bid={bid[:8]} — expired=False")
        try:
            await q.edit_message_text("❌ Можно переопубликовать только истёкшие заявки.")
        except Exception:
            pass
        return

    logger.error(f"[repub] bid={bid[:8]} — starting republish, messages={len(order.get('messages', []))}")

    # Отменяем авто-задания старой заявки
    cancel_auto_delete_job(context, bid)
    if hasattr(context, 'job_queue') and context.job_queue:
        for jname in [f"expire:{bid}", f"forceclose:{bid}"]:
            for job in context.job_queue.get_jobs_by_name(jname):
                job.schedule_removal()

    # Запоминаем данные ДО удаления из стейта
    target_chats = [msg["chat_id"] for msg in order.get("messages", [])]

    # Удаляем старые сообщения из групповых чатов
    await _close_order_from_chats(bid, state, context.bot)

    # Создаём новую заявку как копию старой
    new_bid = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    new_order = order.copy()
    new_order.update({
        "created_at": created_at,
        "messages": [],
        "claimed_by": None,
        "expired": False,
        "bot_message_id": None,
        "bot_chat_id": None,
    })
    state["orders"][new_bid] = new_order

    # Отправляем в те же чаты
    ok = fail = 0
    sent_chat_names = []
    for chat_id in target_chats:
        try:
            msg = await send_message_safe(
                context.bot,
                chat_id=chat_id,
                text=render_message(new_bid, state, chat_id),
                parse_mode=safe_parse_mode(),
                reply_markup=build_keyboard(new_bid, state, chat_id),
                disable_web_page_preview=True
            )
            chat_id = _record_sent_message(state, state["orders"][new_bid], chat_id, msg)
            chat_info = state.get("chats", {}).get(str(chat_id), {})
            chat_name = html.escape(chat_info.get("name", f"Чат {chat_id}"))
            sent_chat_names.append(f"📍 {chat_name}")
            ok += 1
        except Exception as e:
            logger.error(f"[repub] send to chat {chat_id} failed: {e}")
            fail += 1

    save_state(state)

    # Планируем истечение новой заявки
    if hasattr(context, 'job_queue') and context.job_queue:
        await schedule_expiration(context, new_bid, new_order["ttl_min"])

    # Успешное уведомление
    chats_list = "\n".join(sent_chat_names) if sent_chat_names else "—"
    success_text = (
        f"✅ Заявка №{short_id(new_bid)} успешно переопубликована!\n"
        f"Отправлено в чаты:\n{chats_list}"
    )
    try:
        await q.edit_message_text(success_text, parse_mode=safe_parse_mode())
    except Exception as e:
        logger.error(f"[repub] edit_message_text failed: {e}")

    # Новая заявка с кнопками в личку
    order_data = new_order
    formatted_amount = (
        f"{order_data['amount']:,}" if order_data.get('amount') and isinstance(order_data['amount'], int)
        else order_data.get('amount', 'N/A')
    )
    payments_line = f"Платежей: {html.escape(str(order_data['payments']))}\n" if order_data.get('payments') else ""
    new_order_summary = (
        f"✅ Заявка #{short_id(new_bid)} переопубликована!\n\n"
        f"Направление: {html.escape(str(order_data.get('direction', 'N/A')))}\n"
        f"Сумма: {html.escape(str(formatted_amount))} RUB\n"
        f"Банк: {html.escape(str(order_data.get('bank', 'N/A')))}\n"
        f"{payments_line}"
        f"Курс: {html.escape(str(order_data.get('rate', 'N/A')))}\n"
        f"Срок: {escape_html(format_ttl_display(order_data['ttl_min']))}\n\n"
        f"Отправлено в чатов: {ok}, ошибок: {fail}"
    )
    control_keyboard = build_order_control_keyboard(new_bid)
    try:
        new_bot_message = await context.bot.send_message(
            chat_id=uid,
            text=new_order_summary,
            reply_markup=control_keyboard,
            parse_mode=safe_parse_mode()
        )
        state["orders"][new_bid]["bot_message_id"] = new_bot_message.message_id
        state["orders"][new_bid]["bot_chat_id"] = uid
        save_state(state)
    except Exception as e:
        logger.error(f"[repub] send bot message failed: {e}")

async def handle_close_expired_order(update: Update, context, state: Dict[str, Any], bid: str):
    """Закрывает истёкшую заявку"""
    q = update.callback_query
    uid = q.from_user.id

    order = state["orders"].get(bid)
    if not order:
        logger.error(f"[close_expired] bid={bid[:8]} — order NOT in state")
        try:
            await q.edit_message_text("❌ Заявка не найдена.")
        except Exception:
            pass
        return

    creator_id = order.get("creator_id")
    if uid != creator_id and not is_admin(uid, state):
        try:
            await q.edit_message_text("❌ Только создатель может закрыть заявку.")
        except Exception:
            pass
        return

    if not order.get("expired"):
        logger.error(f"[close_expired] bid={bid[:8]} — expired=False, cannot close")
        try:
            await q.edit_message_text("❌ Можно закрыть только истёкшие заявки.")
        except Exception:
            pass
        return

    # Отменяем авто-задания
    cancel_auto_delete_job(context, bid)
    if hasattr(context, 'job_queue') and context.job_queue:
        for job in context.job_queue.get_jobs_by_name(f"forceclose:{bid}"):
            job.schedule_removal()

    logger.error(f"[close_expired] bid={bid[:8]} — closing via _close_order_from_chats")
    await _close_order_from_chats(bid, state, context.bot)

    try:
        await q.edit_message_text(f"🗑️ Заявка №{short_id(bid)} закрыта.")
    except Exception as e:
        logger.error(f"[close_expired] edit_message_text failed: {e}")

async def handle_close_claimed_expired_order(update: Update, context, state: Dict[str, Any], bid: str):
    """Закрывает истёкшую заявку, которая была в работе"""
    q = update.callback_query
    uid = q.from_user.id
    
    order = state["orders"].get(bid)
    if not order:
        try:
            await q.edit_message_text("❌ Заявка не найдена.")
        except Exception:
            pass
        return

    creator_id = order.get("creator_id")
    if uid != creator_id and not is_admin(uid, state):
        try:
            await q.edit_message_text("❌ Только создатель может закрыть заявку.")
        except Exception:
            pass
        return

    if not order.get("expired"):
        try:
            await q.edit_message_text("❌ Можно закрыть только истёкшие заявки.")
        except Exception:
            pass
        return

    if not order.get("claimed_by"):
        try:
            await q.edit_message_text("❌ Эта заявка не находилась в работе.")
        except Exception:
            pass
        return

    # Отменяем автоудаление
    cancel_auto_delete_job(context, bid)

    # Сначала редактируем q.message (убираем кнопки), потом удаляем из групп
    logger.error(f"[close_claimed_expired] bid={bid[:8]} closing")
    try:
        await q.edit_message_text(f"✅ Заявка №{short_id(bid)} закрыта.")
    except Exception as e:
        logger.error(f"[close_claimed_expired] edit failed: {e}")

    await _close_order_from_chats(bid, state, context.bot, skip_bot_msg=True)


async def handle_template_manage_callback(update, context, state, data, action, parts):
    q = update.callback_query
    uid = q.from_user.id

    def _index():
        try:
            return int(parts[2])
        except (IndexError, ValueError):
            return None

    if action == "rename":
        index = _index()
        tpl = templates_store.get_template(state, uid, index) if index is not None else None
        if not tpl:
            await q.edit_message_text("❌ Шаблон не найден. Открой /templates заново.")
            return
        user_sessions[uid] = {"step": "template_rename", "data": {"rename_index": index}}
        await q.edit_message_text(
            f"✏️ Введи новое имя для «{escape_html(tpl['name'])}» (до 30 символов):",
            parse_mode=safe_parse_mode()
        )
        return

    if action == "del":
        index = _index()
        tpl = templates_store.get_template(state, uid, index) if index is not None else None
        if not tpl:
            await q.edit_message_text("❌ Шаблон не найден. Открой /templates заново.")
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"tpl:delok:{index}")],
            [InlineKeyboardButton("✖️ Отмена", callback_data="tpl:delno")],
        ])
        await q.edit_message_text(
            f"Удалить шаблон «{escape_html(tpl['name'])}»?",
            reply_markup=keyboard, parse_mode=safe_parse_mode()
        )
        return

    if action == "delok":
        index = _index()
        if index is None or not templates_store.delete_template(state, uid, index):
            await q.edit_message_text("❌ Шаблон не найден.")
            return
        save_state(state)
        await q.edit_message_text("🗑 Шаблон удалён.")
        return

    if action == "delno":
        await q.edit_message_text("✖️ Удаление отменено.")
        return


if __name__ == "__main__":
    asyncio.run(main())