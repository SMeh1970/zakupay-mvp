"""Import the four archived originals through application HTTP handlers.

Use a development database first. Never sends mail or changes saved orders.
Source files/customer data are external inputs and must not be committed.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--orders-json", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import automation_pipeline as pipeline
    import price_lists as prices
    import prepared_catalog as prepared
    app = FastAPI()
    prices.install_price_lists(app)
    labels = dict(saturn="Сатурн", ipk="ИПК", vent="Крепёжные системы", pipe="Уралпайп")
    sources = json.loads((args.source_dir / "sources.json").read_text())
    report = dict(scope="Все товарные строки четырёх известных структур; не универсальный импорт", imports=[], checks=[], orders=[])
    with TestClient(app) as client:
        for source in sources:
            name = source["key"]
            path = args.source_dir / (name + (".xlsx" if name == "pipe" else ".xls"))
            started = time.perf_counter()
            upload = client.post("/dashboard/prices/upload", data=dict(supplier=labels[name],
                catalog_name="Каталог из письма — " + name, price_date="", source_url=source["email"]["display_url"]),
                files={"file": (source["filename"], path.read_bytes())}, follow_redirects=False)
            assert upload.status_code == 303, upload.text[:1000]
            lid = int(upload.headers["location"].split("/")[-2])
            cfg = prepared.PROFILES[name]
            preview = client.get(upload.headers["location"])
            assert preview.status_code == 200, preview.text[:1000]
            imported = client.post(f"/dashboard/prices/{lid}/import", data=dict(catalog_only="1", profile=name,
                sheet=cfg["sheet"], header_row=cfg["header_row"], source_message_id=source["id"],
                source_received_at=source["email"]["email_ts"]), follow_redirects=False)
            assert imported.status_code == 303, imported.text[:2000]
            record = prices.get_list(lid)
            active_id = lid
            if record["status"] == "duplicate":
                with prices.database() as conn:
                    active_id = prices.execute(conn, "SELECT id FROM supplier_price_lists WHERE catalog_key=? AND status='active'", (record["catalog_key"],)).fetchone()["id"]
            else:
                assert record["status"] == "active", record
            with prices.database() as conn:
                counts = prices.execute(conn, "SELECT COUNT(*) AS n FROM price_product_offers WHERE list_id=?", (active_id,)).fetchone()["n"]
            report["imports"].append(dict(source=name, list_id=active_id, imported=counts, seconds=round(time.perf_counter()-started, 3)))
            print(json.dumps(report["imports"][-1], ensure_ascii=False), flush=True)
        for query in ["Фанера ламинированная толщина 21 мм", "Зенковка диаметр 25 мм угол 90°",
                      "Крепление М10", "Труба диаметр 12 мм стенка 1,5 мм ст.20"]:
            result = prepared.search(query)
            report["checks"].append(result)
            print(json.dumps(dict(query=query, candidates=len(result["candidates"]), seconds=result["seconds"],
                examples=[dict(name=c["name"], match=c["match"]) for c in result["candidates"][:2]]), ensure_ascii=False), flush=True)
        if args.orders_json:
            orders = json.loads(args.orders_json.read_text())
            for order in orders:
                snapshot = json.loads(order["order_json"])
                items = snapshot.get("orderItems") or snapshot.get("items") or []
                started = time.perf_counter()
                results = [dict(position=n, name=i["goodName"], quantity=i.get("count"), unit=i.get("unit"),
                    result=prepared.search(i["goodName"], 5)) for n, i in enumerate(items, 1)]
                result = dict(job_id=int(order["id"]), order_id=int(order["order_id"]), title=order["subject"],
                    items=results, seconds=round(time.perf_counter()-started, 4))
                report["orders"].append(result)
                print(json.dumps(dict(order_id=result["order_id"], positions=len(items),
                    found=sum(bool(i["result"]["candidates"]) for i in results), seconds=result["seconds"]), ensure_ascii=False), flush=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
