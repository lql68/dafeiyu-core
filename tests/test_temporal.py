"""temporal.py 的测试。

覆盖用户点名的九项：
  1. 时区转换
  2. 跨午夜
  3. 昼夜七档分类
  4. 时间间隔计算
  5. 系统时间回拨
  6. fmt_gap 各个边界
  7. 离线时间计算
  8. 无效时间输入
  9. 时区配置缺失或无效

跑法::

    pytest tests/test_temporal.py -q
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from dafeiyu.kernel import rhythm, temporal


# ---------------------------------------------------------------- 工具

@pytest.fixture()
def clean_tz(monkeypatch):
    """每个用例前后都清掉 DFY_TZ，避免互相污染。"""
    monkeypatch.delenv(temporal.TZ_ENV, raising=False)
    yield
    monkeypatch.delenv(temporal.TZ_ENV, raising=False)


def at_tz(hour: int, minute: int = 0, day: int = 15, month: int = 6, year: int = 2026):
    """构造一个「配置时区下的某个时刻」。"""
    return datetime(year, month, day, hour, minute, tzinfo=temporal.tz())


# ---------------------------------------------------------------- 9. 时区配置

class TestTimezoneConfig:
    def test_default_tz_name(self, clean_tz):
        assert temporal.tz_name() == "Asia/Shanghai"

    def test_env_overrides(self, clean_tz, monkeypatch):
        monkeypatch.setenv(temporal.TZ_ENV, "Asia/Tokyo")
        assert temporal.tz_name() == "Asia/Tokyo"

    def test_env_blank_falls_back_to_default(self, clean_tz, monkeypatch):
        monkeypatch.setenv(temporal.TZ_ENV, "   ")
        assert temporal.tz_name() == "Asia/Shanghai"

    def test_tz_resolves(self, clean_tz):
        z = temporal.tz()
        assert z is not None
        # 无论是 zoneinfo 还是固定偏移，都要能算出正确的小时
        assert datetime(2026, 6, 15, 12, 0, tzinfo=z).hour == 12

    def test_tz_source_is_known(self, clean_tz):
        assert temporal.tz_source() in ("zoneinfo", "fixed-offset")

    def test_unknown_tz_raises(self, clean_tz, monkeypatch):
        """不在降级表里、又没有 tzdata 的时区必须抛错，不许静默。"""
        monkeypatch.setenv(temporal.TZ_ENV, "Mars/Olympus_Mons")
        with pytest.raises(temporal.TzError):
            temporal.tz()

    def test_unknown_tz_error_mentions_name(self, clean_tz, monkeypatch):
        monkeypatch.setenv(temporal.TZ_ENV, "Nowhere/Special")
        with pytest.raises(temporal.TzError) as ei:
            temporal.tz()
        assert "Nowhere/Special" in str(ei.value)


# ---------------------------------------------------------------- 1. 时区转换

class TestNow:
    def test_now_has_tzinfo(self, clean_tz):
        n = temporal.now()
        assert n.tzinfo is not None

    def test_now_utc_matches_time_time(self, clean_tz):
        a = temporal.now_utc()
        b = time.time()
        assert abs(a - b) < 1.0

    def test_now_utc_is_float(self, clean_tz):
        assert isinstance(temporal.now_utc(), float)

    def test_tz_conversion_utc_to_local(self, clean_tz):
        """UTC 12:00 在 +08:00 应该是 20:00。"""
        utc_noon = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        local = utc_noon.astimezone(temporal.tz())
        assert local.hour == 20
        assert local.day == 15

    def test_tz_conversion_roundtrip(self, clean_tz):
        """本地 → UTC → 本地，回到原点。"""
        local = at_tz(9, 30)
        back = local.astimezone(timezone.utc).astimezone(temporal.tz())
        assert back.hour == 9 and back.minute == 30

    def test_mono_is_monotonic(self, clean_tz):
        a = temporal.mono()
        time.sleep(0.01)
        b = temporal.mono()
        assert b >= a

    def test_mono_elapsed_none(self, clean_tz):
        assert temporal.mono_elapsed(None) == 0.0

    def test_mono_elapsed_positive(self, clean_tz):
        t0 = temporal.mono()
        time.sleep(0.02)
        assert temporal.mono_elapsed(t0) >= 0.01


# ---------------------------------------------------------------- 3. 昼夜七档

class TestBand:
    @pytest.mark.parametrize("hour,expected", [
        (0, "凌晨"), (4, "凌晨"),
        (5, "清晨"), (7, "清晨"),
        (8, "上午"), (10, "上午"),
        (11, "中午"), (13, "中午"),
        (14, "下午"), (17, "下午"),
        (18, "晚上"), (21, "晚上"),
        (22, "深夜"), (23, "深夜"),
    ])
    def test_bands(self, clean_tz, hour, expected):
        assert temporal.band(at_tz(hour)) == expected

    def test_band_matches_rhythm(self, clean_tz):
        """必须复用 rhythm.band_name，不能是另一套算法。"""
        for h in range(24):
            d = at_tz(h)
            assert temporal.band(d) == rhythm.band_name(rhythm.circadian(d), d)

    def test_band_default_uses_now(self, clean_tz):
        assert temporal.band() in ("凌晨", "清晨", "上午", "中午", "下午", "晚上", "深夜")

    def test_weekday_cn(self, clean_tz):
        d = datetime(2026, 6, 15, 12, 0)  # 2026-06-15 是周一
        assert temporal.weekday_cn(d) == "周一"

    def test_weekday_all_seven(self, clean_tz):
        base = datetime(2026, 6, 15)  # 周一
        got = [temporal.weekday_cn(base + timedelta(days=i)) for i in range(7)]
        assert got == ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------------------------------------------------------- 2. 跨午夜

class TestAcrossMidnight:
    def test_band_changes_across_midnight(self, clean_tz):
        late = at_tz(23, 59, day=15)
        early = at_tz(0, 1, day=16)
        assert temporal.band(late) == "深夜"
        assert temporal.band(early) == "凌晨"

    def test_weekday_rolls_over(self, clean_tz):
        """周日 23:59 → 周一 00:01，星期要跳。"""
        sunday = at_tz(23, 59, day=21)   # 2026-06-21 是周日
        monday = at_tz(0, 1, day=22)
        assert temporal.weekday_cn(sunday) == "周日"
        assert temporal.weekday_cn(monday) == "周一"

    def test_elapsed_across_midnight(self, clean_tz):
        a = at_tz(23, 50, day=15).timestamp()
        b = at_tz(0, 10, day=16).timestamp()
        assert temporal.elapsed(a, b) == pytest.approx(20 * 60, abs=1.0)

    def test_fmt_gap_across_midnight_is_hours(self, clean_tz):
        assert temporal.fmt_gap(20 * 60) == "20 分钟"


# ---------------------------------------------------------------- 4. 间隔计算

class TestElapsed:
    def test_basic(self, clean_tz):
        assert temporal.elapsed(1000.0, 1060.0) == pytest.approx(60.0)

    def test_zero(self, clean_tz):
        assert temporal.elapsed(1000.0, 1000.0) == 0.0

    def test_uses_time_time_when_now_none(self, clean_tz):
        t0 = time.time() - 10
        assert temporal.elapsed(t0) == pytest.approx(10, abs=1.0)

    def test_accepts_int(self, clean_tz):
        assert temporal.elapsed(100, 200) == pytest.approx(100.0)

    def test_accepts_numeric_string(self, clean_tz):
        assert temporal.elapsed("100", "250") == pytest.approx(150.0)

    def test_raw_keeps_negative(self, clean_tz):
        assert temporal.elapsed_raw(200.0, 100.0) == pytest.approx(-100.0)


# ---------------------------------------------------------------- 5. 时钟回拨

class TestClockWentBack:
    def test_elapsed_clamps_to_zero(self, clean_tz):
        """回拨时 elapsed 必须给 0.0，不能给负数。"""
        assert temporal.elapsed(2000.0, 1000.0) == 0.0

    def test_raw_reports_truth(self, clean_tz):
        assert temporal.elapsed_raw(2000.0, 1000.0) == pytest.approx(-1000.0)

    def test_fmt_gap_negative_is_just_now(self, clean_tz):
        assert temporal.fmt_gap(-500) == "刚刚"

    def test_offline_flags_clock_went_back(self, clean_tz):
        off = temporal.offline(2000.0, now_ts=1000.0)
        assert off["clock_went_back"] is True
        assert off["seconds"] == 0.0
        assert off["is_restart"] is False


# ---------------------------------------------------------------- 6. fmt_gap 边界

class TestFmtGap:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "刚刚"),
        (1, "刚刚"),
        (59, "刚刚"),
        (59.9, "刚刚"),
        (60, "1 分钟"),
        (61, "1 分钟"),
        (119, "1 分钟"),
        (120, "2 分钟"),
        (3599, "59 分钟"),
        (3600, "1 小时"),
        (3660, "1 小时 1 分"),
        (7200, "2 小时"),
        (86399, "23 小时 59 分"),
        (86400, "昨天"),
        (86400 * 1.5, "昨天"),
        (86400 * 2, "2 天前"),
        (86400 * 6.9, "6 天前"),
        (86400 * 7, "1 周前"),
        (86400 * 14, "2 周前"),
    ])
    def test_boundaries(self, clean_tz, seconds, expected):
        assert temporal.fmt_gap(seconds) == expected

    def test_negative(self, clean_tz):
        assert temporal.fmt_gap(-1) == "刚刚"

    def test_float_input(self, clean_tz):
        assert temporal.fmt_gap(90.7) == "1 分钟"


# ---------------------------------------------------------------- 7. 离线

class TestOffline:
    def test_short_gap_is_not_restart(self, clean_tz):
        off = temporal.offline(1000.0, now_ts=1000.0 + 120)
        assert off["seconds"] == pytest.approx(120.0)
        assert off["is_restart"] is False
        assert off["is_long"] is False
        assert off["text"] == "2 分钟"

    def test_five_minutes_is_restart(self, clean_tz):
        off = temporal.offline(1000.0, now_ts=1000.0 + 300)
        assert off["is_restart"] is True

    def test_one_hour_is_long(self, clean_tz):
        off = temporal.offline(1000.0, now_ts=1000.0 + 3600)
        assert off["is_long"] is True
        assert off["text"] == "1 小时"

    def test_last_active_is_datetime_with_tz(self, clean_tz):
        off = temporal.offline(1000.0, now_ts=2000.0)
        assert isinstance(off["last_active"], datetime)
        assert off["last_active"].tzinfo is not None

    def test_zero_means_never(self, clean_tz):
        off = temporal.offline(0.0)
        assert off["seconds"] == 0.0
        assert off["is_restart"] is False
        assert off["last_active"] is None

    def test_none_means_never(self, clean_tz):
        off = temporal.offline(None)
        assert off["is_restart"] is False
        assert off["last_active"] is None

    def test_garbage_means_never(self, clean_tz):
        """非法输入不抛错（首次启动本来就没有上次），但也不瞎算。"""
        off = temporal.offline("not-a-timestamp")
        assert off["seconds"] == 0.0
        assert off["last_active"] is None

    def test_multiday(self, clean_tz):
        off = temporal.offline(1000.0, now_ts=1000.0 + 86400 * 3)
        assert off["text"] == "3 天前"
        assert off["is_long"] is True


# ---------------------------------------------------------------- 8. 非法输入

class TestInvalidInput:
    @pytest.mark.parametrize("bad", [None, "abc", object(), [1, 2], {"a": 1}])
    def test_elapsed_rejects_garbage(self, clean_tz, bad):
        with pytest.raises(ValueError):
            temporal.elapsed(bad)

    def test_elapsed_rejects_bool(self, clean_tz):
        """bool 是 int 的子类，容易漏；这里显式拦掉。"""
        with pytest.raises(ValueError):
            temporal.elapsed(True)

    def test_fmt_gap_rejects_garbage(self, clean_tz):
        with pytest.raises(ValueError):
            temporal.fmt_gap("abc")

    def test_fmt_gap_rejects_none(self, clean_tz):
        with pytest.raises(ValueError):
            temporal.fmt_gap(None)

    def test_now_ts_garbage(self, clean_tz):
        with pytest.raises(ValueError):
            temporal.elapsed(100.0, "xyz")


# ---------------------------------------------------------------- 上下文

class TestContext:
    def test_contains_time_and_band(self, clean_tz):
        ctx = temporal.context(dt=at_tz(23, 15))
        assert "23:15" in ctx
        assert "深夜" in ctx
        assert "周五" in ctx or "周" in ctx

    def test_mentions_last_active(self, clean_tz):
        now_ts = time.time()
        ctx = temporal.context(last_active_ts=now_ts - 3 * 3600 - 12 * 60,
                              dt=datetime.fromtimestamp(now_ts, temporal.tz()))
        assert "上次和他说话" in ctx

    def test_space_before_hour_gap(self, clean_tz):
        """「是 3 小时 12 分前」——数字前要有一个空格。"""
        now_ts = time.time()
        ctx = temporal.context(last_active_ts=now_ts - 3 * 3600 - 12 * 60,
                               dt=datetime.fromtimestamp(now_ts, temporal.tz()))
        assert "是 3 小时 12 分前" in ctx
        assert "是3 小时" not in ctx

    def test_space_before_minute_gap(self, clean_tz):
        now_ts = time.time()
        ctx = temporal.context(last_active_ts=now_ts - 25 * 60,
                               dt=datetime.fromtimestamp(now_ts, temporal.tz()))
        assert "是 25 分钟前" in ctx

    def test_yesterday_has_no_extra_qian(self, clean_tz):
        """「是昨天」——不能变成「是昨天前」，也不要空格。"""
        now_ts = time.time()
        ctx = temporal.context(last_active_ts=now_ts - 86400 * 1.2,
                               dt=datetime.fromtimestamp(now_ts, temporal.tz()))
        assert "是昨天" in ctx
        assert "昨天前" not in ctx

    def test_days_ago_no_double_qian(self, clean_tz):
        """「是3 天前」——本身已含「前」，不再补。"""
        now_ts = time.time()
        ctx = temporal.context(last_active_ts=now_ts - 86400 * 3,
                               dt=datetime.fromtimestamp(now_ts, temporal.tz()))
        assert "天前" in ctx
        assert "前前" not in ctx

    def test_short_when_recent(self, clean_tz):
        now_ts = time.time()
        ctx = temporal.context(last_active_ts=now_ts - 30,
                               dt=datetime.fromtimestamp(now_ts, temporal.tz()))
        assert "刚才还在聊" in ctx

    def test_no_last_active(self, clean_tz):
        ctx = temporal.context(last_active_ts=None, dt=at_tz(12))
        assert "上次" not in ctx
        assert ctx.endswith("。")

    def test_is_short(self, clean_tz):
        """bits 会塞进 prompt，这条必须短。"""
        ctx = temporal.context(last_active_ts=time.time() - 7200)
        assert len(ctx) <= 60


class TestGapPhrase:
    """`_gap_phrase()` 是 context 的内部拼装，单独钉住边界。"""

    @pytest.mark.parametrize("text,expected", [
        ("刚刚", "刚刚"),
        ("昨天", "昨天"),
        ("3 天前", "3 天前"),
        ("1 周前", "1 周前"),
        ("5 分钟", "5 分钟前"),
        ("3 小时 12 分", "3 小时 12 分前"),
    ])
    def test_append_qian_only_when_missing(self, text, expected):
        assert temporal._gap_phrase(text) == expected


# ---------------------------------------------------------------- 两种计时

class TestTwoClocksNeverMixed:
    """`mono()` 与 `now_utc()` 不属于同一参考系，不能相减。

    这组测试不验证「实现对不对」，而是钉住「两者数量级差太多」这个事实，
    防止以后有人把 `mono()` 的返回值存进库、再拿 `elapsed()` 去算。
    """

    def test_mono_and_utc_are_far_apart(self, clean_tz):
        m = temporal.mono()
        u = temporal.now_utc()
        # monotonic 起点通常是开机时间（几千到几十万秒），
        # now_utc 是 epoch 秒（约 1.8e9）。差至少一个数量级。
        assert abs(u - m) > 1e6

    def test_feeding_mono_into_elapsed_is_absurd(self, clean_tz):
        """把 mono() 喂给 elapsed() 会算出巨大的正数 —— 这就是混用的后果。"""
        m = temporal.mono()
        got = temporal.elapsed(m)
        # 不该是一个合理的「刚才」值
        assert got > 86400 * 365

    def test_mono_elapsed_ignores_wall_clock(self, clean_tz, monkeypatch):
        """mono_elapsed 只看单调时钟，wall clock 被人为改动也不受影响。"""
        t0 = temporal.mono()
        time.sleep(0.02)
        got = temporal.mono_elapsed(t0)
        assert 0.01 <= got < 5.0

    def test_elapsed_uses_wall_clock(self, clean_tz):
        """elapsed 用的是 UTC 时间戳，可以跨「任意长度」的时间。"""
        # 1970 年到现在
        assert temporal.elapsed(0.0) > 86400 * 365 * 50

    def test_storage_roundtrip_uses_utc(self, clean_tz):
        """跨重启的写法：存 now_utc()，读回来用 elapsed()。"""
        saved = temporal.now_utc() - 3600
        assert temporal.elapsed(saved) == pytest.approx(3600, abs=2.0)
        assert temporal.fmt_gap(temporal.elapsed(saved)) == "1 小时"


# ---------------------------------------------------------------- stats

class TestStats:
    def test_shape(self, clean_tz):
        s = temporal.stats()
        for key in ("tz_name", "tz_source", "now", "now_utc", "mono",
                    "band", "weekday", "fallback_available"):
            assert key in s

    def test_now_parsable(self, clean_tz):
        s = temporal.stats()
        assert datetime.fromisoformat(s["now"]) is not None
