"""scheduler 的行为测试（不依赖真实时钟）。"""

from __future__ import annotations

import json
import os
import time

import pytest

from dafeiyu.kernel import scheduler as S


@pytest.fixture()
def sc(tmp_path):
    return S.Scheduler(str(tmp_path))


def test_第一次总是允许(sc):
    assert sc.allow("world_walk") is True


def test_min_gap_生效(sc):
    t0 = 1_000_000.0
    assert sc.allow("world_walk", min_gap=1500, now=t0) is True
    sc.done("world_walk", ok=True, now=t0)
    assert sc.allow("world_walk", min_gap=1500, now=t0 + 100) is False
    assert sc.allow("world_walk", min_gap=1500, now=t0 + 1600) is True


def test_max_per_day_生效(sc):
    t0 = 1_000_000.0
    for i in range(3):
        assert sc.allow("reading", max_per_day=3, now=t0 + i) is True
        sc.done("reading", ok=True, now=t0 + i)
    assert sc.allow("reading", max_per_day=3, now=t0 + 10) is False


def test_跨天重置(sc):
    t0 = 1_000_000.0
    sc.done("reading", ok=True, now=t0)
    # 手工把 day 改成昨天
    st = sc._t("reading")
    st["day"] = "2000-01-01"
    st["day_n"] = 99
    assert sc.allow("reading", max_per_day=3, now=t0 + 60) is True


def test_失败退避指数增长(sc):
    t0 = 1_000_000.0
    sc.done("lymph", ok=False, now=t0)
    st = sc._t("lymph")
    assert st["fails"] == 1
    wait1 = st["backoff_until"] - t0
    assert wait1 == pytest.approx(S.BACKOFF_BASE)

    sc.done("lymph", ok=False, now=t0 + 100)
    wait2 = sc._t("lymph")["backoff_until"] - (t0 + 100)
    assert wait2 == pytest.approx(S.BACKOFF_BASE * 2)

    sc.done("lymph", ok=False, now=t0 + 200)
    wait3 = sc._t("lymph")["backoff_until"] - (t0 + 200)
    assert wait3 == pytest.approx(S.BACKOFF_BASE * 4)


def test_退避封顶(sc):
    t0 = 1_000_000.0
    for _ in range(20):
        sc.done("lymph", ok=False, now=t0)
    wait = sc._t("lymph")["backoff_until"] - t0
    assert wait == pytest.approx(S.BACKOFF_CAP)


def test_退避期内不允许(sc):
    t0 = 1_000_000.0
    sc.done("lymph", ok=False, now=t0)
    assert sc.allow("lymph", now=t0 + 10) is False
    assert sc.allow("lymph", now=t0 + S.BACKOFF_BASE + 1) is True


def test_成功清零退避(sc):
    t0 = 1_000_000.0
    sc.done("lymph", ok=False, now=t0)
    sc.done("lymph", ok=False, now=t0 + 100)
    assert sc._t("lymph")["fails"] == 2
    sc.done("lymph", ok=True, now=t0 + 200)
    st = sc._t("lymph")
    assert st["fails"] == 0
    assert st["backoff_until"] == 0.0


def test_持久化与重载(sc, tmp_path):
    t0 = 1_000_000.0
    sc.done("world_walk", ok=True, now=t0, note="走到书架")
    sc2 = S.Scheduler(str(tmp_path))
    st = sc2.stats()["tasks"]["world_walk"]
    assert st["ok"] is True
    assert st["note"] == "走到书架"


def test_暂停与恢复(sc):
    assert sc.paused() is False
    sc.pause()
    assert sc.paused() is True
    assert sc.allow("world_walk") is False
    sc.resume()
    assert sc.allow("world_walk") is True


def test_stats_字段齐全(sc):
    sc.done("world_walk", ok=False, note="boom")
    st = sc.stats()["tasks"]["world_walk"]
    for k in ("last_at", "ago", "ok", "fails", "day_n", "backoff_left", "note", "err"):
        assert k in st
    assert st["fails"] == 1
    assert st["err"] == "boom"


def test_任务名不冲突(sc):
    """不同任务互不影响预算。"""
    t0 = 1_000_000.0
    for i in range(3):
        sc.done("reading", ok=True, now=t0 + i)
    assert sc.allow("reading", max_per_day=3, now=t0 + 10) is False
    assert sc.allow("world_walk", max_per_day=3, now=t0 + 10) is True


def test_原子写不留tmp(sc, tmp_path):
    sc.done("world_walk", ok=True)
    assert os.path.exists(os.path.join(str(tmp_path), S.STATE_NAME))
    assert not os.path.exists(os.path.join(str(tmp_path), S.STATE_NAME + ".tmp"))


def test_坏文件不崩(tmp_path):
    p = os.path.join(str(tmp_path), S.STATE_NAME)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{不是 json")
    sc = S.Scheduler(str(tmp_path))
    assert sc.allow("world_walk") is True


def test_hook_跳过时不调用(sc):
    called = []
    run = S.hook(None, sc, "world_walk", min_gap=1500)
    sc.done("world_walk", ok=True)
    r = run(lambda: called.append(1))
    assert r is None
    assert called == []


def test_hook_成功记录(sc):
    run = S.hook(None, sc, "world_walk")
    r = run(lambda: "去了书架")
    assert r == "去了书架"
    assert sc.stats()["tasks"]["world_walk"]["note"] == "去了书架"


def test_hook_失败记录并重抛(sc):
    run = S.hook(None, sc, "world_walk")

    def boom():
        raise RuntimeError("mcp 掉了")

    with pytest.raises(RuntimeError):
        run(boom)
    st = sc.stats()["tasks"]["world_walk"]
    assert st["ok"] is False
    assert "mcp 掉了" in st["err"]
