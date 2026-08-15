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


def _v2_report():
    report = _report()
    report.update({
        "schema_version": 2,
        "attempt_total": 7,
        "scored_attempt_total": 6,
        "excluded_attempts": 1,
        "attempt_passed": 3,
        "attempt_pass_rate": 0.5,
        "attempt_pass_rate_ci95": [0.2, 0.8],
        "task_total": 3,
        "scored_task_total": 2,
        "task_succeeded": 2,
        "task_success_rate": 1.0,
        "task_success_rate_ci95": [0.34, 1.0],
        "task_success_k": 3,
        "pass_at_k_by_k": {"1": 0.5, "2": 0.75, "3": 1.0},
        "pass_at_k_ci95_by_k": {"1": [0.2, 0.8], "2": [0.5, 1.0], "3": [1.0, 1.0]},
        "pass_power_k_by_k": {"1": 0.5, "2": 0.25, "3": 0.0},
        "pass_power_k_ci95_by_k": {"1": [0.2, 0.8], "2": [0.0, 0.5], "3": [0.0, 0.0]},
        "agent_reported_successes": 4,
        "verified_agent_reported_successes": 4,
        "false_successes": 1,
        "false_success_rate": 0.25,
        "unverified_attempts": 1,
        "grader_error_attempts": 0,
        "median_duration_ms": 1200,
        "p95_duration_ms": 1900,
        "efficiency_provenance": {
            "token_count_sources": ["exact"],
            "token_coverage": 1.0,
            "cost_is_estimate": True,
            "cost_rates_usd_per_million": [{"input": 3.0, "output": 15.0}],
        },
        "reproducibility": {
            "taskset_fingerprint": "taskset-sha256",
            "config_fingerprint": "config-sha256",
            "actual_model_ids": ["fixture-model"],
            "actual_run_ids": ["run-1"],
            "dataset_manifest": {
                "name": "fixture",
                "version": "v1",
                "source": "bundled",
                "license": "MIT",
                "taskset_sha256": "manifest-sha256",
                "grader": "pytest",
                "grader_commands": [["python", "-m", "pytest", "-q"]],
                "grader_timeouts_seconds": [60],
            },
            "git_commit": "commit-sha",
            "git_dirty": True,
            "synapse_version": "0.1.0",
            "python_version": "3.12.0",
            "platform": "test-platform",
        },
    })
    report["results"][0].update({"base_task_id": "base-ok", "attempt": 2})
    return report


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
    assert "通过率 / Pass rate" in html_text
    assert "task_id,任务ID" in csv_text
    assert "cost_estimate_usd,预估成本USD" in csv_text
    assert "task_id,任务ID" in csv_text.splitlines()[0]
    assert "成功" in csv_text
    rows = list(csv.DictReader(csv_text.splitlines()))
    assert rows[0]["task_id"] == "ok"
    assert rows[0]["任务ID"] == "ok"
    assert rows[0]["状态"] == "成功"
    assert rows[0]["base_task_id"] == "ok"
    assert rows[0]["attempt"] == "1"
    assert "ok" in csv_text and "bad" in csv_text


def test_visualize_v2_prioritizes_attempt_task_curves_false_success_and_fingerprints(tmp_path):
    report = _v2_report()
    html_text = render_html(report, tmp_path / "report-v2.html").read_text(encoding="utf-8")
    csv_text = write_csv(report, tmp_path / "report-v2.csv").read_text(encoding="utf-8-sig")

    assert "尝试通过率 / Attempt pass rate" in html_text
    assert "任务成功率@3 / Task success@3" in html_text
    assert "成功误报率 / False-success rate" in html_text
    assert "7 scheduled · 1 excluded" in html_text
    assert "3 scheduled" in html_text
    assert "延迟 / Latency" in html_text
    assert "p95 1,900 ms" in html_text
    assert "预估成本 / Estimated cost" in html_text
    assert "source exact" in html_text
    assert "rates in $3.00/M, out $15.00/M" in html_text
    assert "Pass@k 与 Pass^k 曲线 / Pass@k and Pass^k curves" in html_text
    assert "Pass@3: 100.0% (95% CI 100.0%-100.0%)" in html_text
    assert "Pass^3: 0.0% (95% CI 0.0%-0.0%)" in html_text
    assert "复现指纹 / Reproducibility fingerprints" in html_text
    assert "taskset-sha256" in html_text
    assert "config-sha256" in html_text
    assert "commit-sha" in html_text
    assert "fixture-model" in html_text
    assert "run-1" in html_text
    assert "manifest-sha256" in html_text
    assert "bundled / MIT" in html_text

    header = csv_text.splitlines()[0]
    assert header.endswith("base_task_id,基础任务ID,attempt,尝试序号")
    row = next(csv.DictReader(csv_text.splitlines()))
    assert row["base_task_id"] == "base-ok"
    assert row["基础任务ID"] == "base-ok"
    assert row["attempt"] == "2"
    assert row["尝试序号"] == "2"


def test_visualize_converts_existing_json(tmp_path):
    source = tmp_path / "report.json"
    source.write_text(json.dumps(_report()), encoding="utf-8")
    html_path, csv_path = render_report_file(source)
    assert html_path.exists()
    assert csv_path.exists()
