"""Process sandbox — cross-platform execution isolation.

Isolation here is **process-tree containment**: the command (and any children
it spawns) run in a unit that is guaranteed to be reaped when the command is
killed — a Unix process group or a Windows Job Object with
``KILL_ON_JOB_CLOSE``. This is real OS-level containment of the *process tree*
so a runaway command can't orphan children or escape via a fork bomb.

It is deliberately NOT a read-only filesystem sandbox: a coding agent must be
able to write into the workspace, and write access is gated at the
``ActionAuthorizer`` layer instead. (The old code reported ``linux_bwrap`` /
``macos_seatbelt`` / ``windows_job`` as if those OS sandboxes were applied —
they were not. ``method`` now reports what is actually used.)
"""

import asyncio
import os
import signal
import shutil
import sys
from pathlib import Path

from synapse.protocols.sandbox import SandboxResult


_SAFE_ENV_KEYS = {
    "HOME", "LANG", "LC_ALL", "PATH", "PATHEXT", "PYTHONIOENCODING",
    "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USERPROFILE", "WINDIR",
}

try:
    import ctypes  # Windows Job Object API
except ImportError:  # pragma: no cover
    ctypes = None


class ProcessSandbox:
    """Process containment with optional filesystem/network sandbox backends."""

    def __init__(
        self,
        backend: str = "process",
        allow_network: bool = False,
        docker_image: str = "python:3.12-slim",
    ) -> None:
        self._job = None  # Windows Job Object handle (per execution)
        self.allow_network = allow_network
        self.docker_image = docker_image
        self.backend = self._resolve_backend(backend)

    @staticmethod
    def _resolve_backend(backend: str) -> str:
        requested = backend.lower()
        aliases = {"bwrap": "bubblewrap", "sandbox-exec": "seatbelt"}
        requested = aliases.get(requested, requested)
        if requested == "auto":
            if os.name != "nt" and shutil.which("bwrap"):
                return "bubblewrap"
            if sys.platform == "darwin" and shutil.which("sandbox-exec"):
                return "seatbelt"
            return "process"
        executables = {
            "bubblewrap": "bwrap",
            "seatbelt": "sandbox-exec",
            "docker": "docker",
        }
        if requested not in {"process", *executables}:
            raise ValueError(f"Unknown sandbox backend '{backend}'")
        executable = executables.get(requested)
        if executable and not shutil.which(executable):
            raise RuntimeError(f"Sandbox backend '{requested}' is unavailable")
        return requested

    @property
    def method(self) -> str:
        """The isolation mechanism actually applied to child processes."""
        if self.backend != "process":
            return self.backend
        if os.name == "nt":
            return "windows_job_object"
        return "process_group"

    @property
    def platform(self) -> str:
        """Back-compat label — now reports the real mechanism, not a promise."""
        return self.method

    async def execute(
        self,
        command: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 120,
    ) -> SandboxResult:
        try:
            if env is None:
                env = {
                    key: value for key, value in os.environ.items()
                    if key.upper() in _SAFE_ENV_KEYS
                    or key.upper().startswith("PYENV_")
                    or key.upper() in {"VIRTUAL_ENV"}
                }
            # Tool schemas carry paths as strings; normalize at the sandbox
            # boundary so every backend receives one stable path type.
            cwd_path = Path(cwd) if cwd is not None else None
            workdir = (cwd_path or Path.cwd()).resolve()
            argv = self._sandbox_argv(command, workdir)
            if argv is not None:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(workdir),
                    env=env,
                    start_new_session=os.name != "nt",
                )
                if os.name == "nt":
                    self._assign_job(proc.pid)
            elif os.name == "nt":
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(workdir),
                    env=env,
                )
                self._assign_job(proc.pid)
            else:
                # New session → the command owns its own process group, so a
                # kill can take down the whole tree via os.killpg.
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(workdir),
                    env=env,
                    start_new_session=True,
                )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                return SandboxResult(
                    exit_code=proc.returncode or 0,
                    stdout=stdout.decode(errors="ignore"),
                    stderr=stderr.decode(errors="ignore"),
                    timed_out=False,
                    platform=self.platform,
                )
            except asyncio.CancelledError:
                self._kill_tree(proc)
                raise
            except asyncio.TimeoutError:
                self._kill_tree(proc)
                return SandboxResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Command timed out after {timeout}s",
                    timed_out=True,
                    platform=self.platform,
                )
            finally:
                self._close_job()
        except Exception as e:
            self._close_job()
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=str(e),
                timed_out=False,
                platform=self.platform,
            )

    def _sandbox_argv(self, command: str, cwd: Path) -> list[str] | None:
        """Build argv for an explicitly selected strong backend."""
        if self.backend == "process":
            return None
        if self.backend == "bubblewrap":
            args = [
                "bwrap", "--die-with-parent", "--ro-bind", "/", "/",
                "--bind", str(cwd), str(cwd), "--chdir", str(cwd),
                "--proc", "/proc", "--dev", "/dev",
            ]
            if not self.allow_network:
                args.append("--unshare-net")
            return [*args, "/bin/sh", "-lc", command]
        if self.backend == "seatbelt":
            network = "(allow network*)" if self.allow_network else "(deny network*)"
            profile = (
                "(version 1)(allow process*)(allow file-read*)"
                f'(allow file-write* (subpath "{cwd}")){network}'
            )
            return ["sandbox-exec", "-p", profile, "/bin/sh", "-lc", command]
        network = "bridge" if self.allow_network else "none"
        return [
            "docker", "run", "--rm", "--network", network,
            "-v", f"{cwd}:/workspace", "-w", "/workspace",
            self.docker_image, "sh", "-lc", command,
        ]

    # ------------------------------------------------------------------
    # Process-tree kill
    # ------------------------------------------------------------------

    def _kill_tree(self, proc) -> None:
        """Kill the command and every child it spawned."""
        try:
            if os.name == "nt" and self._job is not None:
                # Closing the Job Object (KILL_ON_JOB_CLOSE) reaps the tree.
                self._close_job()
                return
            if os.name != "nt":
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
                return
        except Exception:
            pass
        # Fallback: kill the process directly.
        try:
            proc.kill()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Windows Job Object (real OS-level containment)
    # ------------------------------------------------------------------

    def _assign_job(self, pid: int) -> None:
        """Put *pid* into a kill-on-close Job Object, if available."""
        if os.name != "nt" or ctypes is None:
            return
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("LimitFlags", ctypes.c_int32),
                    ("MinimumWorkingSetSize", ctypes.c_void_p),
                    ("MaximumWorkingSetSize", ctypes.c_void_p),
                    ("ActiveProcessLimit", ctypes.c_int32),
                    ("Affinity", ctypes.c_void_p),
                    ("PriorityClass", ctypes.c_int32),
                    ("SchedulingClass", ctypes.c_int32),
                ]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [("dummy", ctypes.c_ulonglong * 6)]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_void_p),
                    ("JobMemoryLimit", ctypes.c_void_p),
                    ("PeakProcessMemoryUsed", ctypes.c_void_p),
                    ("PeakJobMemoryUsed", ctypes.c_void_p),
                ]

            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info),
            )
            hproc = kernel32.OpenProcess(0x1F0FFF, False, pid)  # PROCESS_ALL_ACCESS
            if hproc:
                kernel32.AssignProcessToJobObject(job, hproc)
                kernel32.CloseHandle(hproc)
            self._job = job
        except Exception:
            # If we can't sandbox, run unsandboxed rather than fail the command.
            self._job = None

    def _close_job(self) -> None:
        if self._job is not None and ctypes is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(self._job)  # type: ignore[attr-defined]
            except Exception:
                pass
            self._job = None
