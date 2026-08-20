"""Step-by-step web screenshotter — captures a flow as annotated PNGs.

每张图自带：步骤编号横幅 + 说明 + 关键元素高亮框，单图自解释。
零新依赖，只用到已装的 playwright 同步 API。

用法:
    # 1) 用内置示例步骤跑一遍看效果
    python tools/screenshot_flow.py

    # 2) 用自己的步骤文件（JSON，格式见下方 STEPS 注释）
    python tools/screenshot_flow.py --steps my_flow.json --out shots

步骤文件 JSON 示例:
[
  {
    "name": "home",            // 文件名用: 01-home.png
    "url": "https://example.com",
    "full_page": true,         // 全页截图（默认 false，只截视口）
    "note": "首页基线",         // 横幅上显示的一行说明
    "highlight": "#login-btn", // 截图前给这个元素画高亮框
    "actions": [               // 按序执行
      {"type": "wait", "selector": "#app"},
      {"type": "click", "selector": "#login-btn"},
      {"type": "fill", "selector": "#user", "value": "admin"},
      {"type": "wait_ms", "ms": 800}   // 等动画/请求
    ]
  }
]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


# 内置示例：改成你自己的流程，或写进 JSON 文件传 --steps
STEPS = [
    {
        "name": "example-home",
        "url": "https://example.com",
        "full_page": True,
        "note": "示例首页基线（把 STEPS 换成你的流程，或传 --steps）",
        "highlight": "h1",
    },
]


def _inject_overlay(page, idx: int, step: dict) -> None:
    """在截图前注入步骤横幅 + 高亮框，让单张图自解释。

    用 IIFE + 注入 JSON 的方式，避免 Playwright 在部分环境下把箭头函数
    误判为表达式导致 SyntaxError。JSON 是 JS 字面量子集，直接拼进源码安全。
    """
    note = step.get("note", step.get("name", ""))
    hl = step.get("highlight")
    args_json = json.dumps(
        {"idx": idx, "name": step.get("name", ""), "note": note, "hl": hl},
        ensure_ascii=False,
    )
    js = (
        "(function(){"
        "var a=" + args_json + ";"
        "var idx=a.idx,name=a.name,note=a.note,hl=a.hl;"
        "var b=document.getElementById('__shot_banner');if(b)b.remove();"
        "var h=document.getElementById('__shot_hl');if(h)h.remove();"
        "var banner=document.createElement('div');banner.id='__shot_banner';"
        "banner.textContent='#'+idx+'  '+name+'  —  '+note;"
        "banner.setAttribute('style','position:fixed;top:0;left:0;right:0;z-index:2147483647;"
        "background:rgba(15,23,42,.92);color:#fff;font:600 14px/1.4 monospace;padding:8px 14px;"
        "box-shadow:0 2px 8px rgba(0,0,0,.4);letter-spacing:.3px;white-space:nowrap;"
        "overflow:hidden;text-overflow:ellipsis;');"
        "document.body.appendChild(banner);"
        "if(hl){var el=document.querySelector(hl);if(el){var r=el.getBoundingClientRect();"
        "var box=document.createElement('div');box.id='__shot_hl';"
        "box.setAttribute('style','position:fixed;z-index:2147483646;pointer-events:none;"
        "left:'+r.left+'px;top:'+r.top+'px;width:'+r.width+'px;height:'+r.height+'px;"
        "border:3px solid #f59e0b;border-radius:4px;box-shadow:0 0 0 9999px rgba(0,0,0,.15);');"
        "document.body.appendChild(box);}}"
        "})();"
    )
    page.evaluate(js)


def _run_actions(page, actions: list[dict]) -> None:
    for act in actions or []:
        t = act.get("type")
        if t == "wait":
            page.wait_for_selector(act["selector"], state="visible")
        elif t == "click":
            page.click(act["selector"])
        elif t == "click_if_exists":
            # 存在才点，不存在则跳过（用于“先尝试关设置”这类非强制操作）
            try:
                page.click(act["selector"], timeout=800)
            except Exception:
                pass
        elif t == "fill":
            page.fill(act["selector"], act.get("value", ""))
        elif t == "wait_ms":
            page.wait_for_timeout(act.get("ms", 500))
        else:
            raise ValueError(f"未知 action 类型: {t}")


def capture(steps: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            for i, step in enumerate(steps, start=1):
                if "url" in step:
                    page.goto(step["url"], wait_until="networkidle")
                _run_actions(page, step.get("actions"))
                _inject_overlay(page, i, step)
                path = out_dir / f"{i:02d}-{step.get('name', 'step')}.png"
                page.screenshot(
                    path=str(path),
                    full_page=step.get("full_page", False),
                )
                print(f"  ✓ {path.name}")
        finally:
            browser.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="按步骤截图 Web 流程")
    ap.add_argument("--steps", help="步骤 JSON 文件路径（不传则用内置示例）")
    ap.add_argument("--out", default="shots", help="输出目录（默认 ./shots）")
    args = ap.parse_args()

    if args.steps:
        steps = json.loads(Path(args.steps).read_text(encoding="utf-8"))
    else:
        steps = STEPS

    if not steps:
        print("没有步骤可跑。", file=sys.stderr)
        return 1

    print(f"开始截图 {len(steps)} 步 → {args.out}/")
    capture(steps, Path(args.out))
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
