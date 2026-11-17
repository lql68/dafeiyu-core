"""时间底座：时区、单调时钟、间隔格式化、离线感知。

阶段 1 · 只做「怎么知道现在几点」这一层，不碰记忆 / 情绪 / 调度。

设计原则（写在这里免得以后自己忘）：

  1. **零第三方运行时依赖** —— `zoneinfo` 是标准库；缺少时区数据时降级到
     固定偏移，而不是报错或悄悄换成本地时间。
  2. **复用 `rhythm.band_name()`**，不复制昼夜判断算法。`band()` 只是
     把「现在几点 → 七档中文」这一步接过来。
  3. **两种计时方式绝不混用**：

     ==================  ==========================  ==========================
     用途                用什么                       为什么
     ==================  ==========================  ==========================
     进程内持续了多久     ``time.monotonic()``         不受系统时钟调整影响
     跨进程 / 跨重启      ``time.time()``（UTC 秒）    唯一能跨重启比较的刻度
     ==================  ==========================  ==========================

     ``mono()`` 与 ``now_utc()`` 的数值**不属于同一个参考系**，直接相减
     得到的是没有意义的数。本模块用函数名把两者分开：

     - ``mono()`` / ``mono_elapsed(t0)`` —— 只能喂 ``mono()`` 的返回值
     - ``elapsed(ts)`` / ``offline(ts)`` / ``fmt_gap()`` —— 只能喂 UTC 时间戳

     写库、写 JSON、跨重启比的，一律 ``now_utc()``；测「这段代码跑了多久」
     才用 ``mono()``。
  4. **时钟异常不静默**：配置无效抛 `TzError`；时间戳非法抛 `ValueError`；
     时钟回拨**不返回负数**（返回 0.0），但调用方可以通过 `elapsed_raw()`
     看到真实差值（负的）。
  5. **不写任何状态文件**。本模块纯函数，`offline()` 只负责算，落盘由
     调用方决定（阶段 5 才讨论 `temporal_state.json`）。

关于 Windows 缺时区数据（实测）：

    D:\\sakana-py\\python.exe   → ZoneInfoNotFoundError, 没有 tzdata
    AstrBot venv                → OK（tzdata 2026.5）

所以 `tz()` 会先试 `ZoneInfo`，失败后退到 `_FALLBACK_OFFSETS` 里的固定偏移。
`Asia/Shanghai` 全年 +08:00 无夏令时，固定偏移与 ZoneInfo 在这一时区上**等价**；
但对有夏令时的时区（如 `America/New_York`）不等价，**没在降级表里的一律抛错**，
不猜。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Optional

from . import rhythm

# ---------------------------------------------------------------- 常量

DEFAULT_TZ = "Asia/Shanghai"
TZ_ENV = "DFY_TZ"

#: 没有 tzdata 时允许降级的时区 → 固定 UTC 偏移（小时）。
#: **只放全年无夏令时的时区**；有夏令时的一律不放，宁可抛错。
_FALLBACK_OFFSETS: dict[str, float] = {
    "Asia/Shanghai": 8.0,
    "Asia/Chongqing": 8.0,
    "Asia/Hong_Kong": 8.0,
    "Asia/Macau": 8.0,
    "Asia/Taipei": 8.0,
    "Asia/Singapore": 8.0,
    "Asia/Tokyo": 9.0,
    "Asia/Seoul": 9.0,
    "Asia/Bangkok": 7.0,
    "UTC": 0.0,
    "Etc/UTC": 0.0,
}

#: `fmt_gap` 的分档边界（秒）。
_GAP_MINUTE = 60.0
_GAP_HOUR = 3600.0
_GAP_DAY = 86400.0
_GAP_WEEK = 604800.0

#: `offline()` 判定「算一次重启」的最小间隔（秒）。
_RESTART_MIN = 300.0


class TzError(RuntimeError):
    """时区配置不可用（名字写错、或缺 tzdata 且不在降级表里）。"""


# ---------------------------------------------------------------- 时区

def tz_name() -> str:
    """当前配置的时区名。环境变量 `DFY_TZ` 优先，缺省 `Asia/Shanghai`。"""
    return (os.environ.get(TZ_ENV) or "").strip() or DEFAULT_TZ


def tz() -> tzinfo:
    """解析时区。

    先试标准库 `zoneinfo`；缺时区数据时退到 `_FALLBACK_OFFSETS` 的固定偏移。
    **两路都不通就抛 `TzError`**，不退化成「本地时间」——那是最坏的一种静默。
    """
    name = tz_name()
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError / ValueError / ImportError
        hours = _FALLBACK_OFFSETS.get(name)
        if hours is None:
            raise TzError(
                "时区不可用: %r（%s）。装了 tzdata 可解，或改用降级表里的时区: %s"
                % (name, type(exc).__name__, ", ".join(sorted(_FALLBACK_OFFSETS)))
            ) from exc
        return timezone(timedelta(hours=hours), name)


def tz_source() -> str:
    """当前时区是怎么来的：`zoneinfo` / `fixed-offset`。

    给诊断用 —— 生产上如果是 `fixed-offset`，说明这台机器没装 tzdata。
    """
    name = tz_name()
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return "zoneinfo"
    except Exception:
        return "fixed-offset" if name in _FALLBACK_OFFSETS else "unavailable"


# ---------------------------------------------------------------- 现在

def now() -> datetime:
    """带时区的当前本地时间。"""
    return datetime.now(tz())


def now_utc() -> float:
    """UTC epoch 秒（与 `time.time()` 同源）。

    存量 / 持久化 / 跨重启比较**一律用这个**。
    不要拿它跟 `mono()` 相减 —— 两者不在同一个参考系。
    """
    return time.time()


def mono() -> float:
    """单调时钟（**只能**用于「这次进程里过了多久」）。

    `time.monotonic()` 的起点是任意的（通常是开机时间），
    **不能存进数据库、不能跨重启比较、不能和 `now_utc()` 相减**。
    要跨重启的时长请用两个 `now_utc()` 的差。
    """
    return time.monotonic()


def mono_elapsed(t0: Optional[float] = None) -> float:
    """自 `t0` 起的单调经过秒数。`t0=None` 时返回 0.0。

    `t0` **必须**是 `mono()` 的返回值；传 `now_utc()` 的值会得到无意义的数。
    """
    if t0 is None:
        return 0.0
    return max(0.0, time.monotonic() - float(t0))


# ---------------------------------------------------------------- 昼夜

def band(dt: Optional[datetime] = None) -> str:
    """昼夜七档中文名（凌晨 / 清晨 / 上午 / 中午 / 下午 / 晚上 / 深夜）。

    **复用 `rhythm.band_name()`**，不复制算法。`rhythm.band_name(c, dt)`
    的第一个参数（circadian 值）实际没有被使用（它直接按小时判），
    这里传 `circadian(dt)` 只是为了跟 `rhythm` 里其它调用点的写法保持一致。
    """
    d = dt or now()
    return rhythm.band_name(rhythm.circadian(d), d)


def weekday_cn(dt: Optional[datetime] = None) -> str:
    """星期几（周一…周日）。"""
    d = dt or now()
    return ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[d.weekday()]


# ---------------------------------------------------------------- 间隔

def _as_float(value: Any, what: str) -> float:
    """把入参转成 float；非法输入抛 ValueError（不吞成 0）。"""
    if isinstance(value, bool):  # bool 是 int 的子类，单独拦掉
        raise ValueError("%s 需要数字，收到 bool: %r" % (what, value))
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s 需要数字，收到 %r" % (what, value)) from exc


def elapsed(ts: Any, now_ts: Optional[float] = None) -> float:
    """从 `ts`（epoch 秒）到现在的经过秒数。

    **时钟回拨时返回 0.0，不返回负数** —— 下游衰减公式拿到负数会算出
    大于 1 的系数（"情绪变强了"），那比 0 危险得多。

    想看真实差值（可能为负）用 `elapsed_raw()`。
    """
    d = elapsed_raw(ts, now_ts)
    return 0.0 if d < 0.0 else d


def elapsed_raw(ts: Any, now_ts: Optional[float] = None) -> float:
    """同 `elapsed()`，但**保留负值**（系统时钟被往回拨时为负）。

    只给诊断 / 告警用，不要拿它喂衰减公式。
    """
    t0 = _as_float(ts, "elapsed(ts)")
    t1 = time.time() if now_ts is None else _as_float(now_ts, "elapsed(now_ts)")
    return t1 - t0


def fmt_gap(seconds: Any) -> str:
    """把秒数转成人话。

    分档（与审计报告 E.1 的表一致）::

        < 60s        「刚刚」
        < 1h         「N 分钟」
        < 1d         「N 小时 M 分」（M=0 时只说小时）
        1d ~ 2d      「昨天」
        < 7d         「N 天前」
        >= 7d        「N 周前」

    负数（时钟回拨）也返回「刚刚」——对外话术上不该出现「-3 分钟前」。
    **非法输入抛 `ValueError`**，不静默变「刚刚」。
    """
    s = _as_float(seconds, "fmt_gap(seconds)")
    if s < _GAP_MINUTE:
        return "刚刚"
    if s < _GAP_HOUR:
        return "%d 分钟" % int(s // 60)
    if s < _GAP_DAY:
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        return "%d 小时" % h if m == 0 else "%d 小时 %d 分" % (h, m)
    if s < _GAP_DAY * 2:
        return "昨天"
    if s < _GAP_WEEK:
        return "%d 天前" % int(s // _GAP_DAY)
    return "%d 周前" % int(s // _GAP_WEEK)


# ---------------------------------------------------------------- 离线

def offline(last_active_ts: Any, now_ts: Optional[float] = None) -> dict[str, Any]:
    """算「上次活跃到现在」隔了多久，以及这算不算一次重启。

    返回::

        {
          "seconds":  3600.0,        # 经过秒数（回拨时为 0.0）
          "raw":      3600.0,        # 真实差值（可能为负）
          "text":     "1 小时",      # fmt_gap 的结果
          "is_restart": True,        # >= 5 分钟算重启
          "is_long":  True,          # >= 1 小时算「离开了挺久」
          "clock_went_back": False,  # 系统时钟是否被往回拨了
          "last_active": datetime,   # 上次活跃的本地时间（带时区）
        }

    `last_active_ts` 为 0 / None / 非法时按「从未活跃」处理（`is_restart=False`，
    `seconds=0.0`），因为首次启动本来就没有「上次」。
    """
    try:
        t0 = _as_float(last_active_ts, "offline(last_active_ts)")
        valid = t0 > 0.0
    except ValueError:
        t0, valid = 0.0, False

    if not valid:
        return {
            "seconds": 0.0,
            "raw": 0.0,
            "text": "刚刚",
            "is_restart": False,
            "is_long": False,
            "clock_went_back": False,
            "last_active": None,
        }

    raw = elapsed_raw(t0, now_ts)
    sec = 0.0 if raw < 0.0 else raw
    return {
        "seconds": sec,
        "raw": raw,
        "text": fmt_gap(sec),
        "is_restart": sec >= _RESTART_MIN,
        "is_long": sec >= _GAP_HOUR,
        "clock_went_back": raw < 0.0,
        "last_active": datetime.fromtimestamp(t0, tz()),
    }


# ---------------------------------------------------------------- 上下文

def _gap_phrase(text: str) -> str:
    """把 `fmt_gap()` 的结果接在「是」后面，补足「…前」。

    `fmt_gap` 的六档输出里，只有「N 分钟」「N 小时 M 分」需要补「前」；
    「昨天」「N 天前」「N 周前」本身已经是完整说法，再补就成「昨天前」。
    """
    if text == "刚刚":
        return "刚刚"
    if text == "昨天" or text.endswith("前"):
        return text
    return text + "前"


def context(last_active_ts: Optional[float] = None,
            dt: Optional[datetime] = None) -> str:
    """拼一小段给人格模型看的时间上下文（≤ 60 字）。

    例：``现在是 2026年11月12日 周四 深夜 23:15，你上次和他说话，是 3 小时 12 分前。``

    **故意短**：`bits` 会塞进 prompt，每多一句都在挤记忆的位置。
    `last_active_ts` 为 None 时不提「上次」。

    日期写全（年月日），因为模型光看「周四」不知道是哪一周；
    但用「11月12日」而不是「2026-11-12」，读起来像人说的话。
    """
    d = dt or now()
    parts = ["现在是%d年%d月%d日 %s %s %02d:%02d"
             % (d.year, d.month, d.day, weekday_cn(d), band(d), d.hour, d.minute)]
    if last_active_ts:
        off = offline(last_active_ts)
        if off["is_restart"]:
            phrase = _gap_phrase(off["text"])
            # 「是 12 小时 48 分前」要空格；「是昨天」不要
            sep = "" if phrase in ("刚刚", "昨天") else " "
            parts.append("你上次和他说话，是%s%s" % (sep, phrase))
        elif off["seconds"] > 0:
            parts.append("刚才还在聊")
    return "，".join(parts) + "。"


def stats() -> dict[str, Any]:
    """自检快照，给 `/temporal` 路由和诊断用。"""
    d = now()
    return {
        "tz_name": tz_name(),
        "tz_source": tz_source(),
        "now": d.isoformat(),
        "now_utc": now_utc(),
        "mono": mono(),
        "band": band(d),
        "weekday": weekday_cn(d),
        "fallback_available": tz_name() in _FALLBACK_OFFSETS,
    }


# ---------------------------------------------------------------------------
# 中文时间表达解析（阶段 3 时间化记忆）
#
# 设计原则（与报告一致）：
#   1. 相对日期按 temporal.tz() 的本地日历边界算，左闭右开 [start, end)
#   2. 不确定年份时不伪造精确年份 -> precision 降级为 approximate
#   3. 纯时段词（上午/下午/晚上）只记时段，日期保持 unknown，不默认成今天
#   4. 解析不出来就 unknown，不猜
# ---------------------------------------------------------------------------

_WEEKDAY_MAP = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}

# 相对日（整天）
_DAY_WORDS = {
    "大前天": -3, "前天": -2, "昨天": -1, "今天": 0, "今日": 0,
    "明天": 1, "明日": 1, "后天": 2, "大后天": 3,
}

# 纯时段词 -> (起始小时, 结束小时)，日期未知
_BANDS = {
    "凌晨": (0, 6), "早上": (6, 9), "上午": (8, 12),
    "中午": (11, 14), "下午": (13, 18), "傍晚": (17, 20),
    "晚上": (19, 24), "夜里": (21, 24), "深夜": (22, 24),
}

# 模糊时间 -> (相对天数跨度, 精度)
_VAGUE = {
    "前几天": (7, "relative"),
    "前段时间": (30, "relative"),
    "最近": (14, "relative"),
    "近来": (14, "relative"),
}

# 明确无法定位的 -> unknown
_UNKNOWNISH = ("小时候", "以前", "从前", "很久以前", "有一天", "某天", "那次", "当年")


def _day_bounds(d):
    """某一天在本地时区下的 [00:00, 次日 00:00) -> (start_ts, end_ts)。"""
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    s = _dt(d.year, d.month, d.day, tzinfo=tz())
    e = s + _td(days=1)
    return s.timestamp(), e.timestamp()


def _week_bounds(d, offset_weeks=0):
    """以 d 所在自然周（周一为首）加 offset 周 -> (start_ts, end_ts)。"""
    from datetime import timedelta as _td
    monday = d - _td(days=d.weekday()) + _td(weeks=offset_weeks)
    s = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    e = s + _td(days=7)
    return s.timestamp(), e.timestamp()


def _status_of(start, end, now_ts):
    if start is None or end is None:
        return "unknown"
    if end <= now_ts:
        return "past"
    if start > now_ts:
        return "future"
    return "unknown"          # 区间跨过现在，但不做 ongoing


def _mk(start, end, precision, label, source, now_ts):
    return {
        "start": start,
        "end": end,
        "precision": precision,
        "status": _status_of(start, end, now_ts),
        "label": label,
        "source": source,
        "tz": tz_name(),
    }


def parse_cn_time(text, now_dt=None):
    """解析文本里的中文时间表达。

    返回 dict: start / end / precision / status / label / source / tz
      start, end : float epoch 秒，或 None（未知）
      precision  : exact / approximate / relative / unknown
      status     : past / future / unknown
    """
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    import re as _re

    if not isinstance(text, str) or not text.strip():
        return _mk(None, None, "unknown", "", "empty", now_ts=now_ts_())

    t = text.strip()
    now_dt = now_dt or now()
    now_ts = now_dt.timestamp()

    # 1) 年月日（精确）
    m = _re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", t)
    if m:
        try:
            d = _dt(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=tz())
            s, e = _day_bounds(d)
            return _mk(s, e, "exact", "%d年%d月%d日" % (d.year, d.month, d.day), "ymd", now_ts)
        except ValueError:
            pass

    # 2) 月日（无年份 -> approximate，不伪造年份）
    m = _re.search(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", t)
    if m:
        mo, dy = int(m.group(1)), int(m.group(2))
        year = now_dt.year
        for _try in (year, year + 1):
            try:
                d = _dt(_try, mo, dy, tzinfo=tz())
                s, e = _day_bounds(d)
                if e > now_ts:                    # 取最近的、还没完全过去的那次
                    return _mk(s, e, "approximate",
                               "%d月%d日（年份未说明）" % (mo, dy), "month_day", now_ts)
            except ValueError:
                continue
        return _mk(None, None, "unknown", "%d月%d日（非法日期）" % (mo, dy), "month_day_bad", now_ts)

    # 3) 周表达：上周三 / 本周五 / 下周二 / 周二
    m = _re.search(r"(上上|上|本|这|下下|下)?\s*周\s*([一二三四五六日天])", t)
    if m:
        prefix = m.group(1) or ""
        wd = _WEEKDAY_MAP[m.group(2)]
        off = {"上上": -2, "上": -1, "本": 0, "这": 0, "下下": 2, "下": 1, "": 0}[prefix]
        base = now_dt - _td(days=now_dt.weekday()) + _td(weeks=off, days=wd)
        s, e = _day_bounds(base)
        prec = "exact" if prefix else "approximate"     # 光「周二」不知道哪一周
        label = "%s周%s" % (prefix, m.group(2))
        return _mk(s, e, prec, label, "weekday", now_ts)

    # 4) 整周：上周 / 本周 / 下周 / 上上周 / 下下周
    m = _re.search(r"(上上|上|本|这|下下|下)\s*周(?![一二三四五六日天])", t)
    if m:
        prefix = m.group(1)
        off = {"上上": -2, "上": -1, "本": 0, "这": 0, "下下": 2, "下": 1}[prefix]
        s, e = _week_bounds(now_dt, off)
        return _mk(s, e, "exact", "%s周（整周）" % prefix, "week", now_ts)

    # 5) 相对日：今天 / 昨天 / 前天 / 大前天 / 明天 / 后天 / 大后天
    # 按词长降序：「大后天」必须先于「后天」匹配，否则子串抢先
    for word, delta in sorted(_DAY_WORDS.items(), key=lambda kv: -len(kv[0])):
        if word in t:
            d = now_dt + _td(days=delta)
            _band = None
            for bw, (h0, h1) in _BANDS.items():
                if bw in t:
                    _band = (bw, h0, h1)
                    break
            if _band:
                bw, h0, h1 = _band
                s = _dt(d.year, d.month, d.day, h0, 0, tzinfo=tz()).timestamp()
                ey, em, ed = (d.year, d.month, d.day + 1) if h1 >= 24 else (d.year, d.month, d.day)
                if h1 >= 24:
                    from datetime import datetime as _dt2
                    _n = _dt(d.year, d.month, d.day, tzinfo=tz()) + _td(days=1)
                    e = _dt(_n.year, _n.month, _n.day, 0, 0, tzinfo=tz()).timestamp()
                else:
                    e = _dt(d.year, d.month, d.day, h1, 0, tzinfo=tz()).timestamp()
                return _mk(s, e, "exact", "%s%s" % (word, bw), "day_word_band", now_ts)
            s, e = _day_bounds(d)
            return _mk(s, e, "exact", word, "day_word", now_ts)

    # 6) 去年夏天 / 今年冬天（季节 -> approximate）
    m = _re.search(r"(前年|去年|今年|明年)\s*(春天|夏天|秋天|冬天|春季|夏季|秋季|冬季)", t)
    if m:
        y = now_dt.year + {"前年": -2, "去年": -1, "今年": 0, "明年": 1}[m.group(1)]
        season = m.group(2)[0]
        rng = {"春": (3, 6), "夏": (6, 9), "秋": (9, 12), "冬": (12, 15)}[season]
        try:
            s = _dt(y, rng[0], 1, tzinfo=tz()).timestamp()
            ey, em = (y + 1, rng[1] - 12) if rng[1] > 12 else (y, rng[1])
            e = _dt(ey, em, 1, tzinfo=tz()).timestamp()
            return _mk(s, e, "approximate", "%s%s" % (m.group(1), m.group(2)), "season", now_ts)
        except ValueError:
            pass

    # 6.5) 上个月 / 这个月 / 下个月（整月，左闭右开）
    m = _re.search(r"(上上|上|本|这|下下|下)个?月(?!\d|\s*\d)", t)
    if m:
        w = m.group(1)
        off = {"上上": -2, "上": -1, "本": 0, "这": 0, "下下": 2, "下": 1}[w]
        y0, m0 = now_dt.year, now_dt.month
        idx = (y0 * 12 + (m0 - 1)) + off
        sy, sm = idx // 12, idx % 12 + 1
        ey, em = (sy + 1, 1) if sm == 12 else (sy, sm + 1)
        try:
            s = _dt(sy, sm, 1, tzinfo=tz()).timestamp()
            e = _dt(ey, em, 1, tzinfo=tz()).timestamp()
            label = {"上上": "上上个月", "上": "上个月", "本": "这个月", "这": "这个月",
                     "下下": "下下个月", "下": "下个月"}[w]
            return _mk(s, e, "exact", label, "month", now_ts)
        except ValueError:
            pass

    # 7) 去年 / 今年 / 明年（整年）
    m = _re.search(r"(前年|去年|今年|明年|后年)", t)
    if m:
        w = m.group(1)
        off = {"前年": -2, "去年": -1, "今年": 0, "明年": 1, "后年": 2}[w]
        y = now_dt.year + off
        try:
            s = _dt(y, 1, 1, tzinfo=tz()).timestamp()
            e = _dt(y + 1, 1, 1, tzinfo=tz()).timestamp()
            return _mk(s, e, "exact", "%s（%d 年）" % (w, y), "year", now_ts)
        except ValueError:
            pass

    # 8) 模糊跨度：最近 / 前几天 / 前段时间
    for word, (days, prec) in _VAGUE.items():
        if word in t:
            s = (now_dt - _td(days=days)).timestamp()
            e = now_ts
            return _mk(s, e, prec, "%s（近 %d 天）" % (word, days), "vague_span", now_ts)

    # 9) 明确无法定位
    for word in _UNKNOWNISH:
        if word in t:
            return _mk(None, None, "unknown", word, "vague_word", now_ts)

    # 10) 纯时段词（只有钟点，没有日期）—— 日期保持未知
    for word, (h0, h1) in _BANDS.items():
        if word in t:
            return _mk(None, None, "unknown", "%s（具体哪天没说）" % word, "band_only", now_ts)

    return _mk(None, None, "unknown", "", "none", now_ts)


def now_ts_():
    """给 parse_cn_time 的兜底分支用（没有 now_dt 时）。"""
    return now_utc()


def query_range(text, now_dt=None):
    """把用户的问题解析成检索区间。

    返回 (start_ts, end_ts) 或 (None, None)。
    只有解析出**日期级**结果才返回区间；纯时段 / unknown 一律 (None, None)，
    避免「下午」被当成「今天下午」而漏掉别的天。
    """
    r = parse_cn_time(text, now_dt)
    if r["precision"] == "unknown" or r["start"] is None:
        return None, None
    return r["start"], r["end"]
