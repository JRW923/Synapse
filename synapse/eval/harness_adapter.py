"""External Harness command protocol v1.

The adapter starts an argv list with ``shell=False`` and writes one UTF-8 JSON
request to stdin.  The request contains ``task_id``, ``task``, ``workspace``,
``seed``, ``model_id``, ``budgets``, ``permissions`` and ``metadata``.  ``metadata`` is built
only from the caller's explicit ``agent_input``; benchmark/grader metadata must
remain in the parent process.  Stdout must contain one JSON object with
``status``, ``output``, ``model_id``, ``metrics``, ``tokens``, ``artifacts``,
``trajectory`` and ``error``; stderr is diagnostics only.

``metrics`` carries duration/tool counters, ``tokens`` carries input/output
counts and provenance, artifacts use workspace-relative paths, and each
trajectory event has a non-empty ``type``.  The validated response is returned
as ``(AgentResult, run_score)`` for direct use by ``BenchmarkRunner``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import signal
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from synapse.protocols.planner import (
    AgentResult,
    Artifact,
    ExecutionMetrics,
    ResultStatus,
)


class HarnessAdapterError(RuntimeError):
    """Base error for command execution and protocol violations."""


class HarnessTimeoutError(HarnessAdapterError):
    """The Harness exceeded its configured wall-clock timeout."""


class HarnessProcessError(HarnessAdapterError):
    """The Harness could not start or exited with a non-zero status."""


class HarnessOutputLimitError(HarnessAdapterError):
    """The Harness exceeded a stdout or stderr byte limit."""


class HarnessProtocolError(HarnessAdapterError):
    """The Harness emitted an invalid protocol response."""


class _WindowsJob:
    """Contain an external process tree in a kill-on-close Job."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._lock = threading.Lock()
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        )
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = (
            wintypes.HANDLE, wintypes.HANDLE,
        )
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not self._kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            self._kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        self._handle = handle

    def assign(self, process: subprocess.Popen) -> None:
        process_handle = self._wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise self._ctypes.WinError(self._ctypes.get_last_error())

    def terminate(self) -> None:
        with self._lock:
            handle = self._handle
            if handle is None:
                return
            self._handle = None
            self._kernel32.TerminateJobObject(handle, 1)
            self._kernel32.CloseHandle(handle)

    def close(self) -> None:
        with self._lock:
            handle = self._handle
            if handle is None:
                return
            self._handle = None
            self._kernel32.CloseHandle(handle)


class CommandHarnessAdapter:
    """Run an external Agent Harness through the versioned JSON protocol."""

    PROTOCOL_VERSION = 1
    _RESPONSE_FIELDS = {
        "protocol_version",
        "status",
        "output",
        "metrics",
        "tokens",
        "artifacts",
        "trajectory",
        "error",
        "model_id",
    }
    _OPTIONAL_RESPONSE_FIELDS = {"run_id"}
    _METRIC_FIELDS = {
        "duration_ms",
        "tool_call_count",
        "tool_success_count",
        "thrashing_events",
    }
    _TOKEN_FIELDS = {"input", "output", "source"}
    _OPTIONAL_TOKEN_FIELDS = {
        "cost_usd",
        "cost_is_estimate",
        "input_cost_per_million_usd",
        "output_cost_per_million_usd",
    }
    _ERROR_FIELDS = {"category", "message", "retryable"}
    _ARTIFACT_FIELDS = {"path", "content", "action"}
    _SAFE_ENV_KEYS = {
        "HOME", "LANG", "LC_ALL", "PATH", "PATHEXT", "PYTHONIOENCODING",
        "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USERPROFILE", "WINDIR",
    }

    def __init__(
        self,
        argv: list[str],
        *,
        expected_model_id: str,
        timeout_seconds: float = 300.0,
        max_stdout_bytes: int = 4 * 1024 * 1024,
        max_stderr_bytes: int = 256 * 1024,
        env: Mapping[str, str] | None = None,
        trusted_host_execution: bool = False,
    ) -> None:
        if not isinstance(argv, list) or not argv:
            raise ValueError("argv must be a non-empty list of strings")
        if any(not isinstance(arg, str) or not arg for arg in argv):
            raise ValueError("argv must be a non-empty list of strings")
        if not isinstance(expected_model_id, str) or not expected_model_id.strip():
            raise ValueError("expected_model_id must be a non-empty string")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        for name, limit in (
            ("max_stdout_bytes", max_stdout_bytes),
            ("max_stderr_bytes", max_stderr_bytes),
        ):
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if env is not None and not isinstance(env, Mapping):
            raise TypeError("env must be a mapping of strings")
        if env is not None and any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in env.items()
        ):
            raise TypeError("env must contain only string keys and values")
        if not isinstance(trusted_host_execution, bool):
            raise TypeError("trusted_host_execution must be a boolean")

        self._argv = tuple(argv)
        self.expected_model_id = expected_model_id.strip()
        self.timeout_seconds = float(timeout_seconds)
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self._env = dict(env or {})
        self.trusted_host_execution = trusted_host_execution

    def to_config(self) -> dict[str, Any]:
        """Return a portable config fingerprint without persisting workspace paths."""
        encoded = json.dumps(
            self._argv, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return {
            "adapter": "external_command",
            "protocol_version": self.PROTOCOL_VERSION,
            "expected_model_id": self.expected_model_id,
            "command": {
                "executable": Path(self._argv[0]).name,
                "argv_sha256": hashlib.sha256(encoded).hexdigest(),
                "arg_count": len(self._argv),
            },
            "timeout_seconds": self.timeout_seconds,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "env_keys": sorted(self._env),
            "trusted_host_execution": self.trusted_host_execution,
        }

    async def run(
        self,
        *,
        task_id: str,
        task: str,
        workspace: str | os.PathLike[str],
        seed: int,
        budgets: Mapping[str, Any],
        permissions: Mapping[str, Any],
        agent_input: Mapping[str, Any],
    ) -> tuple[AgentResult, dict[str, Any]]:
        """Execute one task and return its normalized result and runtime score."""
        request = self._build_request(
            task_id=task_id,
            task=task,
            workspace=workspace,
            seed=seed,
            budgets=budgets,
            permissions=permissions,
            agent_input=agent_input,
        )
        response = await asyncio.to_thread(self._execute, request)
        if response["model_id"] != self.expected_model_id:
            raise HarnessProtocolError(
                "response.model_id does not match configured expected_model_id"
            )
        agent_result, run_score = self._to_result(response)
        run_score["model_id"] = self.expected_model_id
        run_score["comparability"] = {
            "source": "harness_adapter",
            "model_id": self.expected_model_id,
            "budgets": request["budgets"],
            "permissions": request["permissions"],
        }
        return agent_result, run_score

    def _build_request(
        self,
        *,
        task_id: str,
        task: str,
        workspace: str | os.PathLike[str],
        seed: int,
        budgets: Mapping[str, Any],
        permissions: Mapping[str, Any],
        agent_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id.strip():
            raise HarnessProtocolError("task_id must be a non-empty string")
        if not isinstance(task, str) or not task.strip():
            raise HarnessProtocolError("task must be a non-empty string")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise HarnessProtocolError("seed must be an integer")
        for name, value in (
            ("budgets", budgets),
            ("permissions", permissions),
            ("agent_input", agent_input),
        ):
            if not isinstance(value, Mapping):
                raise HarnessProtocolError(f"{name} must be a JSON object")

        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise HarnessProtocolError("workspace must be an existing directory")
        request = {
            "protocol_version": self.PROTOCOL_VERSION,
            "task_id": task_id,
            "task": task,
            "workspace": str(root),
            "seed": seed,
            "model_id": self.expected_model_id,
            "budgets": dict(budgets),
            "permissions": dict(permissions),
            "metadata": dict(agent_input),
        }
        try:
            json.dumps(request, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise HarnessProtocolError(
                f"request must contain only finite JSON values: {exc}"
            ) from exc
        return request

    def _execute(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.trusted_host_execution:
            raise HarnessProcessError(
                "host Harness execution is disabled; pass trusted_host_execution=True "
                "only for trusted commands"
            )
        payload = (
            json.dumps(
                request,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        windows_job = None
        process = None
        try:
            creationflags = 0
            if os.name == "nt":
                windows_job = _WindowsJob()
                # ponytail: normal launch avoids Python _overlapped initialization
                # failures; the short pre-assignment window is the known tradeoff.
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            process = subprocess.Popen(
                list(self._argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                cwd=request["workspace"],
                env=self._execution_env(),
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
            if windows_job is not None:
                windows_job.assign(process)
        except Exception as exc:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            if windows_job is not None:
                windows_job.terminate()
            raise HarnessProcessError(f"failed to start Harness: {exc}") from exc

        streams: dict[str, bytes] = {}
        limit_errors: list[HarnessOutputLimitError] = []
        reader_errors: list[Exception] = []

        def drain(name: str, stream, limit: int) -> None:
            collected = bytearray()
            try:
                while chunk := stream.read(64 * 1024):
                    if len(collected) + len(chunk) > limit:
                        limit_errors.append(HarnessOutputLimitError(
                            f"Harness {name} exceeded {limit} bytes"
                        ))
                        self._kill_process_tree(process, windows_job)
                        break
                    collected.extend(chunk)
            except Exception as exc:  # pragma: no cover - OS pipe failure
                reader_errors.append(exc)
                self._kill_process_tree(process, windows_job)
            finally:
                streams[name] = bytes(collected)
                stream.close()

        assert process.stdout is not None and process.stderr is not None
        readers = [
            threading.Thread(
                target=drain,
                args=("stdout", process.stdout, self.max_stdout_bytes),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=("stderr", process.stderr, self.max_stderr_bytes),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        try:
            try:
                assert process.stdin is not None
                try:
                    process.stdin.write(payload)
                    process.stdin.flush()
                except BrokenPipeError:
                    pass
                finally:
                    process.stdin.close()
                process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                self._kill_process_tree(process, windows_job)
                process.wait()
                for reader in readers:
                    reader.join(timeout=5)
                raise HarnessTimeoutError(
                    f"Harness timed out after {self.timeout_seconds:g}s"
                ) from exc

            if windows_job is not None:
                windows_job.close()
            for reader in readers:
                reader.join(timeout=5)
            if limit_errors:
                raise limit_errors[0]
            if reader_errors:
                raise HarnessProcessError(
                    f"failed to read Harness output: {reader_errors[0]}"
                )
            stdout = streams.get("stdout", b"")
            stderr = streams.get("stderr", b"")

            if process.returncode != 0:
                digest = hashlib.sha256(stderr).hexdigest()
                raise HarnessProcessError(
                    f"Harness exited with code {process.returncode}; "
                    f"stderr_bytes={len(stderr)}; stderr_sha256={digest}"
                )
            return self._parse_response(stdout)
        finally:
            if windows_job is not None:
                windows_job.close()

    def _execution_env(self) -> dict[str, str]:
        environment = {
            key: value for key, value in os.environ.items()
            if key.upper() in self._SAFE_ENV_KEYS
        }
        environment.update(self._env)
        return environment

    @staticmethod
    def _kill_process_tree(
        process: subprocess.Popen,
        windows_job: _WindowsJob | None = None,
    ) -> None:
        if windows_job is not None:
            windows_job.terminate()
            return
        if process.poll() is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass

    @classmethod
    def _parse_response(cls, raw: bytes) -> dict[str, Any]:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HarnessProtocolError("Harness stdout must be valid UTF-8") from exc
        if not text.strip():
            raise HarnessProtocolError("Harness stdout is empty")

        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite number {value}")

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate key {key!r}")
                result[key] = value
            return result

        try:
            response = json.loads(
                text,
                parse_constant=reject_constant,
                object_pairs_hook=reject_duplicate_keys,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise HarnessProtocolError(
                f"Harness stdout must contain one valid JSON object: {exc}"
            ) from exc
        if not isinstance(response, dict):
            raise HarnessProtocolError("Harness response must be a JSON object")

        cls._validate_response(response)
        return response

    @classmethod
    def _validate_response(cls, response: dict[str, Any]) -> None:
        cls._validate_keys(
            response,
            cls._RESPONSE_FIELDS,
            cls._OPTIONAL_RESPONSE_FIELDS,
            "response",
        )
        if response["protocol_version"] != cls.PROTOCOL_VERSION:
            raise HarnessProtocolError(
                f"protocol_version must be {cls.PROTOCOL_VERSION}"
            )
        status = response["status"]
        if status not in {item.value for item in ResultStatus}:
            raise HarnessProtocolError(
                "status must be one of: success, partial, failed"
            )
        if not isinstance(response["output"], str):
            raise HarnessProtocolError("output must be a string")
        if not isinstance(response["model_id"], str) or not response["model_id"].strip():
            raise HarnessProtocolError("model_id must be a non-empty string")

        metrics = cls._require_object(response["metrics"], "metrics")
        cls._validate_keys(metrics, cls._METRIC_FIELDS, set(), "metrics")
        for name in cls._METRIC_FIELDS:
            cls._require_non_negative_int(metrics[name], f"metrics.{name}")
        if metrics["tool_success_count"] > metrics["tool_call_count"]:
            raise HarnessProtocolError(
                "metrics.tool_success_count cannot exceed tool_call_count"
            )

        tokens = cls._require_object(response["tokens"], "tokens")
        cls._validate_keys(
            tokens,
            cls._TOKEN_FIELDS,
            cls._OPTIONAL_TOKEN_FIELDS,
            "tokens",
        )
        cls._require_non_negative_int(tokens["input"], "tokens.input")
        cls._require_non_negative_int(tokens["output"], "tokens.output")
        if not isinstance(tokens["source"], str) or not tokens["source"].strip():
            raise HarnessProtocolError("tokens.source must be a non-empty string")
        if "cost_is_estimate" in tokens and not isinstance(
            tokens["cost_is_estimate"], bool
        ):
            raise HarnessProtocolError("tokens.cost_is_estimate must be a boolean")
        for name in (
            "cost_usd",
            "input_cost_per_million_usd",
            "output_cost_per_million_usd",
        ):
            if name in tokens:
                cls._require_non_negative_number(tokens[name], f"tokens.{name}")

        artifacts = response["artifacts"]
        if not isinstance(artifacts, list):
            raise HarnessProtocolError("artifacts must be an array")
        for index, raw_artifact in enumerate(artifacts):
            field = f"artifacts[{index}]"
            artifact = cls._require_object(raw_artifact, field)
            cls._validate_keys(artifact, cls._ARTIFACT_FIELDS, set(), field)
            path = artifact["path"]
            if not isinstance(path, str) or not path.strip():
                raise HarnessProtocolError(f"{field}.path must be a non-empty string")
            windows_path = PureWindowsPath(path)
            if (
                "\x00" in path
                or PurePosixPath(path).is_absolute()
                or windows_path.is_absolute()
                or bool(windows_path.drive)
                or bool(windows_path.root)
                or ".." in PurePosixPath(path.replace("\\", "/")).parts
            ):
                raise HarnessProtocolError(
                    f"{field}.path must stay relative to the workspace"
                )
            if not isinstance(artifact["content"], str):
                raise HarnessProtocolError(f"{field}.content must be a string")
            if artifact["action"] not in {"created", "modified", "deleted"}:
                raise HarnessProtocolError(
                    f"{field}.action must be created, modified or deleted"
                )

        trajectory = response["trajectory"]
        if not isinstance(trajectory, list):
            raise HarnessProtocolError("trajectory must be an array")
        for index, raw_event in enumerate(trajectory):
            event = cls._require_object(raw_event, f"trajectory[{index}]")
            event_type = event.get("type")
            if not isinstance(event_type, str) or not event_type.strip():
                raise HarnessProtocolError(
                    f"trajectory[{index}].type must be a non-empty string"
                )

        error = response["error"]
        if error is not None:
            error = cls._require_object(error, "error")
            cls._validate_keys(error, cls._ERROR_FIELDS, set(), "error")
            for name in ("category", "message"):
                if not isinstance(error[name], str) or not error[name].strip():
                    raise HarnessProtocolError(
                        f"error.{name} must be a non-empty string"
                    )
            if not isinstance(error["retryable"], bool):
                raise HarnessProtocolError("error.retryable must be a boolean")
        if status == ResultStatus.SUCCESS.value and error is not None:
            raise HarnessProtocolError("successful responses must set error to null")
        if status == ResultStatus.FAILED.value and error is None:
            raise HarnessProtocolError("failed responses must include an error object")

        for name in cls._OPTIONAL_RESPONSE_FIELDS:
            if name in response and (
                not isinstance(response[name], str) or not response[name].strip()
            ):
                raise HarnessProtocolError(f"{name} must be a non-empty string")

        try:
            json.dumps(response, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise HarnessProtocolError(
                f"response must contain only finite JSON values: {exc}"
            ) from exc

    @staticmethod
    def _validate_keys(
        value: dict[str, Any],
        required: set[str],
        optional: set[str],
        field: str,
    ) -> None:
        keys = set(value)
        missing = sorted(required - keys)
        unknown = sorted(keys - required - optional)
        if missing:
            raise HarnessProtocolError(f"{field} is missing fields: {', '.join(missing)}")
        if unknown:
            raise HarnessProtocolError(f"{field} has unknown fields: {', '.join(unknown)}")

    @staticmethod
    def _require_object(value: Any, field: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise HarnessProtocolError(f"{field} must be a JSON object")
        if any(not isinstance(key, str) for key in value):
            raise HarnessProtocolError(f"{field} keys must be strings")
        return value

    @staticmethod
    def _require_non_negative_int(value: Any, field: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HarnessProtocolError(f"{field} must be a non-negative integer")

    @staticmethod
    def _require_non_negative_number(value: Any, field: str) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise HarnessProtocolError(f"{field} must be a non-negative finite number")

    @classmethod
    def _to_result(
        cls, response: dict[str, Any]
    ) -> tuple[AgentResult, dict[str, Any]]:
        metrics = response["metrics"]
        tokens = response["tokens"]
        artifacts = [
            Artifact(
                path=item["path"],
                content=item["content"],
                action=item["action"],
            )
            for item in response["artifacts"]
        ]
        agent_result = AgentResult(
            status=ResultStatus(response["status"]),
            output=response["output"],
            artifacts=artifacts,
            metrics=ExecutionMetrics(
                tokens_input=tokens["input"],
                tokens_output=tokens["output"],
                tool_call_count=metrics["tool_call_count"],
                tool_success_count=metrics["tool_success_count"],
                duration_ms=metrics["duration_ms"],
                thrashing_events=metrics["thrashing_events"],
            ),
        )
        efficiency = {
            "tokens_input": tokens["input"],
            "tokens_output": tokens["output"],
            "token_count_source": tokens["source"],
            "tool_call_count": metrics["tool_call_count"],
            "tool_success_count": metrics["tool_success_count"],
            "duration_ms": metrics["duration_ms"],
            "thrashing_events": metrics["thrashing_events"],
        }
        token_mapping = {
            "cost_usd": "cost_estimate_usd",
            "cost_is_estimate": "cost_is_estimate",
            "input_cost_per_million_usd": "input_cost_per_million_usd",
            "output_cost_per_million_usd": "output_cost_per_million_usd",
        }
        for source, target in token_mapping.items():
            if source in tokens:
                efficiency[target] = tokens[source]

        run_score: dict[str, Any] = {
            "external_harness": {
                "protocol_version": response["protocol_version"],
                "status": response["status"],
                "metrics": metrics,
                "tokens": tokens,
                "artifacts": [
                    {
                        "path": item["path"],
                        "action": item["action"],
                        "content_bytes": len(item["content"].encode("utf-8")),
                        "content_sha256": hashlib.sha256(
                            item["content"].encode("utf-8")
                        ).hexdigest(),
                    }
                    for item in response["artifacts"]
                ],
                "trajectory": [
                    {"type": str(event["type"])}
                    for event in response["trajectory"]
                ],
                "error": (
                    {
                        "category": response["error"]["category"],
                        "retryable": response["error"]["retryable"],
                        "message_sha256": hashlib.sha256(
                            response["error"]["message"].encode("utf-8")
                        ).hexdigest(),
                    }
                    if response["error"] is not None else None
                ),
            },
            "efficiency": efficiency,
        }
        for name in cls._OPTIONAL_RESPONSE_FIELDS:
            if name in response:
                run_score[name] = response[name]
        return agent_result, run_score


__all__ = [
    "CommandHarnessAdapter",
    "HarnessAdapterError",
    "HarnessOutputLimitError",
    "HarnessProcessError",
    "HarnessProtocolError",
    "HarnessTimeoutError",
]
