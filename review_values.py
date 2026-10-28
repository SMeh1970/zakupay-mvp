"""Inline characteristic controls in the existing review table."""
import json
from pathlib import Path


def assets(result, raw, request, job_id, nonce):
    from supplier_comparison import model, _csrf
    data = model(result, raw)
    data.update(csrf=_csrf(request), embedded=True, read_only=False,
                action_url=f'/dashboard/automation/jobs/{job_id}/comparison/action')
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).replace('<', '\\u003c').replace('&', '\\u0026')
    script = Path(__file__).with_name('comparison_assets').joinpath('values.js').read_text(encoding='utf-8')
    return (f'<script nonce="{nonce}" type="application/json" id="comparison-data">{encoded}</script>'
            f'<script nonce="{nonce}">{script}</script>')


CSS = """
.supplier-quote{display:block!important;width:100%;text-align:left;background:#fff!important;color:#333!important;border:1px solid #ddd!important;padding:10px!important;border-radius:6px!important;font-weight:400!important}
.supplier-quote strong,.supplier-quote small,.supplier-quote-name{display:block}
.supplier-quote-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin:4px 0}
.supplier-quote.chosen{border:2px solid #367348!important}
.supplier-choice-dialog{width:min(700px,92vw);max-height:85vh;border:1px solid #ddd;border-radius:10px;padding:20px;color:#333;background:#fff}
.supplier-choice-dialog::backdrop{background:#0006}
.supplier-choice-header{display:flex;align-items:center;justify-content:space-between;gap:12px}
.supplier-choice-header h2{margin:0;font-size:19px}
.supplier-choice-option{border-top:1px solid #ddd;padding:14px 0}
.supplier-choice-option p{margin:5px 0;font-size:13px}
.supplier-choice-error{color:#a00}
.review-table .supplier-price{min-width:190px;vertical-align:top}
.supplier-variant{padding:6px 0;border-bottom:1px solid #eee;font-size:12px}
.supplier-price .matched{color:#367348}
.inline-values .value-position{margin:0;padding:0;border:0;background:none}
.inline-values .value-panel{display:block;font-size:13px;line-height:1.8}
.inline-values .value-group{display:inline;margin-right:10px}
.inline-values .value-group>b{font-weight:400;color:#777;font-size:11px;margin-right:4px}
.inline-values .value-chip{display:inline-flex;align-items:center;white-space:normal;margin-right:3px;color:#997d43;border:0;border-radius:0;padding:0;background:none;gap:0}
.inline-values .value-chip.matched{color:#367348;background:none;border:0}
.inline-values .value-chip.rejected{color:#9b6d65;text-decoration:line-through;background:none;border:0}
.inline-values .value-chip button{min-width:18px;min-height:22px;padding:1px;background:none;border:0;border-radius:0;box-shadow:none;color:#a4aba3;font:400 12px/1 Arial;cursor:pointer}
.inline-values .value-chip button[aria-pressed=true]{color:#557260;font-weight:600}
.inline-values .value-chip.rejected button[aria-pressed=true]{color:#9b6d65}
.inline-values .value-chip button:hover{color:#253e2c}
.inline-values .value-chip button:focus-visible{outline:1px solid #718679}
.inline-values .value-result{margin:5px 0;padding:0;font-size:12px}
.inline-values .value-result p{margin:3px 0}
.inline-values .value-result>p{color:#777;font-size:11px}
.inline-values .value-offer{border-top:1px solid #eee;padding:5px 0}
.invoice-candidate{margin-top:7px;font-size:12px;color:#666}
.invoice-candidate summary{cursor:pointer}
#message{font-size:12px;color:#557260}
"""
