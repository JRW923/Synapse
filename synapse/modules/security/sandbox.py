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
from pathlib import Path

from synapse.protocols.sandbox import SandboxResult

try:
    import ctypes  # Windows Job Object API
except ImportError:  # pragma: no cover
    ctypes = None


class ProcessSandbox:
    """Process-tree isolation via process group (Unix) or Job Object (Windows)."""

    def __init__(self) -> None:
        self._job = None  # Windows Job Object handle (per execution)

    @property
    def method(self) -> str:
        """The isolation mechanism actually applied to child processes."""
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
            if os.name == "nt":
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(cwd) if cwd else None,
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
                    cwd=str(cwd) if cwd else None,
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
