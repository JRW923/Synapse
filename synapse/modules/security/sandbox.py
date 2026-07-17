"""Process sandbox — cross-platform execution isolation.

Phase 1 implements a basic subprocess sandbox. Full platform-specific
sandboxing (Windows Job Objects, macOS Seatbelt, Linux bubblewrap)
will be added in Phase 2.
"""

import asyncio
import platform
from pathlib import Path
from synapse.protocols.sandbox import SandboxResult


class ProcessSandbox:
    """Basic process sandbox with timeout and working directory control.

    In Phase 1, this uses subprocess isolation. Full OS-level sandboxing
    (Seatbelt/bubblewrap/Job Objects) is a Phase 2 enhancement.
    """

    @property
    def platform(self) -> str:
        system = platform.system().lower()
        if system == "windows":
            return "windows_job"
        elif system == "darwin":
            return "macos_seatbelt"
        elif system == "linux":
            return "linux_bwrap"
        return "none"

    async def execute(
        self,
        command: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 120,
        network: bool = False,
        allowed_paths: list[Path] | None = None,
    ) -> SandboxResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env=env,
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
                proc.kill()
                await proc.wait()
                return SandboxResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Command timed out after {timeout}s",
                    timed_out=True,
                    platform=self.platform,
                )
        except Exception as e:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=str(e),
                platform=self.platform,
            )
