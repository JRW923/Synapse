"""CLI 逐帧截图 —— 在伪终端里跑命令，每步把终端画面渲染成带横幅的 PNG。

和 Web 脚本（screenshot_flow.py）风格统一：每张图自带步骤编号横幅，单图自解释。
真实命令 + 真实输出 + 真实 ANSI 颜色，全部由 pyte 虚拟终端捕获后用 Pillow 渲染。

跨平台：Linux/macOS 用 pty 起真实 bash；Windows 无 pty 模块，改用管道 + 后台线程读输出，
并手动回显提示符与命令（推荐装 Git for Windows 以提供 bash.exe，否则回退 cmd.exe）。

依赖（一次性安装）:
    pip install pyte Pillow

用法:
    # 用内置示例步骤跑通链路
    python tools/cli_screenshot_flow.py

    # 用你自己的步骤文件
    python tools/cli_screenshot_flow.py --steps my_cli_flow.json --out shots-cli

步骤文件 JSON 示例:
[
  {
    "name": "build",                       // 文件名: 01-build.png
    "commands": ["cd /opt/Synapse", "pytest -q"],
    "note": "运行测试套件",                  // 横幅上的一行说明
    "wait_ms": 3000,                       // 命令后等多久再截（默认 800ms）
    "expect": "passed"                     // 可选: 等到屏幕出现该串就提前截
  }
]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont
from wcwidth import wcwidth


# 深色主题，与 Web UI 一致
TERM_BG = "#0d1117"
DEFAULT_FG = "#e6edf3"
BANNER_BG = "#161b22"
ACCENT = "#39c5cf"


STEPS = [
    {
        "name": "intro",
        "commands": ['echo "Synapse CLI 演示"', "pwd"],
        "note": "跑通伪终端 + 渲染链路（把 STEPS 换成你的流程，或传 --steps）",
    },
    {
        "name": "run-tests",
        "commands": ["echo 执行任意命令都会截一帧"],
        "note": "每步 = 一组命令 + 等待 + 一张图",
        "wait_ms": 400,
    },
]


def find_font(size: int) -> ImageFont.ImageFont:
    cands = [
        # Linux
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        # macOS
        "/Library/Fonts/Menlo.ttf",
        # Windows
        r"C:\Windows\Fonts\consola.ttf",
        r"C:\Windows\Fonts\cour.ttf",
        r"C:\Windows\Fonts\lucon.ttf",
    ]
    cands += glob.glob("/usr/share/fonts/**/*Mono*.ttf", recursive=True)
    cands += glob.glob("/usr/share/fonts/**/*mono*.ttf", recursive=True)
    cands += glob.glob("C:/Windows/Fonts/*Mono*.ttf", recursive=True)
    cands += glob.glob("C:/Windows/Fonts/*onsola*.ttf", recursive=True)
    seen: set[str] = set()
    for c in cands:
        if c in seen or not os.path.exists(c):
            continue
        seen.add(c)
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            pass
    sys.stderr.write("⚠ 没找到等宽字体，回退到 PIL 默认点阵字体（对齐可能不准）\n")
    return ImageFont.load_default()


def find_cjk_font(size: int) -> ImageFont.ImageFont | None:
    cands = [
        # Linux
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        # Windows
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\malgun.ttf",
        r"C:\Windows\Fonts\msgothic.ttc",
    ]
    for c in cands:
        if not os.path.exists(c):
            continue
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            pass
    return None


# pyte 对 16 色用命名（如 'cyan'），对 256/真彩用 'rrggbb' hex
_NAMED = {
    "black": "#000000", "red": "#cd0000", "green": "#00cd00", "yellow": "#cdcd00",
    "brown": "#cdcd00", "blue": "#0000ee", "magenta": "#cd00cd", "cyan": "#00cdcd",
    "white": "#e5e5e5",
    "brightblack": "#7f7f7f", "brightred": "#ff0000", "brightgreen": "#00ff00",
    "brightyellow": "#ffff00", "brightbrown": "#ffff00", "brightblue": "#5c5cff",
    "brightmagenta": "#ff00ff", "brightcyan": "#00ffff", "brightwhite": "#ffffff",
}


def _rgb(code: str) -> str | None:
    if not code or code == "default":
        return None
    if code.startswith("#"):
        return code
    if code in _NAMED:
        return _NAMED[code]
    if len(code) == 6 and all(c in "0123456789abcdefABCDEF" for c in code):
        return "#" + code
    return DEFAULT_FG  # 兜底，避免崩溃


def render_terminal(screen, cols: int, rows: int,
                    font, cjk_font, char_w: int, char_h: int) -> Image.Image:
    img = Image.new("RGB", (cols * char_w, rows * char_h), TERM_BG)
    d = ImageDraw.Draw(img)
    for y in range(rows):
        line = screen.buffer[y]
        x = 0
        while x < cols:
            ch = line[x]
            data = ch.data
            if not data or data == " ":
                x += 1
                continue
            # 单个 cell 可能含多码点（组合字符/代理对），按每个码点累加宽度
            total_w = sum(wcwidth(c) for c in data)
            if total_w <= 0:
                x += 1
                continue
            cell_w = char_w * total_w
            use_font = cjk_font if total_w >= 2 and cjk_font else font
            fg = _rgb(ch.fg) or DEFAULT_FG
            bg = _rgb(ch.bg)
            if ch.reverse:
                fg, bg = (bg or TERM_BG), fg
            px, py = x * char_w, y * char_h
            if bg and bg != TERM_BG:
                d.rectangle([px, py, px + cell_w, py + char_h], fill=bg)
            d.text((px, py), data, fill=fg, font=use_font)
            x += total_w
    return img


def add_banner(term: Image.Image, idx: int, name: str, note: str) -> Image.Image:
    pad = 10
    bh = 30
    out = Image.new("RGB", (term.width, bh + term.height), BANNER_BG)
    out.paste(term, (0, bh))
    d = ImageDraw.Draw(out)
    d.rectangle([0, 0, term.width, bh - 1], fill=BANNER_BG)
    label = f"#{idx}"
    d.text((pad, 8), label, fill=ACCENT, font=_banner_font)
    w = d.textlength(label, font=_banner_font)
    d.text((pad + w + 8, 8), f"{name}  —  {note}", fill=DEFAULT_FG, font=_banner_font)
    return out


def pump(master: int, stream, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r, _, _ = select.select([master], [], [], min(0.1, end - time.monotonic()))
        if r:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            stream.feed(data)


def wait_step(screen, master: int, stream, step: dict) -> None:
    dur = step.get("wait_ms", 800) / 1000.0
    expect = step.get("expect")
    end = time.monotonic() + dur
    while time.monotonic() < end:
        r, _, _ = select.select([master], [], [], 0.1)
        if r:
            data = os.read(master, 65536)
            if data:
                stream.feed(data)
        if expect and expect in "\n".join(screen.display):
            pump(master, stream, 0.3)  # 收尾输出
            return
    pump(master, stream, 0.2)


def _make_env() -> dict:
    py_bin = os.path.dirname(sys.executable)
    return {
        **os.environ,
        "TERM": "xterm-256color",
        "PS1": "\x1b[36m$\x1b[0m ",
        # 非交互管道下程序默认不输出颜色，这两个变量强制上色
        "CLICOLOR_FORCE": "1",
        "FORCE_COLOR": "1",
        "PATH": py_bin + os.pathsep + os.environ.get("PATH", ""),
    }


def _shell_win() -> list[str]:
    for cand in [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        os.path.expanduser(r"~\scoop\apps\git\current\bin\bash.exe"),
    ]:
        if os.path.exists(cand):
            return [cand, "--norc", "--noprofile"]
    return ["cmd.exe"]


def _run_step_posix(step: dict, cols: int, rows: int,
                   font, cjk_font, char_w: int, char_h: int) -> Image.Image:
    import pty  # 仅 POSIX 可用；Windows 走 _run_step_win，不会执行到这里
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["bash", "--norc", "--noprofile"],
        stdin=slave, stdout=slave, stderr=slave,
        env=_make_env(), start_new_session=True,
    )
    os.close(slave)
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    try:
        pump(master, stream, 0.6)  # 等首屏提示符
        commands = step.get("commands", [])
        if isinstance(commands, str):
            commands = [commands]
        for cmd in commands:
            os.write(master, (cmd + "\n").encode())
        wait_step(screen, master, stream, step)
        return render_terminal(screen, cols, rows, font, cjk_font, char_w, char_h)
    finally:
        proc.kill()
        try:
            os.close(master)
        except OSError:
            pass


def _run_step_win(step: dict, cols: int, rows: int,
                  font, cjk_font, char_w: int, char_h: int) -> Image.Image:
    # Windows 没有 pty 模块，改用管道 + 后台线程读输出；提示符与命令由我们手动回显
    shell = _shell_win()
    is_bash = "bash" in os.path.basename(shell[0]).lower()
    proc = subprocess.Popen(
        shell,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=_make_env(), bufsize=0,
    )
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)

    lock = threading.Lock()

    def _reader() -> None:
        while True:
            data = proc.stdout.read(65536)
            if not data:
                break
            with lock:
                stream.feed(data)

    threading.Thread(target=_reader, daemon=True).start()
    try:
        time.sleep(0.6)
        commands = step.get("commands", [])
        if isinstance(commands, str):
            commands = [commands]
        prompt = "\x1b[36m$\x1b[0m " if is_bash else "> "
        cmd_newline = "\n" if is_bash else "\r\n"
        for cmd in commands:
            with lock:
                stream.feed(prompt.encode() + cmd.encode() + b"\r\n")
            proc.stdin.write((cmd + cmd_newline).encode())
            proc.stdin.flush()
        dur = step.get("wait_ms", 800) / 1000.0
        expect = step.get("expect")
        end = time.monotonic() + dur
        while time.monotonic() < end:
            if expect and expect in "\n".join(screen.display):
                time.sleep(0.3)
                break
            time.sleep(0.1)
        time.sleep(0.2)
        return render_terminal(screen, cols, rows, font, cjk_font, char_w, char_h)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


def _run_step(step: dict, cols: int, rows: int,
              font, cjk_font, char_w: int, char_h: int) -> Image.Image:
    if os.name == "nt":
        return _run_step_win(step, cols, rows, font, cjk_font, char_w, char_h)
    return _run_step_posix(step, cols, rows, font, cjk_font, char_w, char_h)


def capture(steps: list[dict], out_dir: Path,
            cols: int, rows: int, font_size: int) -> None:
    global _banner_font
    font = find_font(font_size)
    cjk_font = find_cjk_font(font_size)
    _banner_font = find_cjk_font(max(font_size, 14)) or font
    char_w = font.getbbox("M")[2]
    _, _, _, bh = font.getbbox("M")
    char_h = bh + 3

    out_dir.mkdir(parents=True, exist_ok=True)
    for i, step in enumerate(steps, start=1):
        term = _run_step(step, cols, rows, font, cjk_font, char_w, char_h)
        img = add_banner(term, i, step.get("name", f"step{i}"), step.get("note", ""))
        path = out_dir / f"{i:02d}-{step.get('name', 'step')}.png"
        img.save(path)
        print(f"  ✓ {path.name}")


_banner_font: ImageFont.ImageFont | None = None


def main() -> int:
    ap = argparse.ArgumentParser(description="按步骤截图 CLI 流程")
    ap.add_argument("--steps", help="步骤 JSON 文件路径（不传则用内置示例）")
    ap.add_argument("--out", default="shots-cli", help="输出目录（默认 ./shots-cli）")
    ap.add_argument("--cols", type=int, default=100, help="终端列数（默认 100）")
    ap.add_argument("--rows", type=int, default=30, help="终端行数（默认 30）")
    ap.add_argument("--font-size", type=int, default=14, help="字号（默认 14）")
    args = ap.parse_args()

    steps = json.loads(Path(args.steps).read_text(encoding="utf-8")) if args.steps else STEPS
    if not steps:
        print("没有步骤可跑。", file=sys.stderr)
        return 1

    print(f"开始 CLI 截图 {len(steps)} 步 → {args.out}/")
    capture(steps, Path(args.out), args.cols, args.rows, args.font_size)
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
