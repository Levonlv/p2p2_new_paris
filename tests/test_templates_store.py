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
