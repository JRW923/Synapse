"""Tests for offline benchmark visualization artifacts."""

import csv
import json

from synapse.eval.visualize import render_html, render_report_file, write_csv


def _report():
    return {
        "name": "smoke",
        "total": 2,
        "passed": 1,
        "pass_rate": 0.5,
        "mean_score": 0.5,
        "duration_ms": 2500,
        "started_at": "2026-08-08T00:00:00Z",
        "metadata": {"provider": "test", "model": "fixture"},
        "by_category": {"functional": {"pass_rate": 0.5}},
        "results": [
            {
                "task_id": "ok",
                "category": "functional",
                "status": "success",
                "passed": True,
                "score": 1.0,
                "duration_ms": 1000,
                "grade_reason": "passed",
                "run_score": {"efficiency": {"tokens_input": 10, "tool_call_count": 2}},
            },
            {
                "task_id": "bad",
                "category": "functional",
                "status": "failed",
                "passed": False,
                "score": 0.0,
                "duration_ms": 1500,
                "grade_reason": "failed",
                "run_score": {"runtime": {"efficiency": {"tokens_output": 5}}},
            },
        ],
    }


def test_visualize_writes_self_contained_html_and_csv(tmp_path):
    report = _report()
    html_path = render_html(report, tmp_path / "report.html")
    csv_path = write_csv(report, tmp_path / "report.csv")
    html_text = html_path.read_text(encoding="utf-8")
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    assert "<svg" in html_text
    assert "分类通过率 / Category pass rate" in html_text
    assert "任务结果 / Task results" in html_text
    assert "工具成功率 / Tool success" in html_text
    assert "评测报告 / Evaluation Report" in html_text
    assert "task_id,任务ID" in csv_text
    assert "cost_estimate_usd,预估成本USD" in csv_text
    assert "task_id,任务ID" in csv_text.splitlines()[0]
    assert "成功" in csv_text
    rows = list(csv.DictReader(csv_text.splitlines()))
    assert rows[0]["task_id"] == "ok"
    assert rows[0]["任务ID"] == "ok"
    assert rows[0]["状态"] == "成功"
    assert "ok" in csv_text and "bad" in csv_text


def test_visualize_converts_existing_json(tmp_path):
    source = tmp_path / "report.json"
    source.write_text(json.dumps(_report()), encoding="utf-8")
    html_path, csv_path = render_report_file(source)
    assert html_path.exists()
    assert csv_path.exists()
