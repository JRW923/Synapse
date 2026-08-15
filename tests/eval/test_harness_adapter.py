"""Tests for the external Harness command protocol adapter."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import time
from pathlib import Path

import pytest

from synapse.eval.harness_adapter import (
    CommandHarnessAdapter,
    HarnessOutputLimitError,
    HarnessProcessError,
    HarnessProtocolError,
    HarnessTimeoutError,
)
from synapse.eval.experiments import Experiment
from synapse.eval.runner import Benchmark, BenchmarkRunner, BenchmarkTask, TaskGrade
from synapse.protocols.planner import ResultStatus


def _response() -> dict:
    return {
        "protocol_version": 1,
        "status": "success",
        "output": "done",
        "metrics": {
            "duration_ms": 125,
            "tool_call_count": 3,
            "tool_success_count": 2,
            "thrashing_events": 1,
        },
        "tokens": {
            "input": 40,
            "output": 10,
            "source": "exact",
            "cost_usd": 0.02,
            "cost_is_estimate": False,
        },
        "artifacts": [
            {"path": "src/fix.py", "content": "fixed = True\n", "action": "modified"}
        ],
        "trajectory": [{"type": "tool_call", "tool": "pytest"}],
        "error": None,
        "model_id": "fixture-model",
        "run_id": "fixture-run",
    }


def _json_command(response: object) -> list[str]:
    code = "import json,sys; json.load(sys.stdin); print(sys.argv[1])"
    return [sys.executable, "-c", code, json.dumps(response)]


def _trusted_adapter(argv: list[str], **kwargs) -> CommandHarnessAdapter:
    return CommandHarnessAdapter(
        argv,
        expected_model_id="fixture-model",
        trusted_host_execution=True,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_command_adapter_rejects_untrusted_host_execution(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-run"
    adapter = CommandHarnessAdapter([
        sys.executable,
        "-c",
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('bad')",
    ], expected_model_id="fixture-model")

    with pytest.raises(HarnessProcessError, match="trusted_host_execution=True"):
        await adapter.run(
            task_id="t",
            task="task",
            workspace=tmp_path,
            seed=1,
            budgets={},
            permissions={},
            agent_input={},
        )
    assert not marker.exists()


@pytest.mark.asyncio
async def test_command_adapter_sends_request_and_normalizes_result(tmp_path: Path) -> None:
    harness_dir = tmp_path / "harness with spaces"
    harness_dir.mkdir()
    script = harness_dir / "fixture.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "request = json.load(sys.stdin)\n"
        "pathlib.Path(request['workspace'], 'request.json').write_text(\n"
        "    json.dumps(request), encoding='utf-8')\n"
        f"print({json.dumps(json.dumps(_response()))})\n",
        encoding="utf-8",
    )
    adapter = _trusted_adapter([sys.executable, str(script)])

    result, run_score = await adapter.run(
        task_id="task-1",
        task="fix safely; echo should-not-run",
        workspace=tmp_path,
        seed=17,
        budgets={"max_tokens": 500},
        permissions={"network": False},
        agent_input={"public_hint": "repo"},
    )

    request = json.loads((tmp_path / "request.json").read_text(encoding="utf-8"))
    assert request == {
        "protocol_version": 1,
        "task_id": "task-1",
        "task": "fix safely; echo should-not-run",
        "workspace": str(tmp_path.resolve()),
        "seed": 17,
        "model_id": "fixture-model",
        "budgets": {"max_tokens": 500},
        "permissions": {"network": False},
        "metadata": {"public_hint": "repo"},
    }
    assert result.status == ResultStatus.SUCCESS
    assert result.output == "done"
    assert result.metrics.tokens_input == 40
    assert result.metrics.tool_success_count == 2
    assert result.artifacts[0].path == "src/fix.py"
    assert run_score["efficiency"]["cost_estimate_usd"] == 0.02
    assert run_score["external_harness"]["trajectory"] == [{"type": "tool_call"}]
    artifact = run_score["external_harness"]["artifacts"][0]
    assert artifact["path"] == "src/fix.py"
    assert "content" not in artifact
    assert len(artifact["content_sha256"]) == 64
    assert run_score["model_id"] == "fixture-model"
    assert run_score["comparability"] == {
        "source": "harness_adapter",
        "model_id": "fixture-model",
        "budgets": {"max_tokens": 500},
        "permissions": {"network": False},
    }
    assert "test_patch" not in request["metadata"]
    assert "grader_command" not in request["metadata"]
    assert str(tmp_path.resolve()) not in json.dumps(adapter.to_config())


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
async def test_command_adapter_starts_python_asyncio_on_windows(tmp_path: Path) -> None:
    response = json.dumps(_response())
    adapter = _trusted_adapter([
        sys.executable,
        "-c",
        "import asyncio,json,sys; json.load(sys.stdin); print(sys.argv[1])",
        response,
    ])

    result, _run_score = await adapter.run(
        task_id="asyncio",
        task="start asyncio",
        workspace=tmp_path,
        seed=1,
        budgets={},
        permissions={},
        agent_input={},
    )

    assert result.status == ResultStatus.SUCCESS


@pytest.mark.asyncio
async def test_command_adapter_rejects_mismatched_reported_model_id(
    tmp_path: Path,
) -> None:
    response = _response()
    response["model_id"] = "unexpected-model"
    adapter = _trusted_adapter(_json_command(response))

    with pytest.raises(HarnessProtocolError, match="expected_model_id"):
        await adapter.run(
            task_id="t",
            task="task",
            workspace=tmp_path,
            seed=1,
            budgets={},
            permissions={},
            agent_input={},
        )


@pytest.mark.asyncio
async def test_command_adapter_uses_workspace_cwd_and_minimal_environment(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("SYNAPSE_PRIVATE_API_TOKEN", "must-not-leak")
    response = _response()
    code = (
        "import json,os,pathlib,sys; json.load(sys.stdin); "
        "response=json.loads(sys.argv[1]); "
        "response['output']=json.dumps({"
        "'cwd':str(pathlib.Path.cwd()),"
        "'private':os.getenv('SYNAPSE_PRIVATE_API_TOKEN'),"
        "'public':os.getenv('HARNESS_PUBLIC')}); print(json.dumps(response))"
    )
    adapter = _trusted_adapter(
        [sys.executable, "-c", code, json.dumps(response)],
        env={"HARNESS_PUBLIC": "explicit"},
    )

    result, _run_score = await adapter.run(
        task_id="t",
        task="task",
        workspace=tmp_path,
        seed=1,
        budgets={},
        permissions={},
        agent_input={},
    )
    observed = json.loads(result.output)
    assert Path(observed["cwd"]) == tmp_path.resolve()
    assert observed["private"] is None
    assert observed["public"] == "explicit"


@pytest.mark.asyncio
async def test_command_adapter_run_score_is_persisted_by_runner(tmp_path: Path) -> None:
    adapter = _trusted_adapter(_json_command(_response()))
    benchmark = Benchmark(
        name="external",
        tasks=[BenchmarkTask(
            id="task-1",
            description="fix it",
            metadata={
                "agent_input": {"public_hint": "repo"},
                "test_patch": "private",
                "grader_command": ["pytest", "-q"],
            },
        )],
    )

    async def run_task(item: BenchmarkTask):
        return await adapter.run(
            task_id=item.id,
            task=item.description,
            workspace=tmp_path,
            seed=7,
            budgets={},
            permissions={},
            agent_input=item.metadata.get("agent_input", {}),
        )

    report = tmp_path / "report.json"
    result = await BenchmarkRunner().run(
        benchmark,
        lambda _description: None,
        task_runner=run_task,
        report_path=report,
    )

    persisted = json.loads(report.read_text(encoding="utf-8"))
    assert result.tokens_input == 40
    assert result.reproducibility["actual_model_ids"] == ["fixture-model"]
    assert result.reproducibility["actual_run_ids"] == ["fixture-run"]
    external = persisted["results"][0]["run_score"]["external_harness"]
    assert external["trajectory"] == [{"type": "tool_call"}]
    assert "content" not in external["artifacts"][0]
    assert "private" not in json.dumps(
        persisted["results"][0]["run_score"], ensure_ascii=False
    )


@pytest.mark.asyncio
async def test_command_adapter_participates_in_paired_multitask_experiment(
    tmp_path: Path,
) -> None:
    response_a = _response()
    response_a.update({
        "status": "failed",
        "output": "not fixed",
        "error": {"category": "model_reasoning", "message": "missed", "retryable": False},
    })
    adapters = {
        "A": _trusted_adapter(_json_command(response_a)),
        "B": _trusted_adapter(_json_command(_response())),
    }
    benchmark = Benchmark(
        name="external-paired",
        tasks=[
            BenchmarkTask(id="one", description="fix one"),
            BenchmarkTask(id="two", description="fix two"),
        ],
        grader=lambda _task, result, _score: TaskGrade(
            result.status == ResultStatus.SUCCESS,
            float(result.status == ResultStatus.SUCCESS),
        ),
    )

    async def run_task(config, task, seed):
        return await adapters[config["label"]].run(
            task_id=task.id,
            task=task.description,
            workspace=tmp_path,
            seed=seed,
            budgets={"max_tokens": 1000},
            permissions={"network": False},
            agent_input=task.metadata.get("agent_input", {}),
        )

    result = await Experiment(
        id="external-paired",
        name="external-paired",
        variables={},
        agent_config_a={"label": "A"},
        agent_config_b={"label": "B"},
        benchmark=benchmark,
        task_runner=run_task,
        runs_per_config=1,
        bootstrap_samples=20,
    ).run()

    assert result.all_metrics_a["functional_success"] == [0.0, 0.0]
    assert result.all_metrics_b["functional_success"] == [1.0, 1.0]
    assert result.all_metrics_a["tokens"] == [50.0, 50.0]
    assert result.excluded_pair_count == 0
    assert result.task_outcome_counts == {"improved": 2}
    assert result.failure_matrix == {"A": {"model_reasoning": 2}}
    assert all(
        observation.variants["B"].run_score["model_id"] == "fixture-model"
        for observation in result.task_observations
    )


@pytest.mark.asyncio
async def test_command_adapter_rejects_nonzero_exit(tmp_path: Path) -> None:
    adapter = _trusted_adapter(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdin.read(); print('provider down', file=sys.stderr); sys.exit(7)",
        ]
    )
    with pytest.raises(HarnessProcessError, match="code 7.*stderr_sha256") as exc_info:
        await adapter.run(
            task_id="t",
            task="task",
            workspace=tmp_path,
            seed=1,
            budgets={},
            permissions={},
            agent_input={},
        )
    assert "provider down" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_command_adapter_rejects_invalid_json(tmp_path: Path) -> None:
    adapter = _trusted_adapter(
        [sys.executable, "-c", "import sys; sys.stdin.read(); print('not-json')"]
    )
    with pytest.raises(HarnessProtocolError, match="one valid JSON object"):
        await adapter.run(
            task_id="t",
            task="task",
            workspace=tmp_path,
            seed=1,
            budgets={},
            permissions={},
            agent_input={},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(status="done"), "status"),
        (lambda value: value.update(protocol_version=2), "protocol_version"),
        (lambda value: value.update(output=1), "output"),
        (
            lambda value: value["metrics"].update(tool_success_count=4),
            "tool_success_count",
        ),
        (lambda value: value["tokens"].update(input=-1), "tokens.input"),
        (lambda value: value["artifacts"][0].update(path="../escape"), "workspace"),
        (lambda value: value["artifacts"][0].update(path="C:escape"), "workspace"),
        (lambda value: value.update(trajectory=[{}]), "trajectory"),
        (lambda value: value.update(error="boom"), "error"),
    ],
)
async def test_command_adapter_strictly_validates_response_fields(
    tmp_path: Path, mutate, message: str
) -> None:
    response = copy.deepcopy(_response())
    mutate(response)
    adapter = _trusted_adapter(_json_command(response))
    with pytest.raises(HarnessProtocolError, match=message):
        await adapter.run(
            task_id="t",
            task="task",
            workspace=tmp_path,
            seed=1,
            budgets={},
            permissions={},
            agent_input={},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_command_adapter_rejects_oversized_output(
    tmp_path: Path, stream: str
) -> None:
    response = json.dumps(_response())
    code = (
        "import sys; sys.stdin.read(); "
        + ("print('x' * 256)" if stream == "stdout" else
           f"print('x' * 256, file=sys.stderr); print({response!r})")
    )
    adapter = _trusted_adapter(
        [sys.executable, "-c", code],
        max_stdout_bytes=128 if stream == "stdout" else 4096,
        max_stderr_bytes=128 if stream == "stderr" else 4096,
    )
    with pytest.raises(HarnessOutputLimitError, match=stream):
        await adapter.run(
            task_id="t",
            task="task",
            workspace=tmp_path,
            seed=1,
            budgets={},
            permissions={},
            agent_input={},
        )


@pytest.mark.asyncio
async def test_command_adapter_stops_stream_when_limit_is_reached(tmp_path: Path) -> None:
    marker = tmp_path / "survived-output-limit"
    code = (
        "import json,pathlib,sys,time; request=json.load(sys.stdin); "
        "sys.stdout.write('x' * 1000000); sys.stdout.flush(); time.sleep(0.7); "
        "pathlib.Path(request['workspace'], 'survived-output-limit').write_text('bad')"
    )
    adapter = _trusted_adapter(
        [sys.executable, "-c", code],
        timeout_seconds=2,
        max_stdout_bytes=1024,
    )

    started = time.monotonic()
    with pytest.raises(HarnessOutputLimitError, match="stdout"):
        await adapter.run(
            task_id="t",
            task="task",
            workspace=tmp_path,
            seed=1,
            budgets={},
            permissions={},
            agent_input={},
        )
    assert time.monotonic() - started < 2.0
    await asyncio.sleep(0.8)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_command_adapter_kills_and_reaps_timed_out_process(tmp_path: Path) -> None:
    marker = tmp_path / "survived"
    code = (
        "import json,pathlib,sys,time; request=json.load(sys.stdin); "
        "time.sleep(0.7); pathlib.Path(request['workspace'], 'survived').write_text('bad')"
    )
    adapter = _trusted_adapter(
        [sys.executable, "-c", code], timeout_seconds=0.1
    )

    started = time.monotonic()
    with pytest.raises(HarnessTimeoutError, match="timed out"):
        await adapter.run(
            task_id="t",
            task="task",
            workspace=tmp_path,
            seed=1,
            budgets={},
            permissions={},
            agent_input={},
        )
    assert time.monotonic() - started < 2.0
    await asyncio.sleep(0.8)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_command_adapter_timeout_reaps_child_process(tmp_path: Path) -> None:
    marker = tmp_path / "child-survived"
    child = (
        "import pathlib,time; time.sleep(0.7); "
        f"pathlib.Path({str(marker)!r}).write_text('bad')"
    )
    parent = (
        "import json,subprocess,sys,time; json.load(sys.stdin); "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(5)"
    )
    adapter = _trusted_adapter(
        [sys.executable, "-c", parent], timeout_seconds=0.1,
    )

    with pytest.raises(HarnessTimeoutError, match="timed out"):
        await adapter.run(
            task_id="t",
            task="task",
            workspace=tmp_path,
            seed=1,
            budgets={},
            permissions={},
            agent_input={},
        )
    await asyncio.sleep(0.8)
    assert not marker.exists()
