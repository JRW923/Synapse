"""Tests for s14 Cron 调度.

验收：注册每分钟任务，推进（假）时间，断言按 cron 触发。
"""

from __future__ import annotations

from datetime import datetime

from synapse.modules.cron import CronScheduler, next_trigger, _match


def test_next_trigger_minutely():
    base = datetime(2026, 1, 1, 0, 0, 30)
    nxt = next_trigger(base, "* * * * *")
    assert nxt == datetime(2026, 1, 1, 0, 1, 0)


def test_next_trigger_specific_hour():
    base = datetime(2026, 1, 1, 0, 0, 0)
    # 每天 03:15
    nxt = next_trigger(base, "15 3 * * *")
    assert nxt == datetime(2026, 1, 1, 3, 15, 0)


def test_match_weekday():
    # 周一下午 5 点
    mon = datetime(2026, 7, 27, 17, 0)  # 2026-07-27 是周一
    assert _match("0 17 * * 1", mon)
    tue = datetime(2026, 7, 28, 17, 0)
    assert not _match("0 17 * * 1", tue)


async def test_scheduler_fires_on_advance():
    class FakeClock:
        def __init__(self):
            self.t = datetime(2026, 1, 1, 0, 0, 0)
        def __call__(self):
            return self.t

    clock = FakeClock()
    fired = []

    async def cb():
        fired.append(clock.t)

    sched = CronScheduler(clock=clock)
    sched.add_job("* * * * *", cb)

    clock.t = datetime(2026, 1, 1, 0, 0, 30)
    await sched._pump()
    assert fired == []

    clock.t = datetime(2026, 1, 1, 0, 1, 0)
    await sched._pump()
    assert len(fired) == 1

    clock.t = datetime(2026, 1, 1, 0, 2, 0)
    await sched._pump()
    assert len(fired) == 2

    # 跳过多分钟也能补触发（每次 pump 仅触发一次，由 next 推进）
    clock.t = datetime(2026, 1, 1, 0, 5, 0)
    await sched._pump()
    assert len(fired) == 3  # 只触发一次（pump 单步），next 已推进到 00:03
