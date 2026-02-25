from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import PriceSnapshot
from app.utils import dump_json


def generate_json_report(path: str | Path, payload: dict[str, Any]) -> None:
    dump_json(path, payload)


def generate_markdown_report(
    path: str | Path,
    run_summary: dict[str, Any],
    top_drops: list[dict[str, Any]],
    errors_by_provider: dict[str, int],
) -> None:
    lines = [
        "# Price Drop Intelligence Report",
        "",
        f"- Run ID: `{run_summary['run_id']}`",
        f"- Timestamp UTC: `{datetime.now(tz=timezone.utc).isoformat()}`",
        f"- Products: **{run_summary['total_products']}**",
        f"- Snapshots OK: **{run_summary['snapshots_ok']}**",
        f"- Snapshots Error: **{run_summary['snapshots_error']}**",
        f"- Alerts: **{run_summary['alerts_sent']}**",
        "",
        "## Top quedas",
        "",
        "| Produto | Atual | Referência | Queda % | Queda abs | Link |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for d in top_drops:
        lines.append(
            f"| {d['product_id']} | {d['current_price']} {d['currency']} | {d['reference_price']} | {d['drop_percent']:.2f}% | {d['drop_amount']:.2f} | {d['url']} |"
        )

    lines.extend(["", "## Erros por provider", ""])
    if not errors_by_provider:
        lines.append("- Nenhum erro")
    else:
        for provider, count in errors_by_provider.items():
            lines.append(f"- `{provider}`: {count}")

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")


def generate_site(latest_json_path: str | Path, out_dir: str | Path = "site") -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    latest_json = Path(latest_json_path).read_text(encoding="utf-8")
    (out / "latest.json").write_text(latest_json, encoding="utf-8")
    html = """<!doctype html>
<html><head><meta charset='utf-8'><title>Price Drop Intelligence</title>
<style>body{font-family:Arial;padding:20px}.card{border:1px solid #ddd;padding:12px;border-radius:8px;margin:8px 0}</style></head>
<body><h1>Price Drop Intelligence</h1><div id='root'></div>
<script>
fetch('latest.json').then(r=>r.json()).then(data=>{
 const root=document.getElementById('root');
 const summary=document.createElement('p');
 summary.textContent=`Run ${data.run.run_id}: ${data.run.alerts_sent} alertas`;
 root.appendChild(summary);
 (data.alerts||[]).forEach(a=>{
   const d=document.createElement('div');d.className='card';
   d.innerHTML=`<b>${a.product_id}</b><br>Atual: ${a.current_price} ${a.currency}<br>Queda: ${a.drop_percent.toFixed(2)}%`;
   root.appendChild(d);
 });
});
</script></body></html>"""
    (out / "index.html").write_text(html, encoding="utf-8")
