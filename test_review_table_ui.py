"""Review table in a real browser: every found item is listed under its supplier, values come only from found
names, ✓/✕ wait for «ОК», and «ОК» applies the choice to every supplier column."""
import gc
import os
import socket
import tempfile
import threading
import time
from datetime import date, timedelta
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("playwright.sync_api")
import requests  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import automation_pipeline as p  # noqa: E402
import price_lists as prices  # noqa: E402
import supplier_comparison as sc  # noqa: E402
from test_price_lists_catalog import settings, workbook  # noqa: E402

DAY = (date.today() - timedelta(days=1)).isoformat()


class Inline:
    def __init__(self, target=None, **kwargs):
        self.target = target

    def start(self):
        self.target()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_found_items_staged_choices_and_ok_for_all_supplier_columns():
    tmp = tempfile.mkdtemp()
    port = free_port()
    with patch.object(sc, "_session", lambda request: None), patch.multiple(
            p, DB_PATH=os.path.join(tmp, "j.db"), DATABASE_URL="", _initialized_database_key=None):
        app = FastAPI()
        prices.install_price_lists(app)
        p.install_automation_pipeline(app, Mock(), fetch_all_orders=Mock(return_value=[]))
        sc.install_supplier_comparison(app)
        for supplier, base in (("Сатурн", 3000), ("Вега", 3300)):
            rows = [["Наименование", "Цена", "Артикул", "Ед. изм.", "Остаток"]]
            rows += [[f"Монолитный поликарбонат прозрачный {t} мм 2050х3050", base + t * 200, f"{supplier[:2]}{t}", "лист", None]
                     for t in (4, 6, 8, 10)]
            prices.activate_list(prices.stage_file(workbook(rows), f"{supplier}.xlsx", supplier, "Основной", DAY), settings())
        order = {"id": 555, "name": "UI", "delay": 0, "customer": {"name": "X", "inn": "7716997861", "kpp": "770801001"},
                 "orderItems": [{"goodName": "Монолитный поликарбонат 8 мм прозрачный 2050×3050", "count": 10, "unit": {"name": "лист"}},
                                {"goodName": "Зубило алмазное космическое 17 мм", "count": 1, "unit": {"name": "шт"}}]}
        job = p.process_api_order(order)["job_id"]
        real = p.threading.Thread
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        time.sleep(1.5)
        try:
            with patch.object(p.threading, "Thread", lambda *a, **k: Inline(*a, **k) if str(k.get("name") or "").startswith("supplier-search-") else real(*a, **k)):
                requests.post(f"http://127.0.0.1:{port}/dashboard/automation/jobs/{job}/start",
                              data={"selected_position": ["1", "2"]}, allow_redirects=False)
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport={"width": 1900, "height": 1100})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{port}/dashboard/automation/jobs/{job}/review")
                page.wait_for_selector('[data-value-position="1"] .value-chip')

                def row_cells(position):
                    tr = page.query_selector(f'[data-value-position="{position}"]').evaluate_handle("e=>e.closest('tr')").as_element()
                    return {th: td for th, td in zip([t.inner_text() for t in page.query_selector_all("#review-table tr:first-child th")][5:],
                                                     tr.query_selector_all("td[data-supplier-column]"))}

                # 1. every found item of a supplier is listed in its own column by default
                cells = row_cells(1)
                for supplier in ("Сатурн", "Вега"):
                    assert len(cells[supplier].query_selector_all(".supplier-item")) == 4, supplier
                    assert "Найдено: 4" in cells[supplier].inner_text()
                # 2. nothing found -> no values, no OK bar, no invented items
                empty = page.query_selector('[data-value-position="2"]')
                assert not empty.query_selector_all(".value-chip") and not empty.query_selector_all(".value-ok-bar")
                assert not row_cells(2)["Сатурн"].query_selector_all(".supplier-item")
                # 3. values come with the names they were found in
                cell = page.query_selector('[data-value-position="1"]')
                names = cell.query_selector(".found-names")
                names.query_selector("summary").click()
                text = names.inner_text()
                assert "4мм" in text and "Сатурн · Са4 — Монолитный поликарбонат прозрачный 4 мм" in text
                # 4. ✓/✕ only stage
                ok = cell.query_selector(".value-ok-bar button")
                assert ok.is_disabled()

                def chip(label):
                    for c in page.query_selector('[data-value-position="1"]').query_selector_all(".value-chip"):
                        if c.inner_text().replace(" ", "").replace("\n", "").startswith(label):
                            return c
                for label, symbol in (("8мм", "✓"), ("4мм", "✕"), ("6мм", "✕")):
                    chip(label).query_selector(f'button:text-is("{symbol}")').click()
                    page.wait_for_timeout(100)
                assert page.inner_text("#message") == ""  # nothing was saved yet
                assert len(row_cells(1)["Сатурн"].query_selector_all(".supplier-item:not(.blocked)")) == 1
                ok = page.query_selector('[data-value-position="1"] .value-ok-bar button')
                assert "(3)" in ok.inner_text() and not ok.is_disabled()
                # 5. OK saves once and the choice stands for both supplier columns
                ok.click()
                page.wait_for_selector("#message:has-text('сохранено')", timeout=10000)
                states = {c.inner_text().replace(" ", "").replace("\n", "")[:3]: c.get_attribute("class") for c in
                          page.query_selector('[data-value-position="1"]').query_selector_all(".value-chip")}
                assert "rejected" in states["4мм"] and "rejected" in states["6мм"]
                for supplier in ("Сатурн", "Вега"):
                    assert len(row_cells(1)[supplier].query_selector_all(".supplier-item")) == 4
                    assert len(row_cells(1)[supplier].query_selector_all(".supplier-item:not(.blocked)")) == 1
                assert not errors, errors
                browser.close()
        finally:
            server.should_exit = True
            gc.collect()
