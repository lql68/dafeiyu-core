"""节律层的单元测试。

    pytest -q

这些数字不是「约等于」，是从真实运行里抄下来的。
改公式就会红 —— 这是故意的：这些数是设计决策，不是巧合。
"""

from datetime import datetime

import pytest

from dafeiyu import rhythm


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 7, hour, minute, 0)


class TestCircadian:
    """SCN 周期：谷在 05:00、峰在 17:00。"""

    def test_trough_at_5am(self):
        assert rhythm.circadian(at(5)) == pytest.approx(0.0, abs=0.02)

    def test_peak_at_5pm(self):
        assert rhythm.circadian(at(17)) == pytest.approx(1.0, abs=0.02)

    def test_midpoint_at_11am(self):
        # 谷峰之间应该正好一半
        assert rhythm.circadian(at(11)) == pytest.approx(0.5, abs=0.02)

    def test_range_is_0_to_1(self):
        for h in range(24):
            c = rhythm.circadian(at(h))
            assert 0.0 <= c <= 1.0


class TestBandName:
    """时段名按小时直判，不用 circadian 反推（会绕）。"""

    @pytest.mark.parametrize(
        "hour,expected",
        [
            (3, "凌晨"),
            (6, "清晨"),
            (9, "上午"),
            (12, "中午"),
            (15, "下午"),
            (20, "晚上"),
            (23, "深夜"),
        ],
    )
    def test_bands(self, hour, expected):
        assert rhythm.band_name(0.5, at(hour)) == expected


class TestLag:
    """反应延迟是本层最值钱的输出 —— 她真的晚回。"""

    def test_afternoon_is_fast(self):
        v = rhythm.vitals(warmth=0.85, att_mode="outward", quiet_s=30, dt=at(15))
        assert v["lag"] < 1.2

    def test_early_morning_is_slow(self):
        v = rhythm.vitals(warmth=0.85, att_mode="outward", quiet_s=30, dt=at(3))
        assert v["lag"] > 2.0

    def test_sleeping_adds_seconds(self):
        awake = rhythm.vitals(warmth=0.85, att_mode="outward", quiet_s=30, dt=at(3))
        asleep = rhythm.vitals(warmth=0.85, att_mode="outward", quiet_s=900, dt=at(3))
        assert asleep["sleeping"] is True
        assert asleep["lag"] > awake["lag"] + 1.5

    def test_lag_is_clamped(self):
        # 极端情况不能算出「等 10 秒」
        v = rhythm.vitals(warmth=0.4, att_mode="self", quiet_s=99999, dt=at(4))
        assert 0.4 <= v["lag"] <= 5.0


class TestSleep:
    """睡眠是被节律谷底 + 长时间无刺激一起推出来的，不是到点断电。"""

    def test_not_sleepy_at_7am_even_if_quiet(self):
        # 第一版用 circadian < 0.15 判，会覆盖到 08:00 —— 早上八点还在睡不合理
        assert rhythm.sleepy(rhythm.circadian(at(7)), 720, at(7)) is False

    def test_sleepy_at_2am_when_quiet(self):
        assert rhythm.sleepy(rhythm.circadian(at(2)), 720, at(2)) is True

    def test_not_sleepy_at_2am_when_active(self):
        assert rhythm.sleepy(rhythm.circadian(at(2)), 60, at(2)) is False

    @pytest.mark.parametrize(
        "quiet_s,expected",
        [
            (600, "awake"),
            (610, "sws"),      # 入睡头 20 分钟最沉
            (2000, "light"),   # 33 分钟，过了头 20 分钟的深睡段
            (5000, "rem"),     # 73 分钟，周期末段在做梦
        ],
    )
    def test_stage(self, quiet_s, expected):
        assert rhythm.sleep_stage(quiet_s) == expected


class TestArousal:
    """唤醒不是开关，是被反复小幅推上去/放下来的。"""

    def test_soft_ceiling(self):
        # 不加软顶时 user_d=0.9 会顶到 1.0，顶满会让 hr/rr 长期挂着异常值
        a = rhythm.arousal_proxy(warmth=0.85, att_mode="outward", quiet_s=30, user_d=0.9)
        assert a < 1.0

    def test_self_mode_lowers(self):
        out = rhythm.arousal_proxy(warmth=0.85, att_mode="outward", quiet_s=300)
        slf = rhythm.arousal_proxy(warmth=0.85, att_mode="self", quiet_s=300)
        assert slf < out

    def test_user_delta_adds(self):
        base = rhythm.arousal_proxy(warmth=0.85, att_mode="outward", quiet_s=300)
        high = rhythm.arousal_proxy(warmth=0.85, att_mode="outward", quiet_s=300, user_d=0.2)
        assert high > base


class TestHint:
    """拼进 system 的一句话，别堆。"""

    def test_returns_nonempty(self):
        v = rhythm.vitals(dt=at(15))
        h = rhythm.hint(v)
        assert isinstance(h, str) and len(h) > 5

    def test_sleeping_hint_mentions_waking(self):
        v = rhythm.vitals(quiet_s=900, dt=at(3))
        h = rhythm.hint(v)
        assert "叫醒" in h or "睡" in h

    def test_high_arousal_mentions_heartbeat(self):
        v = rhythm.vitals(warmth=0.85, att_mode="outward", quiet_s=30, dt=at(15), user_d=0.3)
        if v["arousal"] >= 0.7:
            assert "心跳" in rhythm.hint(v)
