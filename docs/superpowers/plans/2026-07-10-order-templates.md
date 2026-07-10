# Именованные шаблоны заявок — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать мерчанту сохранять параметры заявки как именованные шаблоны и создавать типовую заявку в 3 шага (шаблон → сумма → чаты) вместо 7.

**Architecture:** Чистая логика шаблонов (CRUD, валидация) выносится в отдельный модуль `templates_store.py` — тестируется автономно на `python3` без Telegram. UI и хендлеры остаются в `simple_bot.py`, изолированы от старого флоу новыми значениями `step` и callback-префиксом `tpl:`. Старый путь `/create` (без шаблонов) не меняется.

**Tech Stack:** Python 3.10+, python-telegram-bot 22.8. Тесты — self-contained assert-скрипты на stdlib (pytest в проекте нет).

**Спека:** `docs/superpowers/specs/2026-07-10-order-templates-design.md`

---

## Структура файлов

| Файл | Ответственность | Действие |
|------|-----------------|----------|
| `templates_store.py` | Чистый CRUD шаблонов + валидация над dict `state` | Создать |
| `tests/test_templates_store.py` | Автономные assert-тесты чистой логики | Создать |
| `simple_bot.py` | UI, хендлеры, роутинг; импорт из `templates_store` | Модифицировать |

**Примечание про snapshot:** шаблон хранит только `direction, bank, payments, rate, ttl_min` — эти поля есть в объекте `order` после публикации. `banks` (список для галочек) НЕ хранится: в шаблонном флоу шаг выбора банка пропускается, а `finalize` использует строку `bank`. Это осознанное упрощение относительно спеки §1.

---

## Task 1: Модуль хранилища шаблонов (чистая логика + тесты)

**Files:**
- Create: `templates_store.py`
- Test: `tests/test_templates_store.py`

- [ ] **Step 1: Написать падающий тест**

Create `tests/test_templates_store.py`:

```python
"""Автономные тесты templates_store. Запуск: python3 tests/test_templates_store.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import templates_store as ts


def fresh_state():
    return {}


def test_snapshot_extracts_only_known_fields():
    order = {
        "direction": "Продам RUB - возьму USDT",
        "amount": 150000,
        "bank": "СБЕР",
        "payments": "до 2 платежей",
        "rate": "байбит средний",
        "ttl_min": 180,
        "creator_id": 42,
        "messages": [],
    }
    snap = ts.snapshot_from_order(order)
    assert snap == {
        "direction": "Продам RUB - возьму USDT",
        "bank": "СБЕР",
        "payments": "до 2 платежей",
        "rate": "байбит средний",
        "ttl_min": 180,
    }, snap


def test_snapshot_skips_missing_and_none():
    order = {"direction": "d", "bank": "СБЕР", "rate": None, "ttl_min": 30}
    snap = ts.snapshot_from_order(order)
    assert snap == {"direction": "d", "bank": "СБЕР", "ttl_min": 30}, snap


def test_add_and_list():
    st = fresh_state()
    ok, code = ts.add_template(st, 1, "СБЕР-продажа", {"bank": "СБЕР", "ttl_min": 30})
    assert ok and code == "ok", (ok, code)
    items = ts.list_templates(st, 1)
    assert len(items) == 1 and items[0]["name"] == "СБЕР-продажа", items


def test_add_duplicate_without_overwrite_fails():
    st = fresh_state()
    ts.add_template(st, 1, "X", {"bank": "СБЕР"})
    ok, code = ts.add_template(st, 1, "X", {"bank": "АЛЬФА"})
    assert not ok and code == "exists", (ok, code)
    assert len(ts.list_templates(st, 1)) == 1


def test_add_duplicate_with_overwrite_replaces():
    st = fresh_state()
    ts.add_template(st, 1, "X", {"bank": "СБЕР"})
    ok, code = ts.add_template(st, 1, "X", {"bank": "АЛЬФА"}, overwrite=True)
    assert ok and code == "ok", (ok, code)
    items = ts.list_templates(st, 1)
    assert len(items) == 1 and items[0]["bank"] == "АЛЬФА", items


def test_limit_enforced():
    st = fresh_state()
    for i in range(ts.MAX_TEMPLATES):
        ok, _ = ts.add_template(st, 1, f"t{i}", {"bank": "СБЕР"})
        assert ok
    ok, code = ts.add_template(st, 1, "over", {"bank": "СБЕР"})
    assert not ok and code == "limit", (ok, code)


def test_templates_are_per_user():
    st = fresh_state()
    ts.add_template(st, 1, "A", {"bank": "СБЕР"})
    ts.add_template(st, 2, "B", {"bank": "АЛЬФА"})
    assert len(ts.list_templates(st, 1)) == 1
    assert len(ts.list_templates(st, 2)) == 1
    assert ts.list_templates(st, 1)[0]["name"] == "A"


def test_get_template_bounds():
    st = fresh_state()
    ts.add_template(st, 1, "A", {"bank": "СБЕР"})
    assert ts.get_template(st, 1, 0)["name"] == "A"
    assert ts.get_template(st, 1, 5) is None
    assert ts.get_template(st, 1, -1) is None


def test_validate_name():
    assert ts.validate_name("  hi  ") == (True, "hi")
    assert ts.validate_name("") == (False, "empty")
    assert ts.validate_name("   ") == (False, "empty")
    ok, code = ts.validate_name("x" * 31)
    assert not ok and code == "too_long", (ok, code)
    assert ts.validate_name("x" * 30)[0] is True


def test_rename():
    st = fresh_state()
    ts.add_template(st, 1, "A", {"bank": "СБЕР"})
    ts.add_template(st, 1, "B", {"bank": "АЛЬФА"})
    ok, code = ts.rename_template(st, 1, 0, "C")
    assert ok and code == "ok", (ok, code)
    assert ts.list_templates(st, 1)[0]["name"] == "C"
    # переименование в существующее имя другого шаблона запрещено
    ok, code = ts.rename_template(st, 1, 0, "B")
    assert not ok and code == "exists", (ok, code)
    # переименование в своё же имя разрешено (no-op по имени)
    ok, code = ts.rename_template(st, 1, 1, "B")
    assert ok and code == "ok", (ok, code)
    # выход за границы
    ok, code = ts.rename_template(st, 1, 9, "Z")
    assert not ok and code == "not_found", (ok, code)


def test_delete():
    st = fresh_state()
    ts.add_template(st, 1, "A", {"bank": "СБЕР"})
    ts.add_template(st, 1, "B", {"bank": "АЛЬФА"})
    assert ts.delete_template(st, 1, 0) is True
    assert [t["name"] for t in ts.list_templates(st, 1)] == ["B"]
    assert ts.delete_template(st, 1, 5) is False


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n✅ {len(tests)} tests passed")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 tests/test_templates_store.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'templates_store'`

- [ ] **Step 3: Написать модуль**

Create `templates_store.py`:

```python
"""Хранилище именованных шаблонов заявок.

Чистая логика без Telegram: работает с dict `state`, поэтому тестируется
автономно. Шаблоны лежат в state["templates"][str(uid)] списком объектов
{"name": str, "direction": str, "bank": str, "payments": str, "rate": str,
 "ttl_min": int}. amount и target_chats в шаблон не входят.
"""

MAX_TEMPLATES = 10
MAX_NAME_LEN = 30
SNAPSHOT_FIELDS = ("direction", "bank", "payments", "rate", "ttl_min")


def _bucket(state, uid):
    """Список шаблонов пользователя с созданием контейнеров при отсутствии."""
    return state.setdefault("templates", {}).setdefault(str(uid), [])


def list_templates(state, uid):
    return state.get("templates", {}).get(str(uid), [])


def get_template(state, uid, index):
    items = list_templates(state, uid)
    if 0 <= index < len(items):
        return items[index]
    return None


def snapshot_from_order(order):
    """Достаёт из заявки только поля, из которых состоит шаблон."""
    snap = {}
    for f in SNAPSHOT_FIELDS:
        if f in order and order[f] is not None:
            snap[f] = order[f]
    return snap


def validate_name(name):
    """(True, cleaned) | (False, 'empty'|'too_long')."""
    cleaned = (name or "").strip()
    if not cleaned:
        return False, "empty"
    if len(cleaned) > MAX_NAME_LEN:
        return False, "too_long"
    return True, cleaned


def name_exists(state, uid, name):
    return any(t.get("name") == name for t in list_templates(state, uid))


def find_index_by_name(state, uid, name):
    for i, t in enumerate(list_templates(state, uid)):
        if t.get("name") == name:
            return i
    return -1


def add_template(state, uid, name, snapshot, overwrite=False):
    """(ok, code). code in {'ok','exists','limit'}."""
    bucket = _bucket(state, uid)
    existing = find_index_by_name(state, uid, name)
    if existing >= 0:
        if not overwrite:
            return False, "exists"
        bucket[existing] = {"name": name, **snapshot}
        return True, "ok"
    if len(bucket) >= MAX_TEMPLATES:
        return False, "limit"
    bucket.append({"name": name, **snapshot})
    return True, "ok"


def rename_template(state, uid, index, new_name):
    """(ok, code). code in {'ok','empty','too_long','exists','not_found'}."""
    ok, res = validate_name(new_name)
    if not ok:
        return False, res
    items = list_templates(state, uid)
    if not (0 <= index < len(items)):
        return False, "not_found"
    if items[index].get("name") != res and name_exists(state, uid, res):
        return False, "exists"
    items[index]["name"] = res
    return True, "ok"


def delete_template(state, uid, index):
    items = list_templates(state, uid)
    if 0 <= index < len(items):
        items.pop(index)
        return True
    return False
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 tests/test_templates_store.py`
Expected: PASS — `✅ 11 tests passed`

- [ ] **Step 5: Коммит**

```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код"
git add templates_store.py tests/test_templates_store.py
git commit -m "feat: модуль templates_store с чистой логикой шаблонов + тесты"
```

---

## Task 2: Инициализация ключа templates в стейте

**Files:**
- Modify: `simple_bot.py` (импорт вверху; `load_state` ~217-224)

- [ ] **Step 1: Добавить импорт модуля**

В `simple_bot.py` рядом с остальными import (после блока `try: import telegram`), добавить строку:

```python
import templates_store
```

- [ ] **Step 2: Инициализировать ключ в load_state**

В `load_state`, в блоке `data.setdefault(...)` (там, где `data.setdefault("ratings", {})`), добавить строкой ниже:

```python
    data.setdefault("templates", {})
```

- [ ] **Step 3: Проверить компиляцию**

Run: `cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 -m py_compile simple_bot.py && echo OK`
Expected: `OK`

- [ ] **Step 4: Проверить, что старый стейт не ломается**

Run:
```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 -c "
import simple_bot
s = simple_bot.load_state()
assert 'templates' in s and isinstance(s['templates'], dict)
print('OK templates key present:', s['templates'])
"
```
Expected: `OK templates key present: {}` (или существующее содержимое)

- [ ] **Step 5: Коммит**

```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код"
git add simple_bot.py
git commit -m "feat: инициализация state['templates'] и импорт templates_store"
```

---

## Task 3: Хелпер клавиатуры заявки + кнопка «В шаблон»

Убирает дублирование `control_keyboard` (4 копии) и добавляет кнопку сохранения шаблона.

**Files:**
- Modify: `simple_bot.py` — новый хелпер; замена в **5 идентичных местах** построения `control_keyboard` (проверено: набор кнопок везде одинаков — remind_bot/edit_amount/send_remaining/close, поэтому замена безопасна):
  1. `on_callback`, блок отмены редактирования суммы (~1598), переменная `bid`
  2. `finalize_order_creation` (~2875), переменная `bid`
  3. `handle_back_to_control` (~3635), переменная `bid`
  4. `handle_amount_edit_input`, после изменения суммы (~3775), переменная `bid`
  5. `handle_republish_order` (~4171), переменная **`new_bid`**

- [ ] **Step 1: Добавить хелпер рядом с build_keyboard**

Найти определение `def build_keyboard(` и над ним добавить:

```python
def build_order_control_keyboard(bid: str) -> InlineKeyboardMarkup:
    """Клавиатура управления заявкой в личке мерчанта (сводка после публикации)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Напомнить о заявке", callback_data=f"remind_bot:{bid}")],
        [InlineKeyboardButton("📝 Редактировать сумму", callback_data=f"edit_amount:{bid}")],
        [InlineKeyboardButton("💾 В шаблон", callback_data=f"tpl:save:{bid}")],
        [InlineKeyboardButton("📤 Отправить в оставшиеся чаты", callback_data=f"send_remaining:{bid}")],
        [InlineKeyboardButton("🗑️ Закрыть заявку", callback_data=f"close:{bid}")]
    ])
```

- [ ] **Step 2: Заменить 5 копий на вызов хелпера**

В каждом из 5 мест (см. список Files выше — искать по `callback_data=f"remind_bot:`) заменить литерал `InlineKeyboardMarkup([... remind_bot ... close ...])`, присваиваемый переменной `control_keyboard`, на вызов хелпера с правильной переменной id:

- места 1-4 (строки ~1598, ~2875, ~3635, ~3775): `control_keyboard = build_order_control_keyboard(bid)`
- место 5 (`handle_republish_order`, ~4171): `control_keyboard = build_order_control_keyboard(new_bid)`

> Все 5 клавиатур проверены и идентичны по набору кнопок — замена не меняет UX нигде, кроме добавления «💾 В шаблон» во всех местах консистентно.

- [ ] **Step 3: Проверить компиляцию**

Run: `cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 -m py_compile simple_bot.py && echo OK`
Expected: `OK`

- [ ] **Step 4: Проверить, что кнопка присутствует ровно в хелпере**

Run:
```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 -c "
import simple_bot
kb = simple_bot.build_order_control_keyboard('abc')
flat = [b.callback_data for row in kb.inline_keyboard for b in row]
assert 'tpl:save:abc' in flat, flat
assert 'close:abc' in flat and 'remind_bot:abc' in flat, flat
print('OK keyboard:', flat)
"
```
Expected: `OK keyboard: [...'tpl:save:abc'...]`

- [ ] **Step 5: Коммит**

```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код"
git add simple_bot.py
git commit -m "refactor: хелпер build_order_control_keyboard + кнопка 'В шаблон'"
```

---

## Task 4: Хелпер клавиатуры выбора чатов (DRY для шаблонного флоу)

Выносит построение кнопок чатов, чтобы переиспользовать в шаблонном флоу без дублирования.

**Files:**
- Modify: `simple_bot.py` — новый хелпер; рефактор `show_chat_selection` (~2732)

- [ ] **Step 1: Добавить хелпер над show_chat_selection**

```python
def build_chat_keyboard(available_chats, state, include_back=True):
    """Клавиатура выбора целевых чатов: 'Все чаты' + по чату + опц. 'Назад'."""
    buttons = [[InlineKeyboardButton("Все чаты", callback_data="chat:all")]]
    for chat_id in available_chats[:30]:
        chat_name = state["chats"].get(str(chat_id), {}).get("name", f"Чат {chat_id}")
        buttons.append([InlineKeyboardButton(f"📍 {chat_name}", callback_data=f"chat:{chat_id}")])
    if include_back:
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(buttons)
```

- [ ] **Step 2: Использовать хелпер в show_chat_selection**

В `show_chat_selection` заменить ручное построение `buttons`/`keyboard` (блок от `buttons = [[InlineKeyboardButton("Все чаты"...` до `keyboard = InlineKeyboardMarkup(buttons)`) на:

```python
    keyboard = build_chat_keyboard(available_chats, state, include_back=True)
```

Строки с `await q.edit_message_text("🎯 Выберите целевые чаты:", reply_markup=keyboard)` оставить без изменений.

- [ ] **Step 3: Проверить компиляцию + хелпер**

Run:
```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 -m py_compile simple_bot.py && python3 -c "
import simple_bot
st = {'chats': {'-100': {'id': -100, 'name': 'OTC'}}}
kb = simple_bot.build_chat_keyboard([-100], st)
flat = [b.callback_data for row in kb.inline_keyboard for b in row]
assert 'chat:all' in flat and 'chat:-100' in flat and 'back' in flat, flat
print('OK', flat)
"`
```
Expected: `OK ['chat:all', 'chat:-100', 'back']`

- [ ] **Step 4: Коммит**

```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код"
git add simple_bot.py
git commit -m "refactor: вынести build_chat_keyboard для переиспользования"
```

---

## Task 5: /create показывает список шаблонов

**Files:**
- Modify: `simple_bot.py` — `create_order_start` (~1255)

- [ ] **Step 1: Показать шаблоны, если они есть**

В `create_order_start`, после блока инициализации сессии `user_sessions[uid] = {...}`, но ПЕРЕД показом клавиатуры направления, вставить ветвление. Заменить хвост функции (от создания сессии до `await update.message.reply_text("🏗 Создание заявки...")`) на:

```python
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
```

- [ ] **Step 2: Проверить компиляцию**

Run: `cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 -m py_compile simple_bot.py && echo OK`
Expected: `OK`

- [ ] **Step 3: Коммит**

```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код"
git add simple_bot.py
git commit -m "feat: /create показывает список шаблонов если они есть"
```

---

## Task 6: Роутинг tpl: и обработка выбора/создания шаблона

**Files:**
- Modify: `simple_bot.py` — `on_callback` (добавить блок `tpl:` после `q.answer()`, ~1590); новый обработчик `handle_template_callback`

- [ ] **Step 1: Добавить диспетчер tpl: в on_callback**

В `on_callback`, сразу после блока `try: await q.answer() ... except: pass` (~1590), вставить:

```python
    if data.startswith("tpl:"):
        await handle_template_callback(update, context, state, data)
        return
```

- [ ] **Step 2: Написать handle_template_callback (use / new; save/rename/del — в следующих тасках)**

Добавить новую функцию (например, рядом с `create_order_start`):

```python
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

    # save / rename / del / overwrite / delok / delno — добавляются в Tasks 7-9
    await handle_template_manage_callback(update, context, state, data, action, parts)
```

> Заглушка `handle_template_manage_callback` реализуется в Task 8. Чтобы Task 6 компилировался и работал изолированно, временно добавить в конец файла минимальную заглушку:

```python
async def handle_template_manage_callback(update, context, state, data, action, parts):
    await update.callback_query.edit_message_text("⚙️ В разработке.")
```

(Заглушка будет заменена полной реализацией в Task 8 — там же удалить эту версию.)

- [ ] **Step 3: Проверить компиляцию**

Run: `cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 -m py_compile simple_bot.py && echo OK`
Expected: `OK`

- [ ] **Step 4: Коммит**

```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код"
git add simple_bot.py
git commit -m "feat: роутинг tpl: и обработка выбора/создания шаблона (use/new)"
```

---

## Task 7: Ввод суммы в шаблонном флоу → выбор чатов

**Files:**
- Modify: `simple_bot.py` — `handle_message` (~3027, добавить ветку `template_amount`)

- [ ] **Step 1: Добавить ветку template_amount в handle_message**

В `handle_message`, после ветки `elif session["step"] == "custom_rate":` и перед блоком `# Обработка команды /back`, добавить:

```python
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
```

> Примечание 1: `include_back=False` — в шаблонном флоу возврат «Назад» из чатов не нужен (сумма — единственный предыдущий шаг, мерчант просто заново вводит сумму или шлёт /create). Это закрывает камень №5 из спеки: мы не даём кнопку, ведущую на несуществующий шаг ttl.
>
> Примечание 2 (осознанный компромисс по С2): при `len(available_chats) == 1` НЕ зеркалим авто-публикацию обычного флоу. Причина: `finalize_order_creation` завязана на callback `q.edit_message_text`, которого при текстовом вводе суммы нет — авто-публикация потребовала бы дубля всей `finalize` (~70 строк) ради экономии одного тапа. Показываем клавиатуру с единственным чатом (+«Все чаты»); мерчант делает один явный тап. Приемлемо и без дублирования кода.

- [ ] **Step 2: Проверить компиляцию**

Run: `cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 -m py_compile simple_bot.py && echo OK`
Expected: `OK`

- [ ] **Step 3: Проверить, что выбор чата ведёт в finalize**

Логика: после `step="chats"` нажатие `chat:...` перехватывается зонтиком `on_callback` (`chat:` в SESSION_PREFIXES, сессия активна) → `handle_order_creation` ветка `step=="chats"` → `finalize_order_creation(q, ...)`. `q` здесь настоящий callback от кнопки чата, `q.edit_message_text` работает. Изменений в этих функциях не требуется.

Ручная сверка (без запуска бота): открыть `handle_order_creation`, ветка `elif session["step"] == "chats":` — убедиться, что `data.startswith("chat:")` → `finalize_order_creation(q, ...)`. Отметить галочкой после визуальной проверки.

- [ ] **Step 4: Коммит**

```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код"
git add simple_bot.py
git commit -m "feat: ввод суммы в шаблонном флоу ведёт к выбору чатов и публикации"
```

---

## Task 8: Сохранение шаблона из заявки (кнопка → имя)

**Files:**
- Modify: `simple_bot.py` — расширить `handle_template_callback` (save + overwrite); заменить заглушку `handle_template_manage_callback`; добавить ветку `template_name` в `handle_message`

- [ ] **Step 1: Добавить ветку template_name в handle_message**

В `handle_message`, после ветки `template_amount`, добавить:

```python
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
        del user_sessions[uid]
        await update.message.reply_text(
            f"✅ Шаблон «{escape_html(res)}» сохранён. Теперь /create предложит его.",
            parse_mode=safe_parse_mode()
        )
        return
```

- [ ] **Step 2: Реализовать save/overwrite/cancel в handle_template_callback**

Заменить в `handle_template_callback` последнюю строку (вызов `handle_template_manage_callback`) на полноценную обработку. Заменить блок:

```python
    # save / rename / del / overwrite / delok / delno — добавляются в Tasks 7-9
    await handle_template_manage_callback(update, context, state, data, action, parts)
```

на:

```python
    if action == "save":
        bid = parts[2] if len(parts) > 2 else ""
        order = state["orders"].get(bid)
        if not order:
            await q.edit_message_text("❌ Заявка закрыта, шаблон не сохранить.")
            return
        snapshot = templates_store.snapshot_from_order(order)
        user_sessions[uid] = {
            "step": "template_name",
            "data": {"tpl_snapshot": snapshot},
        }
        await q.edit_message_text("💾 Введи имя шаблона (до 30 символов):")
        return

    if action == "overwrite":
        session = user_sessions.get(uid)
        if not session or "pending_name" not in session.get("data", {}):
            await q.edit_message_text("❌ Нечего перезаписывать. Начни заново через заявку.")
            return
        name = session["data"]["pending_name"]
        snapshot = session["data"].get("tpl_snapshot", {})
        templates_store.add_template(state, uid, name, snapshot, overwrite=True)
        save_state(state)
        del user_sessions[uid]
        await q.edit_message_text(
            f"✅ Шаблон «{escape_html(name)}» перезаписан.", parse_mode=safe_parse_mode()
        )
        return

    if action == "cancel":
        user_sessions.pop(uid, None)
        await q.edit_message_text("✖️ Отменено.")
        return

    await handle_template_manage_callback(update, context, state, data, action, parts)
```

- [ ] **Step 3: Проверить компиляцию**

Run: `cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 -m py_compile simple_bot.py && echo OK`
Expected: `OK`

- [ ] **Step 4: Проверить сохранение end-to-end на чистой логике**

Run:
```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 -c "
import templates_store as ts
st = {}
order = {'direction': 'Продам RUB - возьму USDT', 'amount': 150000, 'bank': 'СБЕР', 'payments': '1 платеж', 'rate': 'байбит средний', 'ttl_min': 180}
snap = ts.snapshot_from_order(order)
ok, code = ts.add_template(st, 7, 'СБЕР-продажа', snap)
assert ok, code
t = ts.get_template(st, 7, 0)
assert 'amount' not in t and t['bank']=='СБЕР' and t['name']=='СБЕР-продажа', t
print('OK saved template:', t)
"
```
Expected: `OK saved template: {...без amount...}`

- [ ] **Step 5: Коммит**

```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код"
git add simple_bot.py
git commit -m "feat: сохранение шаблона из заявки (имя, дубль→перезапись, лимит)"
```

---

## Task 9: Команда /templates (просмотр, переименование, удаление)

**Files:**
- Modify: `simple_bot.py` — новая команда `templates_cmd`; полная реализация `handle_template_manage_callback` (rename/del/delok/delno); ветка `template_rename` в `handle_message`; регистрация в `main` и `set_user_commands`

- [ ] **Step 1: Добавить команду templates_cmd**

Рядом с `cmd_stats` добавить:

```python
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
        buttons.append([
            InlineKeyboardButton(f"✏️ {t['name'][:15]}", callback_data=f"tpl:rename:{i}"),
            InlineKeyboardButton("🗑", callback_data=f"tpl:del:{i}"),
        ])
    await send_func(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
    )
```

- [ ] **Step 2: Реализовать handle_template_manage_callback**

Заменить заглушку `handle_template_manage_callback` (из Task 6) на:

```python
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
```

- [ ] **Step 3: Добавить ветку template_rename в handle_message**

После ветки `template_name` в `handle_message`:

```python
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
```

- [ ] **Step 4: Зарегистрировать команду в main и в меню**

> ⚠️ КРИТИЧНО: в `main` хендлеры регистрируются внутри вложенной функции `def _build_app()` на переменную **`_app`** (НЕ `application`). И обработчик обязан стоять ДО `_app.add_handler(MessageHandler(filters.COMMAND, unknown))` (последняя строка блока, ~3991), иначе `unknown` перехватит `/templates`.

В `_build_app()`, среди других `_app.add_handler(CommandHandler(...))` — например, сразу после строки с `cmd_listmerchants` (~3988) и ПЕРЕД `_app.add_handler(CallbackQueryHandler(on_callback))`, добавить:

```python
        _app.add_handler(CommandHandler("templates", templates_cmd))
```

В `set_user_commands`, в список команд для роли merchant И admin (там, где `/create`, `/myorders`), добавить:

```python
        BotCommand("templates", "Мои шаблоны заявок"),
```

(Сверить фактический способ формирования списка `BotCommand` в функции и добавить в ветки merchant/admin, НЕ в обычного пользователя.)

- [ ] **Step 5: Проверить компиляцию**

Run: `cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 -m py_compile simple_bot.py && echo OK`
Expected: `OK`

- [ ] **Step 6: Коммит**

```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код"
git add simple_bot.py
git commit -m "feat: команда /templates — просмотр, переименование, удаление шаблонов"
```

---

## Task 10: Финальная проверка

**Files:** нет изменений — только верификация

- [ ] **Step 1: Компиляция и юнит-тесты**

Run:
```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 -m py_compile simple_bot.py templates_store.py && python3 tests/test_templates_store.py
```
Expected: `✅ 11 tests passed` и без ошибок компиляции

- [ ] **Step 2: Проверить загрузку модуля и наличие всех хендлеров**

Run:
```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && python3 -c "
import simple_bot as b
for name in ['templates_cmd', 'handle_template_callback', 'handle_template_manage_callback', 'build_order_control_keyboard', 'build_chat_keyboard', '_render_templates_list']:
    assert hasattr(b, name), 'MISSING: ' + name
assert 'tpl:save:x' in [x for x in [b.build_order_control_keyboard('x').inline_keyboard[2][0].callback_data]]
print('OK all handlers present')
"
```
Expected: `OK all handlers present`

- [ ] **Step 3: Убедиться, что заглушка из Task 6 удалена**

Run: `cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && grep -n "⚙️ В разработке" simple_bot.py; echo "exit $?"`
Expected: пусто, `exit 1` (строки не найдено — заглушка заменена в Task 9)

- [ ] **Step 3b: Проверить регистрацию /templates (защита от бага К1 — `application` vs `_app`)**

Run:
```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код" && \
  grep -n 'CommandHandler("templates"' simple_bot.py && \
  grep -c 'application.add_handler' simple_bot.py
```
Expected: строка с `_app.add_handler(CommandHandler("templates", templates_cmd))` найдена; второй grep выводит `0` (нигде не используется несуществующая `application`). Дополнительно глазами сверить: строка регистрации `/templates` стоит ВЫШЕ `_app.add_handler(MessageHandler(filters.COMMAND, unknown))`.

- [ ] **Step 4: Ручной smoke-чеклист (запуск бота в тестовом окружении)**

Запустить бота с тестовым токеном (НЕ прод — конфликт polling!) и проверить в личке:
1. `/create` без шаблонов → выбор направления (старый флоу цел).
2. Создать заявку обычным флоу → на сводке есть кнопка «💾 В шаблон».
3. «💾 В шаблон» → ввести имя → «Шаблон сохранён».
4. `/create` → появился список с шаблоном + «Новую с нуля».
5. Выбрать шаблон → ввести сумму → выбор чатов → заявка опубликована; направление/банк/курс/TTL совпадают с шаблоном.
6. «➕ Новую с нуля» → обычный флоу с направления.
7. `/templates` → список; переименовать; удалить с подтверждением.
8. Сохранить шаблон с существующим именем → предложение перезаписать.
9. Создать 10 шаблонов, 11-й → сообщение о лимите.

- [ ] **Step 5: Обновить документацию**

В `Claude.md` (или `CLAUDE.md`): в таблицу команд добавить `/templates`, в разделе про создание заявки упомянуть шаблоны, в «Файлы проекта» добавить `templates_store.py` и `tests/`. Коммит:

```bash
cd "/Users/kirakosyanlevon/Desktop/p2p бот код"
git add Claude.md
git commit -m "docs: описать шаблоны заявок и /templates"
```

---

## Self-Review чеклист (для автора плана — выполнено)

- **Покрытие спеки:** §1 модель данных → Task 1-2; §2 создание → Task 3, 8; §3 использование → Task 5, 6, 7; §4 /templates → Task 9; §5 крайние случаи → Task 8 (дубль/лимит/валидация), Task 6 (границы индекса); §6 точки интеграции и 6 камней → распределены (камень 1 → Task 7 `template_amount`; камень 2 → Task 8 snapshot из order; камень 3 → Task 3 хелпер; камень 4 → Task 7-9 ветки step; камень 5 → Task 7 `include_back=False`; камень 6 → без изменений).
- **Placeholder-скан:** заглушка `handle_template_manage_callback` намеренная и удаляется в Task 9 (проверка в Task 10 Step 3).
- **Консистентность типов:** `templates_store` API (add_template/rename_template/delete_template/get_template/list_templates/snapshot_from_order/validate_name/MAX_TEMPLATES) едино в тестах и вызовах; callback-схема `tpl:use:N` / `tpl:new` / `tpl:save:{bid}` / `tpl:rename:N` / `tpl:del:N` / `tpl:delok:N` / `tpl:delno` / `tpl:overwrite` / `tpl:cancel` согласована между генерацией кнопок и диспетчером.

## Учтённые находки независимого ревью (2026-07-10)

- **К1 (критично):** в `main` переменная приложения — `_app` внутри `def _build_app()`, НЕ `application`. Task 9 Step 4 исправлен; добавлена проверка Task 10 Step 3b. Порядок регистрации — до `MessageHandler(filters.COMMAND, unknown)`.
- **С1:** мест построения `control_keyboard` — 5, не 4 (включая `handle_amount_edit_input` ~3774). Task 3 исправлен, все 5 адресов уточнены, клавиатуры проверены как идентичные.
- **С2:** при 1 доступном чате в шаблонном флоу авто-публикацию НЕ зеркалим (осознанный компромисс — избегаем дубля `finalize`; см. примечание в Task 7).
- **М1:** в список шаблонов (`/create`) и в цепочки ввода имени добавлена кнопка «✖️ Отмена» (`tpl:cancel`).
- **М3:** защита от повреждённого шаблона (нет обязательного поля) — проверка перед prefill в Task 6, чтобы не упасть в `schedule_expiration(None)`.
- **Подтверждено рабочим:** роутинг `tpl:` не перехватывается зонтиком `SESSION_PREFIXES`; новые `elif`-ветки `handle_message` не ломают существующие; `q` при выборе чата реальный — `finalize` работает; все API-символы существуют.
