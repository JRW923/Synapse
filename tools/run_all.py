#!/usr/bin/env python3
"""一键跑全套截图：起 synpase serve → 跑 CLI 三套 + Web 两套流程 → 关服务。

跨平台（Windows / macOS / Linux 均用同一个 Python 脚本）。

用法:
    python tools/run_all.py                # 全套，产出到 shots-all/
    python tools/run_all.py --out demo     # 指定产出目录
    python tools/run_all.py --skip-cli     # 只跑 Web
    python tools/run_all.py --no-serve     # 服务已在 8000 跑着，跳过启动
    python tools/run_all.py --port 9000    # 服务端口不是 8000

前置: pip install playwright pyte Pillow + playwright install chromium + pip install -e .
Web 真实任务流程(my_flow_real.json) 需先在 Web UI 配好 API Key 且确认模式=全部允许。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str]) -> int:
    r = subprocess.run([sys.executable, *cmd], cwd=ROOT)
    if r.returncode != 0:
        print(f"  ⚠ 步骤失败（退出码 {r.returncode}）: {' '.join(cmd)}")
    return r.returncode


def _server_ready(port: int, timeout: float = 30.0) -> bool:
    url = f"http://localhost:{port}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="一键跑全套截图")
    ap.add_argument("--out", default="shots-all", help="产出根目录（默认 ./shots-all）")
    ap.add_argument("--port", type=int, default=8000, help="synapse serve 端口")
    ap.add_argument("--skip-cli", action="store_true", help="跳过 CLI 流程")
    ap.add_argument("--skip-web", action="store_true", help="跳过 Web 流程（也不起服务）")
    ap.add_argument("--no-serve", action="store_true", help="服务已在运行，跳过启动")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    proc = None
    if not args.skip_web:
        if not args.no_serve:
            log = out / "server.log"
            print(f"启动 synapse serve (port {args.port})，日志 → {log}")
            with open(log, "w") as lf:
                proc = subprocess.Popen(
                    [sys.executable, "-c",
                     "from synapse.adapters.cli import main; raise SystemExit(main())",
                     "serve", "--port", str(args.port)],
                    cwd=ROOT, stdout=lf, stderr=subprocess.STDOUT,
                )
            print("等待服务就绪…")
            if not _server_ready(args.port):
                print(f"  ⚠ 服务未在 30s 内就绪，查看 {log}（Web 流程可能失败）")
        elif not _server_ready(args.port):
            print(f"  ⚠ --no-serve 但 {args.port} 未就绪，Web 流程可能失败")

    try:
        if not args.skip_cli:
            print("\n=== CLI 流程 ===")
            _run(["tools/cli_screenshot_flow.py",
                  "--steps", "tools/my_cli_flow.json",
                  "--out", str(out / "cli")])

        if not args.skip_web:
            print("\n=== Web 首启引导流程（无需 Key）===")
            _run(["tools/screenshot_flow.py",
                  "--steps", "tools/my_flow.json",
                  "--out", str(out / "web")])
            print("\n=== Web 真实任务流程（需已配 Key + 确认模式=全部允许）===")
            _run(["tools/screenshot_flow.py",
                  "--steps", "tools/my_flow_real.json",
                  "--out", str(out / "web-real")])
    finally:
        if proc is not None:
            print("\n关闭 synapse serve…")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    print(f"\n完成。产出目录: {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
