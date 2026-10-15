"""Cached official category reference; dashboard reads never call Zakupay."""

import json
from datetime import datetime, timezone


SCHEMA = """CREATE TABLE IF NOT EXISTS zakupay_reference_cache (
    name TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
)"""


def category_id(value):
    if isinstance(value, bool):
        return ""
    text = str(value or "").strip()
    return str(int(text)) if text.isascii() and text.isdigit() and int(text) > 0 else ""


def normalize_categories(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("Закупай не вернул справочник категорий.")
    categories = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Некорректный справочник категорий.")
        key = category_id(row.get("id"))
        name = str(row.get("name") or "").strip()
        parent = row.get("parent")
        parent_id = category_id(parent.get("id") if isinstance(parent, dict) else parent)
        if not key or not name:
            raise ValueError("Некорректный справочник категорий.")
        entry = {"id": key, "name": name, "parent": {"id": parent_id} if parent_id else None}
        if key in categories and categories[key] != entry:
            raise ValueError("Неоднозначные идентификаторы категорий.")
        categories[key] = entry
    for key in categories:
        seen = {key}
        parent = categories[key]["parent"]
        while parent and parent["id"] in categories:
            if parent["id"] in seen:
                raise ValueError("Некорректная иерархия категорий.")
            seen.add(parent["id"])
            parent = categories[parent["id"]]["parent"]
    return list(categories.values())


def load_categories(conn):
    row = conn.execute("SELECT payload_json,updated_at FROM zakupay_reference_cache WHERE name='categories'").fetchone()
    if row:
        try:
            return normalize_categories(json.loads(row["payload_json"])), row["updated_at"]
        except (ValueError, TypeError):
            pass
    return [], ""


def save_categories(conn, execute, rows):
    categories = normalize_categories(rows)
    execute(conn, """INSERT INTO zakupay_reference_cache(name,payload_json,updated_at)
        VALUES ('categories',?,?) ON CONFLICT(name) DO UPDATE SET
        payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
        (json.dumps(categories, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
    return len(categories)


def category_choices(saved, reference):
    """Official IDs plus historical snapshot categories, without inventing nodes."""
    nodes = {row["id"]: row for row in reference}
    labels = {}
    for item in saved:
        for key, name in item["categories"].items():
            labels.setdefault(key, name)
    for key, row in nodes.items():
        path = [row["name"]]
        parent = row.get("parent")
        seen = {key}
        while parent and parent["id"] in nodes and parent["id"] not in seen:
            seen.add(parent["id"])
            node = nodes[parent["id"]]
            path.append(node["name"])
            parent = node.get("parent")
        labels[key] = " → ".join(reversed(path))
    return dict(sorted(labels.items(), key=lambda pair: (pair[1].casefold(), pair[0])))


def category_descendants(selected, reference):
    parents = {row["id"]: (row.get("parent") or {}).get("id") for row in reference}
    result = {selected}
    for key in parents:
        current, seen = key, set()
        while current and current not in seen:
            if current == selected:
                result.add(key)
                break
            seen.add(current)
            current = parents.get(current)
    return result
