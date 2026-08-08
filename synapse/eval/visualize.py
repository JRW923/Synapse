"""Zero-dependency HTML/SVG and CSV rendering for benchmark reports."""

from __future__ import annotations

import argparse
import csv
import html
import json
from pathlib import Path
from typing import Any


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _pct(value: Any) -> str:
    try:
        return f"{float(value):.1%}"
    except (TypeError, ValueError):
        return "-"


def _number(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _wilson_interval(successes: int, total: int) -> list[float]:
    if total <= 0:
        return [0.0, 0.0]
    z = 1.96
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * ((p * (1 - p) + z * z / (4 * total)) / total) ** 0.5 / denominator
    return [max(0.0, center - margin), min(1.0, center + margin)]


def _find_runtime(value: Any) -> dict[str, Any]:
    """Find the first runtime metric snapshot in nested run-score data."""
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get("efficiency"), dict):
        return value
    for key in ("runtime", "run_score"):
        nested = value.get(key)
        if isinstance(nested, dict):
            found = _find_runtime(nested)
            if found:
                return found
    for nested in value.values():
        if isinstance(nested, dict):
            found = _find_runtime(nested)
            if found:
                return found
    return {}


def _task_metrics(task: dict[str, Any]) -> dict[str, Any]:
    runtime = _find_runtime(task.get("run_score", {}))
    efficiency = runtime.get("efficiency", {}) if isinstance(runtime, dict) else {}
    return {
        "tokens_input": efficiency.get("tokens_input", 0),
        "tokens_output": efficiency.get("tokens_output", 0),
        "tool_call_count": efficiency.get("tool_call_count", 0),
        "tool_success_count": efficiency.get("tool_success_count", 0),
        "cost_estimate_usd": efficiency.get("cost_estimate_usd", 0),
        "process_score": (runtime.get("process", {}) or {}).get("process_score", 0),
        "safety_violations": sum(
            int(v or 0)
            for v in (runtime.get("safety", {}) or {}).values()
            if isinstance(v, (int, float))
        ),
    }


def write_csv(report: dict[str, Any], path: str | Path) -> Path:
    """Write one flattened row per task for spreadsheets and plotting."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "task_id", "category", "status", "passed", "score", "duration_ms",
        "tokens_input", "tokens_output", "tool_call_count", "tool_success_count",
        "cost_estimate_usd", "process_score", "safety_violations",
    ]
    with target.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for task in report.get("results", []):
            writer.writerow({
                "task_id": task.get("task_id", ""),
                "category": task.get("category", ""),
                "status": task.get("status", ""),
                "passed": task.get("passed", False),
                "score": task.get("score", 0),
                "duration_ms": task.get("duration_ms", 0),
                **_task_metrics(task),
            })
    return target


def render_html(report: dict[str, Any], path: str | Path) -> Path:
    """Render a self-contained dashboard with summary and SVG charts."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    categories = report.get("by_category", {}) or {}
    tasks = report.get("results", []) or []
    task_metric_rows = [_task_metrics(task) for task in tasks]
    input_tokens = int(report.get("tokens_input", 0) or 0) or sum(
        int(row.get("tokens_input", 0) or 0) for row in task_metric_rows
    )
    output_tokens = int(report.get("tokens_output", 0) or 0) or sum(
        int(row.get("tokens_output", 0) or 0) for row in task_metric_rows
    )
    total_cost = float(report.get("total_cost_usd", 0) or 0) or sum(
        float(row.get("cost_estimate_usd", 0) or 0) for row in task_metric_rows
    )
    pass_ci = report.get("pass_rate_ci95")
    if not isinstance(pass_ci, list) or len(pass_ci) != 2 or (
        pass_ci == [0, 0] and report.get("passed", 0)
    ):
        pass_ci = _wilson_interval(int(report.get("passed", 0) or 0), int(report.get("total", 0) or 0))
    max_duration = max((float(t.get("duration_ms", 0) or 0) for t in tasks), default=1.0)
    category_rows = []
    for name, data in categories.items():
        rate = max(0.0, min(1.0, float(data.get("pass_rate", 0) or 0)))
        width = int(280 * rate)
        category_rows.append(
            f'<g><text x="0" y="{len(category_rows) * 34 + 20}" class="label">{_escape(name)}</text>'
            f'<rect x="120" y="{len(category_rows) * 34 + 7}" width="280" height="18" rx="4" class="track"/>'
            f'<rect x="120" y="{len(category_rows) * 34 + 7}" width="{width}" height="18" rx="4" class="bar"/>'
            f'<text x="414" y="{len(category_rows) * 34 + 21}" class="value">{_pct(rate)}</text></g>'
        )
    task_rows = []
    for index, task in enumerate(tasks):
        duration = float(task.get("duration_ms", 0) or 0)
        width = int(280 * duration / max_duration) if max_duration else 0
        color = "#2fbf9f" if task.get("passed") else "#e07a5f"
        task_rows.append(
            f'<g><text x="0" y="{index * 30 + 20}" class="label">{_escape(task.get("task_id", ""))}</text>'
            f'<rect x="170" y="{index * 30 + 7}" width="280" height="16" rx="4" class="track"/>'
            f'<rect x="170" y="{index * 30 + 7}" width="{width}" height="16" rx="4" fill="{color}"/>'
            f'<text x="462" y="{index * 30 + 20}" class="value">{_number(duration, 0)} ms</text></g>'
        )
    task_table = "".join(
        f'<tr><td>{_escape(t.get("task_id", ""))}</td><td>{_escape(t.get("category", ""))}</td>'
        f'<td class="{"ok" if t.get("passed") else "bad"}">{"PASS" if t.get("passed") else "FAIL"}</td>'
        f'<td>{_number(t.get("score", 0), 3)}</td><td>{_number(t.get("duration_ms", 0), 0)} ms</td>'
        f'<td>{_escape(t.get("grade_reason", ""))}</td></tr>'
        for t in tasks
    )
    metadata = report.get("metadata", {}) or {}
    html_text = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(report.get('name', 'Synapse Eval'))} Dashboard</title>
<style>
:root {{ color-scheme: dark; --bg:#10151b; --panel:#18212a; --line:#2c3b46; --text:#edf4f5; --muted:#9aadb5; --cyan:#55c6d8; --green:#2fbf9f; --red:#e07a5f; }}
* {{ box-sizing:border-box; }} body {{ margin:0; padding:32px; background:var(--bg); color:var(--text); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1180px; margin:auto; }} header {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-end; border-bottom:1px solid var(--line); padding-bottom:20px; }}
h1 {{ margin:0; font-size:28px; letter-spacing:.02em; }} h2 {{ margin:0 0 14px; font-size:16px; }} .muted {{ color:var(--muted); }}
.grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:20px 0; }} .card,.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; }} .card {{ padding:16px; }} .card strong {{ display:block; margin-top:4px; font-size:24px; color:var(--cyan); }}
.charts {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }} .panel {{ padding:18px; margin-bottom:16px; }} svg {{ width:100%; height:auto; min-height:120px; overflow:visible; }} .label,.value {{ fill:var(--text); font-size:12px; }} .value {{ fill:var(--muted); }} .track {{ fill:#25333d; }} .bar {{ fill:var(--cyan); }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); }} th {{ color:var(--muted); font-weight:500; }} .ok {{ color:var(--green); font-weight:700; }} .bad {{ color:var(--red); font-weight:700; }}
@media (max-width:800px) {{ body {{ padding:18px; }} .grid,.charts {{ grid-template-columns:1fr 1fr; }} header {{ display:block; }} table {{ font-size:12px; }} }}
@media (max-width:520px) {{ .grid,.charts {{ grid-template-columns:1fr; }} }}
</style></head><body><main>
<header><div><h1>{_escape(report.get('name', 'Synapse Eval'))}</h1><div class="muted">Synapse Benchmark Dashboard · {_escape(report.get('started_at', ''))}</div></div>
<div class="muted">{_escape(metadata.get('provider', ''))}/{_escape(metadata.get('model', ''))}</div></header>
<section class="grid">
<div class="card">Pass rate<strong>{_pct(report.get('pass_rate', 0))}</strong><span class="muted">95% CI {_pct(pass_ci[0])}–{_pct(pass_ci[1])}</span></div>
<div class="card">Pass@k<strong>{_pct(report.get('pass_at_k', report.get('pass_rate', 0)))}</strong><span class="muted">独立重复任务</span></div>
<div class="card">Mean score<strong>{_number(report.get('mean_score', 0), 3)}</strong></div>
<div class="card">Passed<strong>{_escape(report.get('passed', 0))}/{_escape(report.get('total', 0))}</strong></div>
<div class="card">Duration<strong>{_number(float(report.get('duration_ms', 0) or 0) / 1000, 1)}s</strong></div>
<div class="card">Tokens / cost<strong>{_number(input_tokens + output_tokens, 0)}</strong><span class="muted">${_number(total_cost, 4)}</span></div>
</section>
<section class="charts"><div class="panel"><h2>Category pass rate</h2><svg viewBox="0 0 500 {max(80, len(category_rows)*34+24)}">{''.join(category_rows) or '<text x="0" y="20" class="muted">No category data</text>'}</svg></div>
<div class="panel"><h2>Task duration</h2><svg viewBox="0 0 560 {max(80, len(task_rows)*30+24)}">{''.join(task_rows) or '<text x="0" y="20" class="muted">No task data</text>'}</svg></div></section>
<section class="panel"><h2>Task results</h2><div style="overflow:auto"><table><thead><tr><th>Task</th><th>Category</th><th>Status</th><th>Score</th><th>Duration</th><th>Grader</th></tr></thead><tbody>{task_table or '<tr><td colspan="6" class="muted">No task results</td></tr>'}</tbody></table></div></section>
<footer class="muted">Generated locally by Synapse · official benchmark runner/data version remains external when metadata says so.</footer>
</main></body></html>"""
    target.write_text(html_text, encoding="utf-8")
    return target


def render_report_file(report_path: str | Path, html_path: str | Path | None = None,
                       csv_path: str | Path | None = None) -> tuple[Path, Path]:
    """Convert an existing JSON report into HTML and CSV artifacts."""
    source = Path(report_path).expanduser().resolve()
    report = json.loads(source.read_text(encoding="utf-8"))
    html_target = Path(html_path).expanduser() if html_path else source.with_suffix(".html")
    csv_target = Path(csv_path).expanduser() if csv_path else source.with_suffix(".csv")
    return render_html(report, html_target), write_csv(report, csv_target)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a Synapse benchmark JSON report")
    parser.add_argument("report", help="Path to benchmark JSON report")
    parser.add_argument("--html", default=None, help="HTML output path")
    parser.add_argument("--csv", default=None, help="CSV output path")
    args = parser.parse_args()
    html_path, csv_path = render_report_file(args.report, args.html, args.csv)
    print(f"HTML: {html_path}")
    print(f"CSV:  {csv_path}")


if __name__ == "__main__":
    main()
