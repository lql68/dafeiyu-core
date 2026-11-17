"""阶段 3：parse_cn_time / query_range 的行为测试。

全部用固定的 now_dt，不依赖真实时钟。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from dafeiyu.kernel import temporal

# 固定「现在」= 2026-11-14（周六）15:30 本地时间
NOW = temporal.tz().localize(datetime(2026, 11, 14, 15, 30)) if hasattr(
    temporal.tz(), "localize"
) else datetime(2026, 11, 14, 15, 30, tzinfo=temporal.tz())

DAY = 86400.0


def parse(text, now=NOW):
    return temporal.parse_cn_time(text, now_dt=now)


def day_start(d: datetime) -> float:
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


# ---------------------------------------------------------------- 绝对日期
class TestAbsoluteDate:
    def test_年月日_exact(self):
        r = parse("2026年11月10日开会")
        assert r["precision"] == "exact"
        assert r["status"] == "past"
        assert r["start"] == pytest.approx(day_start(datetime(2026, 11, 10, tzinfo=NOW.tzinfo)))
        assert r["end"] - r["start"] == pytest.approx(DAY, abs=1)

    def test_月日_无年份是approximate(self):
        r = parse("5月30日截止")
        assert r["precision"] == "approximate"
        assert r["status"] == "future"
        assert "年份未说明" in r["label"]

    def test_月日_跨年推一年(self):
        # 现在是 11 月，「3月5日」应指向明年
        r = parse("3月5日")
        assert r["precision"] == "approximate"
        assert r["status"] == "future"
        y = datetime.fromtimestamp(r["start"], tz=NOW.tzinfo).year
        assert y == 2027

    def test_月日_今年已过用明年(self):
        r = parse("2月1日")
        assert r["precision"] == "approximate"
        y = datetime.fromtimestamp(r["start"], tz=NOW.tzinfo).year
        assert y == 2027


# ---------------------------------------------------------------- 相对日
class TestRelativeDay:
    @pytest.mark.parametrize(
        "word,offset",
        [("大前天", -3), ("前天", -2), ("昨天", -1), ("今天", 0), ("明天", 1), ("后天", 2), ("大后天", 3)],
    )
    def test_日偏移(self, word, offset):
        r = parse("%s去了公司" % word)
        assert r["precision"] == "exact"
        want = day_start(datetime(2026, 11, 14, tzinfo=NOW.tzinfo)) + offset * DAY
        assert r["start"] == pytest.approx(want)
        assert r["end"] - r["start"] == pytest.approx(DAY, abs=1)

    def test_昨天是past(self):
        assert parse("昨天我去钓鱼")["status"] == "past"

    def test_明天是future(self):
        assert parse("明天见")["status"] == "future"

    def test_相对日加时段取时段区间(self):
        r = parse("今天下午在看书")
        assert r["precision"] == "exact"
        assert r["end"] - r["start"] < DAY          # 不是整天
        assert r["end"] - r["start"] > 3600         # 也不是一小时


# ---------------------------------------------------------------- 周
class TestWeek:
    def test_本周整周(self):
        r = parse("本周")
        assert r["precision"] == "exact"
        assert r["end"] - r["start"] == pytest.approx(7 * DAY, abs=2)

    def test_上周是三周前的相对(self):
        r = parse("上周开会")
        assert r["precision"] == "exact"
        assert r["status"] == "past"

    def test_下周是future(self):
        r = parse("下周")
        assert r["precision"] == "exact"
        assert r["status"] == "future"

    def test_下周二(self):
        r = parse("下周二提交")
        assert r["precision"] == "exact"
        assert r["status"] == "future"

    def test_上周三(self):
        r = parse("上周三开会")
        assert r["precision"] == "exact"
        assert r["status"] == "past"

    def test_光周二不带前缀是approximate(self):
        r = parse("周二开会")
        assert r["precision"] == "approximate"

    def test_周一起算(self):
        # 2026-11-16 是周一
        r = parse("下周一")
        d = datetime.fromtimestamp(r["start"], tz=NOW.tzinfo)
        assert d.weekday() == 0


# ---------------------------------------------------------------- 季节 / 年
class TestSeasonYear:
    def test_去年夏天是approximate(self):
        r = parse("去年夏天")
        assert r["precision"] == "approximate"
        assert r["status"] == "past"

    def test_冬天跨年(self):
        r = parse("去年冬天")
        assert r["precision"] == "approximate"
        # 冬天应跨到次年，区间长于一个季度的一半
        assert r["end"] - r["start"] > 60 * DAY

    def test_去年整年(self):
        r = parse("去年")
        assert r["precision"] == "exact"
        assert r["end"] - r["start"] > 360 * DAY


# ---------------------------------------------------------------- 模糊
class TestVague:
    def test_前段时间是relative(self):
        r = parse("前段时间很忙")
        assert r["precision"] == "relative"
        assert r["status"] == "past"

    def test_最近是relative(self):
        assert parse("最近")["precision"] == "relative"

    def test_前几天(self):
        assert parse("前几天")["precision"] == "relative"


# ---------------------------------------------------------------- 未知
class TestUnknown:
    @pytest.mark.parametrize("word", ["小时候", "以前", "从前", "很久以前", "有一天", "某天", "当年"])
    def test_模糊词返回unknown(self, word):
        r = parse("%s住在海边" % word)
        assert r["precision"] == "unknown"
        assert r["status"] == "unknown"
        assert r["start"] is None and r["end"] is None

    def test_纯时段不生成日期(self):
        r = parse("下午看书")
        assert r["precision"] == "unknown"
        assert r["start"] is None
        assert "具体哪天没说" in r["label"]

    def test_纯时段上午(self):
        r = parse("上午在上课")
        assert r["precision"] == "unknown"
        assert r["start"] is None

    def test_纯时段晚上(self):
        r = parse("晚上去散步")
        assert r["precision"] == "unknown"
        assert r["start"] is None

    def test_完全没有时间词(self):
        r = parse("张三喜欢喝咖啡")
        assert r["precision"] == "unknown"

    def test_空字符串(self):
        assert parse("")["precision"] == "unknown"


# ---------------------------------------------------------------- query_range
class TestQueryRange:
    def test_exact返回区间(self):
        s, e = temporal.query_range("昨天")
        assert s is not None and e is not None and e > s

    def test_unknown返回None(self):
        s, e = temporal.query_range("小时候")
        assert s is None and e is None

    def test_纯时段返回None(self):
        s, e = temporal.query_range("下午")
        assert s is None and e is None

    def test_无时间词返回None(self):
        assert temporal.query_range("张三喜欢咖啡") == (None, None)


# ---------------------------------------------------------------- 结构完整性
class TestShape:
    def test_七个键齐全(self):
        r = parse("昨天")
        for k in ("start", "end", "precision", "status", "label", "source", "tz"):
            assert k in r, "缺字段 %s" % k

    def test_tz是字符串(self):
        assert isinstance(parse("昨天")["tz"], str)

    def test_precision只能是四档(self):
        for text in ("昨天", "5月30日", "前段时间", "小时候", ""):
            assert parse(text)["precision"] in ("exact", "approximate", "relative", "unknown")

    def test_status只能是三档(self):
        for text in ("昨天", "明天", "小时候", ""):
            assert parse(text)["status"] in ("past", "future", "unknown")

    def test_不返回ongoing(self):
        now = datetime(2026, 11, 14, 15, 30, tzinfo=NOW.tzinfo)
        # 「今天」的区间包含 now，但按设计不做 ongoing
        r = temporal.parse_cn_time("今天", now_dt=now)
        assert r["status"] in ("past", "future", "unknown")


# ---------------------------------------------------------------- 时区
class TestTimezone:
    def test_用temporal的时区(self):
        r = parse("昨天")
        assert r["tz"] == temporal.tz_name()

    def test_换时区后边界跟着换(self, monkeypatch):
        monkeypatch.setenv("DFY_TZ", "Asia/Tokyo")
        r = temporal.parse_cn_time("昨天", now_dt=NOW)
        assert r["tz"] == "Asia/Tokyo"


# ---------------------------------------------------------------- 整月
class TestMonth:
    """整月区间（左闭右开，含跨年）"""

    def test_上个月(self):
        r = parse("上个月我们讨论过什么")
        assert r["precision"] == "exact"
        assert r["label"] == "上个月"
        m = datetime.fromtimestamp(r["start"], temporal.tz())
        assert (m.year, m.month, m.day) == (2026, 10, 1)

    def test_这个月(self):
        r = parse("这个月做了什么")
        m = datetime.fromtimestamp(r["start"], temporal.tz())
        assert (m.year, m.month, m.day) == (2026, 11, 1)

    def test_下个月_跨年(self):
        later = datetime(2026, 12, 20, 10, 0, tzinfo=temporal.tz())
        r = parse("下个月计划", later)
        s_ = datetime.fromtimestamp(r["start"], temporal.tz())
        e_ = datetime.fromtimestamp(r["end"], temporal.tz())
        assert (s_.year, s_.month, s_.day) == (2027, 1, 1)
        assert (e_.year, e_.month, e_.day) == (2027, 2, 1)

    def test_一月的上个月跨年回退(self):
        jan = datetime(2027, 1, 15, 10, 0, tzinfo=temporal.tz())
        r = parse("上个月", jan)
        s_ = datetime.fromtimestamp(r["start"], temporal.tz())
        e_ = datetime.fromtimestamp(r["end"], temporal.tz())
        assert (s_.year, s_.month) == (2026, 12)
        assert (e_.year, e_.month) == (2027, 1)

    def test_左闭右开(self):
        r = parse("上个月")
        s_ = datetime.fromtimestamp(r["start"], temporal.tz())
        e_ = datetime.fromtimestamp(r["end"], temporal.tz())
        assert s_.day == 1 and s_.hour == 0 and s_.minute == 0
        assert e_.day == 1 and e_.hour == 0 and e_.minute == 0
        assert (e_ - s_).days in (28, 29, 30, 31)

    def test_query_range_上个月(self):
        a, b = temporal.query_range("上个月", NOW)
        assert a is not None and b is not None
