"""Cron 定时调度 (s14).

进程内调度器：按 5 字段 cron 表达式（分 时 日 月 周）在到期时触发回调。
优先使用标准库 asyncio，不引入第三方依赖；如环境已装 APScheduler 可作升级
路径（见模块底部 ponytail 说明）。

ponytail: 进程内调度，多实例重复运行会各自触发——生产多实例应改用外部触发器
（系统 cron / 消息队列）。next_trigger 用「逐分钟扫描」到一年内首个匹配，逻辑
简单且边界正确，时间开销可忽略（一天仅 1440 次匹配）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

_WEEKDAY_MAP = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 0}  # datetime.weekday() -> cron weekday(0=Sun)


def _parse_field(field: str, lo: int, hi: int) -> list[int]:
    """Expand one cron field into the set of matching integer values."""
    out: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            out.update(range(lo, hi + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            out.update(range(lo, hi + 1, step))
        elif "-" in part and "/" in part:
            rng, step = part.split("/")
            a, b = rng.split("-")
            out.update(range(int(a), int(b) + 1, int(step)))
        elif "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return sorted(out)


def _match(expr: str, dt: datetime) -> bool:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron 表达式需 5 字段，得到 {len(fields)}: {expr!r}")
    minutes = _parse_field(fields[0], 0, 59)
    hours = _parse_field(fields[1], 0, 23)
    days = _parse_field(fields[2], 1, 31)
    months = _parse_field(fields[3], 1, 12)
    weekdays = _parse_field(fields[4], 0, 7)
    wd = _WEEKDAY_MAP[dt.weekday()] if dt.weekday() in _WEEKDAY_MAP else dt.weekday()
    # 日与周互相为「或」：cron 标准——两者都非 * 时为 OR 语义；
    # 其一为 * 时只受另一个约束。
    day_ok = dt.day in days
    wd_ok = wd in weekdays
    day_star = fields[2].strip() == "*"
    wd_star = fields[4].strip() == "*"
    if day_star and wd_star:
        day_cond = True
    elif day_star:
        day_cond = wd_ok
    elif wd_star:
        day_cond = day_ok
    else:
        day_cond = day_ok or wd_ok
    return (
        dt.minute in minutes
        and dt.hour in hours
        and dt.month in months
        and day_cond
    )


def next_trigger(after: datetime, expr: str) -> datetime:
    """Return the first datetime >= after+1min that matches *expr* (within 1yr)."""
    cand = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(366 * 24 * 60):
        if _match(expr, cand):
            return cand
        cand += timedelta(minutes=1)
    raise ValueError(f"cron 表达式一年内无匹配: {expr!r}")


class CronScheduler:
    def __init__(self, clock=None):
        # clock: 无参可调用对象，返回当前 datetime；测试可注入假时钟。
        self._clock = clock or datetime.now
        self._jobs: list[dict] = []
        self._task = None
        self._stop = False

    def add_job(self, cron_expr: str, callback) -> int:
        jid = len(self._jobs) + 1
        self._jobs.append({
            "id": jid,
            "expr": cron_expr,
            "cb": callback,
            "next": next_trigger(self._clock(), cron_expr),
        })
        return jid

    def start(self) -> None:
        self._stop = False
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        self._stop = True

    async def _pump(self) -> None:
        """One iteration: fire every due job and recompute its next trigger."""
        now = self._clock()
        for j in self._jobs:
            if now >= j["next"]:
                r = j["cb"]()
                if asyncio.iscoroutine(r):
                    await r
                j["next"] = next_trigger(j["next"], j["expr"])

    async def _run(self) -> None:
        while not self._stop:
            await self._pump()
            now = self._clock()
            if self._jobs:
                wait = min((j["next"] - now).total_seconds() for j in self._jobs)
            else:
                wait = 60
            await asyncio.sleep(max(0, min(wait, 60)))


# ponytail: 若未来需要持久化/跨进程去重，可在此委托给 APScheduler：
#   try:
#       from apscheduler.schedulers.asyncio import AsyncIOScheduler
#   except ImportError:
#       AsyncIOScheduler = None  # 退回上面的进程内实现
