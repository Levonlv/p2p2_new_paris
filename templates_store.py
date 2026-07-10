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
