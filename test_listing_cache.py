import json
import sqlite3
from datetime import date

import saved_dashboard as sd


def database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE automation_jobs (id INTEGER PRIMARY KEY, order_id INTEGER, event_type TEXT, subject TEXT, "
                 "status TEXT, viewed_at TEXT, order_json TEXT, result_json TEXT, updated_at TEXT)")
    for job_id, day in ((1, "2026-11-07"), (2, "2026-11-05"), (3, "2026-10-20")):
        order = dict(name=f"Заявка {job_id}", creationDate=day + "T10:00:00", orderItems=[dict(goodName="Труба", count=1, unit="шт")])
        conn.execute("INSERT INTO automation_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                     (job_id, 100 + job_id, "order", "", "new", None, json.dumps(order), "{}", "t0"))
    return conn


def counting(calls):
    def execute(conn, sql, params=()):
        calls.append(sql)
        return conn.execute(sql, params)
    return execute


def test_second_view_reads_only_change_marks_and_changed_requests():
    conn, calls = database(), []
    execute = counting(calls)
    assert len(sd.described_items(execute, conn, database="a")) == 3
    calls.clear()
    assert len(sd.described_items(execute, conn, database="a")) == 3
    assert calls == [sd.STAMP_QUERY]
    conn.execute("UPDATE automation_jobs SET updated_at='t1', subject='x' WHERE id=2")
    calls.clear()
    items = sd.described_items(execute, conn, database="a")
    assert len(calls) == 2 and "id IN (?)" in calls[1]
    conn.execute("DELETE FROM automation_jobs WHERE id=3")
    assert sorted(i["id"] for i in sd.described_items(execute, conn, database="a")) == [1, 2]
    assert len(items) == 3


def test_list_shows_last_three_days_unless_searching_or_all_dates():
    items = sd.described_items(counting([]), database(), database="b")
    page = sd.select_page(None, {}, items=items, recent_days=3, today=date(2026, 11, 7))
    assert [i["id"] for i in page["items"]] == [1, 2] and page["recent_from"] == date(2026, 11, 5)
    page = sd.select_page(None, {"all_dates": "1"}, items=[dict(i) for i in items], recent_days=3, today=date(2026, 11, 7))
    assert [i["id"] for i in page["items"]] == [1, 2, 3] and page["recent_from"] is None
    page = sd.select_page(None, {"order_id": "103"}, items=[dict(i) for i in items], recent_days=3, today=date(2026, 11, 7))
    assert [i["id"] for i in page["items"]] == [3]


def item(categories_and_quantities):
    rows = [dict(position=i, name="", search="", quantity=q, unit="шт", category=c)
            for i, (c, q) in enumerate(categories_and_quantities, 1)]
    return dict(quantity_rows=rows, categories={c: c for c, _ in categories_and_quantities})


def test_half_of_positions_or_one_large_position_in_my_rubrics():
    filters, _ = sd.filters_from_query({"category_id": "mine", "rubric_mode": "half"})
    assert sd.rubric_share_ok(item([("mine", 1), ("pipe", 1)]), filters, {"mine"})
    assert not sd.rubric_share_ok(item([("mine", 1), ("pipe", 1), ("pipe", 1)]), filters, {"mine"})
    filters, _ = sd.filters_from_query({"category_id": "mine", "rubric_mode": "half", "rubric_big_qty": "100"})
    assert sd.rubric_share_ok(item([("mine", 500), ("pipe", 1), ("pipe", 1)]), filters, {"mine"})
    assert not sd.rubric_share_ok(item([("mine", 50), ("pipe", 1000), ("pipe", 1)]), filters, {"mine"})
