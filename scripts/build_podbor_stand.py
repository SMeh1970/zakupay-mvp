"""Build the claude.ai stand of «Подбор товаров» from the app's own page.

The app page (comparison_assets/podbor.html) is the only source. The stand is the same file with
example positions instead of a real request and APP.local=true: decisions stay in the browser,
nothing is sent to the app. Usage:

    python scripts/build_podbor_stand.py out.html
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "comparison_assets" / "podbor.html"
EXAMPLES = Path(__file__).with_name("podbor_stand_examples.json")


def build() -> str:
    text = PAGE.read_text(encoding="utf-8")
    data = {
        "local": True,
        "title": "Стенд подбора товаров",
        "positions": json.loads(EXAMPLES.read_text(encoding="utf-8")),
        "chosen": {}, "chosenKey": {}, "state": None, "links": [],
    }
    serialized = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    if text.count("<script>") != 1:
        raise RuntimeError("podbor.html: ожидался один <script>")
    return text.replace("<script>", f'<script type="application/json" id="podbor-data">{serialized}</script><script>', 1)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "podbor-stand.html"
    out.write_text(build(), encoding="utf-8")
    print(out)
