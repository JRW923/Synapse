"""Zero-dependency HTML/SVG and CSV rendering for evaluation reports."""

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


_STATUS_ZH = {
    "success": "成功",
    "partial": "部分完成",
    "failed": "失败",
    "error": "错误",
}

_CSV_COLUMNS = [
    ("task_id", "任务ID"),
    ("category", "分类"),
    ("status", "状态"),
    ("passed", "通过"),
    ("score", "得分"),
    ("duration_ms", "耗时毫秒"),
    ("tokens_input", "输入Token"),
    ("tokens_output", "输出Token"),
    ("tool_call_count", "工具调用数"),
    ("tool_success_count", "工具成功数"),
    ("cost_estimate_usd", "预估成本USD"),
    ("process_score", "过程得分"),
    ("safety_violations", "安全事件数"),
    ("base_task_id", "基础任务ID"),
    ("attempt", "尝试序号"),
]


def _bilingual(zh: str, en: str) -> str:
    return f"{zh} / {en}"


def _status_label(status: Any) -> str:
    value = str(status or "").strip()
    if not value:
        return "-"
    return _bilingual(_STATUS_ZH.get(value.lower(), value), value.upper())


def _wilson_interval(successes: int, total: int) -> list[float]:
    if total <= 0:
        return [0.0, 0.0]
    z = 1.96
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * ((p * (1 - p) + z * z / (4 * total)) / total) ** 0.5 / denominator
    return [max(0.0, center - margin), min(1.0, center + margin)]


def _interval(value: Any, successes: int, total: int) -> list[float]:
    if isinstance(value, list) and len(value) == 2 and not (value == [0, 0] and successes):
        return value
    return _wilson_interval(successes, total)


def _curve(value: Any) -> dict[int, float]:
    if not isinstance(value, dict):
        return {}
    points = {}
    for raw_k, raw_score in value.items():
        try:
            k = int(raw_k)
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        if k > 0:
            points[k] = max(0.0, min(1.0, score))
    return points


def _curve_tooltip(label: str, k: int, value: float, intervals: Any) -> str:
    detail = f"{label}{k}: {_pct(value)}"
    if isinstance(intervals, dict):
        interval = intervals.get(str(k), intervals.get(k))
        if isinstance(interval, list) and len(interval) == 2:
            detail += f" (95% CI {_pct(interval[0])}-{_pct(interval[1])})"
    return _escape(detail)


def _pass_curve_svg(report: dict[str, Any]) -> str:
    pass_at = _curve(report.get("pass_at_k_by_k"))
    pass_power = _curve(report.get("pass_power_k_by_k"))
    keys = sorted(pass_at.keys() | pass_power.keys())
    if not keys:
        return '<text x="0" y="20" class="muted">暂无曲线数据 / No curve data</text>'

    left, top, plot_width, plot_height = 54, 18, 484, 184
    x_positions = {
        k: left + (plot_width / 2 if len(keys) == 1 else index * plot_width / (len(keys) - 1))
        for index, k in enumerate(keys)
    }

    def y_position(score: float) -> float:
        return top + (1 - score) * plot_height

    svg = []
    for tick in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = y_position(tick)
        svg.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" class="axis"/>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" class="value">{tick:.0%}</text>'
        )
    tick_stride = max(1, (len(keys) + 11) // 12)
    for index, k in enumerate(keys):
        if index % tick_stride == 0 or index == len(keys) - 1:
            svg.append(
                f'<text x="{x_positions[k]:.1f}" y="222" text-anchor="middle" class="value">k={k}</text>'
            )

    series = (
        ("Pass@", pass_at, report.get("pass_at_k_ci95_by_k"), "#55c6d8"),
        ("Pass^", pass_power, report.get("pass_power_k_ci95_by_k"), "#f2cc8f"),
    )
    for label, points, intervals, color in series:
        coordinates = " ".join(
            f"{x_positions[k]:.1f},{y_position(points[k]):.1f}" for k in keys if k in points
        )
        if len(points) > 1:
            svg.append(
                f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="3"/>'
            )
        for k, score in sorted(points.items()):
            svg.append(
                f'<circle cx="{x_positions[k]:.1f}" cy="{y_position(score):.1f}" r="4" fill="{color}">'
                f'<title>{_curve_tooltip(label, k, score, intervals)}</title></circle>'
            )
    svg.append(
        '<line x1="60" y1="250" x2="84" y2="250" stroke="#55c6d8" stroke-width="3"/>'
        '<text x="92" y="254" class="label">Pass@k</text>'
        '<line x1="180" y1="250" x2="204" y2="250" stroke="#f2cc8f" stroke-width="3"/>'
        '<text x="212" y="254" class="label">Pass^k</text>'
    )
    return "".join(svg)


def _reproducibility_rows(value: Any) -> str:
    data = value if isinstance(value, dict) else {}
    manifest = data.get("dataset_manifest", {})
    if not isinstance(manifest, dict):
        manifest = {}
    git_dirty = data.get("git_dirty")
    dirty_label = "-" if git_dirty is None else _bilingual("是" if git_dirty else "否", "YES" if git_dirty else "NO")
    rows = (
        (_bilingual("任务集指纹", "Taskset fingerprint"), data.get("taskset_fingerprint")),
        (_bilingual("配置指纹", "Config fingerprint"), data.get("config_fingerprint")),
        (_bilingual("实际模型", "Actual model(s)"), ", ".join(data.get("actual_model_ids", []) or [])),
        (_bilingual("运行 ID", "Run ID(s)"), ", ".join(data.get("actual_run_ids", []) or [])),
        (_bilingual("数据集", "Dataset"),
         f"{manifest.get('name', '-')} @ {manifest.get('version', 'unknown')}"),
        (_bilingual("来源 / 许可证", "Source / License"),
         f"{manifest.get('source', '-')} / {manifest.get('license', '-') }"),
        (_bilingual("任务清单指纹", "Manifest task hash"), manifest.get("taskset_sha256")),
        (_bilingual("评分器", "Grader"), manifest.get("grader")),
        (_bilingual("评分器指纹", "Grader fingerprint"), manifest.get("grader_sha256")),
        (_bilingual("评分命令", "Grader command(s)"),
         json.dumps(manifest.get("grader_commands", []), ensure_ascii=False)),
        (_bilingual("评分超时", "Grader timeout(s)"),
         ", ".join(str(item) for item in manifest.get("grader_timeouts_seconds", []) or []) or "-"),
        (_bilingual("Git 提交", "Git commit"), data.get("git_commit")),
        (_bilingual("工作区有改动", "Git dirty"), dirty_label),
        (_bilingual("Synapse 版本", "Synapse version"), data.get("synapse_version")),
        (_bilingual("Python / 平台", "Python / Platform"),
         f"{data.get('python_version', '-')} / {data.get('platform', '-')}")
    )
    return "".join(
        f'<tr><th>{_escape(label)}</th><td class="fingerprint">{_escape(content or "-")}</td></tr>'
        for label, content in rows
    )


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
    fields = [label for en, zh in _CSV_COLUMNS for label in (en, zh)]
    with target.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for task in report.get("results", []):
            values = {
                "task_id": task.get("task_id", ""),
                "category": task.get("category", ""),
                "status": task.get("status", ""),
                "passed": task.get("passed", False),
                "score": task.get("score", 0),
                "duration_ms": task.get("duration_ms", 0),
                **_task_metrics(task),
                "base_task_id": task.get("base_task_id", task.get("task_id", "")),
                "attempt": task.get("attempt", 1),
            }
            display_values = {
                **values,
                "status": _STATUS_ZH.get(str(values["status"]).lower(), values["status"]),
                "passed": "是" if values["passed"] else "否",
            }
            row = {}
            for en, zh in _CSV_COLUMNS:
                row[en] = values.get(en, "")
                row[zh] = display_values.get(en, "")
            writer.writerow(row)
    return target


def render_html(report: dict[str, Any], path: str | Path) -> Path:
    """Render a self-contained bilingual dashboard with summary and SVG charts."""
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
    tool_calls = sum(int(row.get("tool_call_count", 0) or 0) for row in task_metric_rows)
    tool_successes = sum(int(row.get("tool_success_count", 0) or 0) for row in task_metric_rows)
    tool_success_rate = float(report.get("tool_success_rate", 0) or 0)
    if not tool_success_rate and tool_calls:
        tool_success_rate = tool_successes / tool_calls
    process_scores = [float(row.get("process_score", 0) or 0) for row in task_metric_rows]
    process_score = sum(process_scores) / len(process_scores) if process_scores else 0.0
    safety_violations = sum(int(row.get("safety_violations", 0) or 0) for row in task_metric_rows)
    efficiency_provenance = report.get("efficiency_provenance", {}) or {}
    cost_label = _bilingual("预估成本", "Estimated cost") if efficiency_provenance.get(
        "cost_is_estimate"
    ) else _bilingual("成本", "Cost")
    token_sources = ", ".join(efficiency_provenance.get("token_count_sources", []) or []) or "-"
    cost_rates = "; ".join(
        f"in ${_number(rate.get('input', 0), 2)}/M, out ${_number(rate.get('output', 0), 2)}/M"
        for rate in efficiency_provenance.get("cost_rates_usd_per_million", [])
        if isinstance(rate, dict)
    ) or "-"
    pass_ci = _interval(
        report.get("pass_rate_ci95"),
        int(report.get("passed", 0) or 0),
        int(report.get("total", 0) or 0),
    )
    try:
        schema_v2 = int(report.get("schema_version", 1)) >= 2
    except (TypeError, ValueError):
        schema_v2 = False
    if schema_v2:
        scheduled_attempts = int(report.get("attempt_total", report.get("total", 0)) or 0)
        attempt_total = int(report.get("scored_attempt_total", scheduled_attempts) or 0)
        excluded_attempts = int(
            report.get("excluded_attempts", scheduled_attempts - attempt_total) or 0
        )
        attempt_passed = int(report.get("attempt_passed", report.get("passed", 0)) or 0)
        attempt_rate = report.get("attempt_pass_rate", report.get("pass_rate", 0))
        attempt_ci = _interval(report.get("attempt_pass_rate_ci95"), attempt_passed, attempt_total)
        scheduled_tasks = int(report.get("task_total", 0) or 0)
        task_total = int(report.get("scored_task_total", scheduled_tasks) or 0)
        task_succeeded = int(report.get("task_succeeded", 0) or 0)
        task_success_rate = report.get("task_success_rate", report.get("pass_at_k", 0))
        task_success_ci = _interval(
            report.get("task_success_rate_ci95"), task_succeeded, task_total,
        )
        task_success_k = int(report.get("task_success_k", 1) or 1)
        agent_successes = int(report.get("agent_reported_successes", 0) or 0)
        verified_successes = int(report.get("verified_agent_reported_successes", agent_successes) or 0)
        false_successes = int(report.get("false_successes", 0) or 0)
        unverified_attempts = int(report.get("unverified_attempts", 0) or 0)
        grader_errors = int(report.get("grader_error_attempts", 0) or 0)
        attempt_rate_text = _pct(attempt_rate) if attempt_total else "n/a"
        attempt_ci_text = (
            f"95% CI {_pct(attempt_ci[0])}-{_pct(attempt_ci[1])}"
            if attempt_total else _bilingual("需要外部 grader", "external grader required")
        )
        task_rate_text = _pct(task_success_rate) if task_total else "n/a"
        task_ci_text = (
            f"95% CI {_pct(task_success_ci[0])}-{_pct(task_success_ci[1])}"
            if task_total else _bilingual("需要完整已验证重复", "complete verified repeats required")
        )
        primary_cards = f"""
<div class="card">{_bilingual('尝试通过率', 'Attempt pass rate')}<strong>{attempt_rate_text}</strong><span class="muted">{attempt_ci_text}</span><small>{attempt_passed}/{attempt_total} analyzed · {scheduled_attempts} scheduled · {excluded_attempts} excluded</small></div>
<div class="card">{_bilingual(f'任务成功率@{task_success_k}', f'Task success@{task_success_k}')}<strong>{task_rate_text}</strong><span class="muted">{task_ci_text}</span><small>{task_succeeded}/{task_total} analyzed · {scheduled_tasks} scheduled</small></div>
<div class="card">{_bilingual('成功误报率', 'False-success rate')}<strong>{_pct(report.get('false_success_rate', 0))}</strong><small>{false_successes}/{verified_successes} {_bilingual('次已验证自报成功', 'verified reported successes')} · {unverified_attempts} unverified · {grader_errors} grader errors</small></div>"""
        curve_panel = (
            f'<section class="panel"><h2>{_bilingual("Pass@k 与 Pass^k 曲线", "Pass@k and Pass^k curves")}</h2>'
            f'<div class="muted curve-note">{_bilingual("Pass@k 衡量多次尝试至少一次成功，Pass^k 衡量多次尝试全部成功", "Pass@k measures eventual success; Pass^k measures consistent success")}</div>'
            f'<svg viewBox="0 0 560 270" role="img" aria-label="Pass@k and Pass^k curves">{_pass_curve_svg(report)}</svg></section>'
        )
        reproducibility_panel = (
            f'<section class="panel"><h2>{_bilingual("复现指纹", "Reproducibility fingerprints")}</h2>'
            f'<div style="overflow:auto"><table class="repro"><tbody>{_reproducibility_rows(report.get("reproducibility"))}</tbody></table></div></section>'
        )
        passed_card_label = _bilingual("通过尝试", "Passed attempts")
        passed_card_value = f"{attempt_passed}/{attempt_total}"
    else:
        primary_cards = f"""
<div class="card">{_bilingual('通过率', 'Pass rate')}<strong>{_pct(report.get('pass_rate', 0))}</strong><span class="muted">95% CI {_pct(pass_ci[0])}-{_pct(pass_ci[1])}</span></div>
<div class="card">Pass@k<strong>{_pct(report.get('pass_at_k', report.get('pass_rate', 0)))}</strong><span class="muted">{_bilingual('独立重复任务', 'Independent repeats')}</span></div>"""
        curve_panel = ""
        reproducibility_panel = ""
        passed_card_label = _bilingual("通过任务", "Passed")
        passed_card_value = f"{_escape(report.get('passed', 0))}/{_escape(report.get('total', 0))}"
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
        f'<td class="{"ok" if t.get("passed") else "bad"}">{_bilingual("通过" if t.get("passed") else "失败", "PASS" if t.get("passed") else "FAIL")}</td>'
        f'<td>{_number(t.get("score", 0), 3)}</td><td>{_number(t.get("duration_ms", 0), 0)} ms</td>'
        f'<td>{_status_label(t.get("status"))}</td>'
        f'<td>{_escape(t.get("grade_reason", ""))}</td></tr>'
        for t in tasks
    )
    metadata = report.get("metadata", {}) or {}
    html_text = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(report.get('name', 'Synapse Eval'))} · 评测报告 / Evaluation Report</title>
<style>
:root {{ color-scheme: dark; --bg:#141515; --panel:#202223; --line:#3b3f40; --text:#f1f3f2; --muted:#a7afac; --cyan:#58b9bd; --green:#45b885; --red:#dc7468; }}
* {{ box-sizing:border-box; }} body {{ margin:0; padding:32px; background:var(--bg); color:var(--text); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1180px; margin:auto; }} header {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-end; border-bottom:1px solid var(--line); padding-bottom:20px; }}
h1 {{ margin:0; font-size:28px; letter-spacing:0; }} h2 {{ margin:0 0 14px; font-size:16px; letter-spacing:0; }} .muted {{ color:var(--muted); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin:20px 0; }} .card,.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; }} .card {{ padding:16px; }} .card strong {{ display:block; margin-top:4px; font-size:24px; color:var(--cyan); }} .card small {{ display:block; color:var(--muted); margin-top:4px; }}
.charts {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }} .panel {{ padding:18px; margin-bottom:16px; }} svg {{ width:100%; height:auto; min-height:120px; overflow:visible; }} .label,.value {{ fill:var(--text); font-size:12px; }} .value {{ fill:var(--muted); }} .track {{ fill:#25333d; }} .bar {{ fill:var(--cyan); }} .axis {{ stroke:var(--line); stroke-width:1; }} .curve-note {{ margin:-6px 0 8px; }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); }} th {{ color:var(--muted); font-weight:500; }} .ok {{ color:var(--green); font-weight:700; }} .bad {{ color:var(--red); font-weight:700; }}
.repro th {{ width:210px; }} .fingerprint {{ font-family:ui-monospace,SFMono-Regular,Consolas,monospace; word-break:break-all; }}
@media (max-width:800px) {{ body {{ padding:18px; }} .grid,.charts {{ grid-template-columns:1fr 1fr; }} header {{ display:block; }} table {{ font-size:12px; }} }}
@media (max-width:520px) {{ .grid,.charts {{ grid-template-columns:1fr; }} }}
</style></head><body><main>
<header><div><h1>{_escape(report.get('name', 'Synapse Eval'))}</h1><div class="muted">{_bilingual('评测报告', 'Evaluation Report')} · {_escape(report.get('started_at', ''))}</div></div>
<div class="muted">{_bilingual('提供商', 'Provider')}: {_escape(metadata.get('provider', '-'))}<br>{_bilingual('模型', 'Model')}: {_escape(metadata.get('model', '-'))}</div></header>
<section class="grid">
{primary_cards}
<div class="card">{_bilingual('平均得分', 'Mean score')}<strong>{_number(report.get('mean_score', 0), 3)}</strong></div>
<div class="card">{passed_card_label}<strong>{passed_card_value}</strong></div>
<div class="card">{_bilingual('延迟', 'Latency')}<strong>{_number(report.get('median_duration_ms', report.get('duration_ms', 0)), 0)} ms</strong><small>median · p95 {_number(report.get('p95_duration_ms', report.get('duration_ms', 0)), 0)} ms</small></div>
<div class="card">{_bilingual('Token 与成本', 'Tokens / Cost')}<strong>{_number(input_tokens + output_tokens, 0)}</strong><small>in {_number(input_tokens, 0)} · out {_number(output_tokens, 0)} · {cost_label} ${_number(total_cost, 4)} · per successful task {_number(report.get('tokens_per_succeeded_task'), 2)} tokens / ${_number(report.get('cost_per_succeeded_task_usd'), 6)} · coverage {_pct(efficiency_provenance.get('token_coverage', 0))} · source {token_sources} · rates {cost_rates}</small></div>
<div class="card">{_bilingual('工具成功率', 'Tool success')}<strong>{_pct(tool_success_rate)}</strong><small>{tool_successes}/{tool_calls} calls</small></div>
<div class="card">{_bilingual('过程得分', 'Process score')}<strong>{_number(process_score, 3)}</strong><small>{_bilingual('安全事件', 'Safety events')}: {safety_violations}</small></div>
</section>
{curve_panel}
<section class="charts"><div class="panel"><h2>{_bilingual('分类通过率', 'Category pass rate')}</h2><svg viewBox="0 0 500 {max(80, len(category_rows)*34+24)}">{''.join(category_rows) or '<text x="0" y="20" class="muted">暂无分类数据 / No category data</text>'}</svg></div>
<div class="panel"><h2>{_bilingual('任务耗时', 'Task duration')}</h2><svg viewBox="0 0 560 {max(80, len(task_rows)*30+24)}">{''.join(task_rows) or '<text x="0" y="20" class="muted">暂无任务数据 / No task data</text>'}</svg></div></section>
{reproducibility_panel}
<section class="panel"><h2>{_bilingual('任务结果', 'Task results')}</h2><div style="overflow:auto"><table><thead><tr><th>{_bilingual('任务', 'Task')}</th><th>{_bilingual('分类', 'Category')}</th><th>{_bilingual('通过', 'Pass')}</th><th>{_bilingual('得分', 'Score')}</th><th>{_bilingual('耗时', 'Duration')}</th><th>{_bilingual('状态', 'Status')}</th><th>{_bilingual('评分器', 'Grader')}</th></tr></thead><tbody>{task_table or '<tr><td colspan="7" class="muted">暂无任务结果 / No task results</td></tr>'}</tbody></table></div></section>
<footer class="muted">{_bilingual('由 Synapse 本地生成', 'Generated locally by Synapse')} · { _bilingual('官方 benchmark runner 与数据版本仍由外部提供', 'Official benchmark runner/data version remains external when metadata says so')}.</footer>
</main></body></html>"""
    target.write_text(html_text, encoding="utf-8")
    return target


def render_experiment_html(report: dict[str, Any], path: str | Path) -> Path:
    """Render a self-contained paired A/B experiment dashboard."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    outcome_labels = {
        "A": _bilingual("A 胜出", "A wins"),
        "B": _bilingual("B 胜出", "B wins"),
        "tradeoff": _bilingual("存在权衡", "Trade-off"),
        "inconclusive": _bilingual("证据不足", "Inconclusive"),
    }
    outcome = str(report.get("outcome", "inconclusive"))
    comparable = bool(report.get("comparability_eligible"))
    issues = report.get("comparability_issues", []) or []
    issue_text = ", ".join(str(item) for item in issues) or _bilingual("无", "None")

    metric_rows = []
    for name, metric in sorted((report.get("metric_results") or {}).items()):
        interval = metric.get("bootstrap_ci")
        ci_text = (
            f"[{_number(interval[0], 4)}, {_number(interval[1], 4)}]"
            if isinstance(interval, (list, tuple)) and len(interval) == 2 else "-"
        )
        winner = metric.get("winner") or "-"
        metric_rows.append(
            f"<tr><td>{_escape(name)}</td><td>{_escape(metric.get('role', '-'))}</td>"
            f"<td>{_escape('higher' if metric.get('higher_is_better') else 'lower')}</td>"
            f"<td>{_number(metric.get('mean_a'), 4)}</td><td>{_number(metric.get('mean_b'), 4)}</td>"
            f"<td>{_number(metric.get('mean_delta'), 4)}</td><td>{_pct(metric.get('relative_improvement'))}</td>"
            f"<td>{ci_text}</td><td>{_number(metric.get('p_value'), 4)}</td>"
            f"<td>{_number(metric.get('adjusted_p_value'), 4)}</td>"
            f"<td>{_escape(metric.get('test', '-'))}</td><td>{_escape(winner)}</td></tr>"
        )

    task_rows = "".join(
        f"<tr><td>{_escape(item.get('task_id', ''))}</td>"
        f"<td>{_escape(item.get('category', ''))}</td>"
        f"<td>{_escape(item.get('basis', ''))}</td>"
        f"<td>{_escape(item.get('outcome', ''))}</td>"
        f"<td>{_escape(item.get('valid_attempt_pairs', 0))}/{_escape(item.get('attempt_pairs', 0))}</td></tr>"
        for item in report.get("task_comparisons", []) or []
    )
    category_rows = "".join(
        f"<tr><td>{_escape(category)}</td>"
        f"<td>{_escape(counts.get('improved', 0))}</td>"
        f"<td>{_escape(counts.get('regressed', 0))}</td>"
        f"<td>{_escape(counts.get('both_passed', 0))}</td>"
        f"<td>{_escape(counts.get('both_failed', 0))}</td>"
        f"<td>{_escape(counts.get('excluded', 0))}</td></tr>"
        for category, counts in sorted(
            (report.get("task_outcomes_by_category") or {}).items()
        )
    )
    failure_rows = "".join(
        f"<tr><td>{_escape(label)}</td><td>{_escape(category)}</td><td>{_escape(count)}</td></tr>"
        for label, categories in sorted((report.get("failure_matrix") or {}).items())
        for category, count in sorted(categories.items())
    )

    fingerprints = report.get("effective_config_fingerprints", {}) or {}
    diff_paths = report.get("config_diff_paths", []) or []
    allowed_paths = report.get("allowed_config_diff_paths") or []
    reproducibility = report.get("reproducibility", {}) or {}
    models = reproducibility.get("actual_model_ids", {}) or {}
    html_text = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(report.get('experiment_name', 'Synapse Experiment'))} · A/B Experiment</title>
<style>
:root {{ color-scheme:light; --bg:#f4f5f3; --panel:#fff; --line:#d7dbd8; --text:#18201d; --muted:#65706b; --teal:#087f73; --green:#237a49; --red:#b33a32; --amber:#9a6410; }}
* {{ box-sizing:border-box; }} body {{ margin:0; padding:28px; background:var(--bg); color:var(--text); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1240px; margin:auto; }} header {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-end; border-bottom:1px solid var(--line); padding-bottom:18px; }}
h1 {{ margin:0; font-size:28px; letter-spacing:0; }} h2 {{ margin:0 0 12px; font-size:16px; letter-spacing:0; }} .muted {{ color:var(--muted); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin:18px 0; }} .card,.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; }} .card {{ padding:15px; }} .card strong {{ display:block; margin-top:4px; font-size:22px; color:var(--teal); }} .card small {{ display:block; margin-top:4px; color:var(--muted); }} .panel {{ padding:18px; margin-bottom:16px; }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; padding:9px 8px; border-bottom:1px solid var(--line); vertical-align:top; }} th {{ color:var(--muted); font-weight:600; white-space:nowrap; }} .ok {{ color:var(--green); }} .bad {{ color:var(--red); }} .warn {{ color:var(--amber); }} .mono {{ font-family:ui-monospace,SFMono-Regular,Consolas,monospace; word-break:break-all; }}
.two {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }} @media (max-width:820px) {{ body {{ padding:16px; }} header {{ display:block; }} .two {{ grid-template-columns:1fr; }} table {{ font-size:12px; }} }}
</style></head><body><main>
<header><div><h1>{_escape(report.get('experiment_name', 'Synapse Experiment'))}</h1><div class="muted">{_bilingual('配对 A/B 实验报告', 'Paired A/B experiment report')} · {_escape(report.get('generated_at', ''))}</div></div><div class="muted">ID: {_escape(report.get('experiment_id', '-'))}<br>seed: {_escape(report.get('seed', 0))}</div></header>
<section class="grid">
<div class="card">{_bilingual('结论', 'Outcome')}<strong>{outcome_labels.get(outcome, _escape(outcome))}</strong><small>{_bilingual('仅 confirmatory 指标可产生 winner', 'Only confirmatory metrics can produce a winner')}</small></div>
<div class="card">{_bilingual('可比性', 'Comparability')}<strong class="{'ok' if comparable else 'bad'}">{_bilingual('满足', 'Eligible') if comparable else _bilingual('不满足', 'Ineligible')}</strong><small>{_escape(issue_text)}</small></div>
<div class="card">{_bilingual('主指标', 'Primary metric')}<strong>{_escape(report.get('primary_metric', '-'))}</strong><small>{_escape(report.get('direction', '-'))} is better</small></div>
<div class="card">{_bilingual('任务 / 配对', 'Tasks / pairs')}<strong>{_escape(report.get('task_count', 0))} / {_escape(report.get('attempt_pairs', 0))}</strong><small>{_escape(report.get('excluded_pair_count', 0))} excluded</small></div>
</section>
<section class="panel"><h2>{_bilingual('指标比较', 'Metric comparisons')}</h2><div style="overflow:auto"><table><thead><tr><th>Metric</th><th>Role</th><th>Direction</th><th>A mean</th><th>B mean</th><th>B-A</th><th>Improvement</th><th>95% CI</th><th>p</th><th>Holm p</th><th>Test</th><th>Winner</th></tr></thead><tbody>{''.join(metric_rows) or '<tr><td colspan="12" class="muted">No metric data</td></tr>'}</tbody></table></div></section>
<section class="two"><div class="panel"><h2>{_bilingual('任务切片', 'Task slices')}</h2><div style="overflow:auto"><table><thead><tr><th>Category</th><th>Improved</th><th>Regressed</th><th>Both pass</th><th>Both fail</th><th>Excluded</th></tr></thead><tbody>{category_rows or '<tr><td colspan="6" class="muted">No task slices</td></tr>'}</tbody></table></div></div>
<div class="panel"><h2>{_bilingual('失败矩阵', 'Failure matrix')}</h2><table><thead><tr><th>Variant</th><th>Category</th><th>Count</th></tr></thead><tbody>{failure_rows or '<tr><td colspan="3" class="muted">No classified failures</td></tr>'}</tbody></table></div></section>
<section class="panel"><h2>{_bilingual('逐任务对照', 'Per-task comparison')}</h2><div style="overflow:auto"><table><thead><tr><th>Task</th><th>Category</th><th>Basis</th><th>Outcome</th><th>Valid / total pairs</th></tr></thead><tbody>{task_rows or '<tr><td colspan="5" class="muted">No task-level observations</td></tr>'}</tbody></table></div></section>
<section class="panel"><h2>{_bilingual('复现与配置证据', 'Reproducibility and config evidence')}</h2><table><tbody>
<tr><th>Effective config A</th><td class="mono">{_escape(fingerprints.get('A', '-'))}</td></tr><tr><th>Effective config B</th><td class="mono">{_escape(fingerprints.get('B', '-'))}</td></tr>
<tr><th>Observed diff paths</th><td class="mono">{_escape(', '.join(diff_paths) or '-')}</td></tr><tr><th>Allowed diff paths</th><td class="mono">{_escape(', '.join(allowed_paths) or '-')}</td></tr>
<tr><th>Actual model A</th><td>{_escape(', '.join(models.get('A', []) or []) or '-')}</td></tr><tr><th>Actual model B</th><td>{_escape(', '.join(models.get('B', []) or []) or '-')}</td></tr>
<tr><th>Dataset</th><td class="mono">{_escape(json.dumps(reproducibility.get('dataset_manifest', {}), ensure_ascii=False, sort_keys=True))}</td></tr>
</tbody></table></section>
<footer class="muted">{_bilingual('工程回归只验证评测链路；能力结论需要冻结数据集、可信外部 grader 与完整可比性证据', 'Engineering regression validates the evaluation pipeline only; capability claims require a frozen dataset, trusted external grader, and complete comparability evidence')}.</footer>
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
