"""方案 P0 · 节律层：给她一条真的会走的生理基线。

依据《人体生理学与生物学知识整理》：
  1.5  生物节律 —— SCN 周期 24.2h；皮质醇清晨峰、体温傍晚最高凌晨最低
  7.1  心率 75 次/分、0.8s 心动周期
  8.2  呼吸 12 次/分、潮气量 500mL；深慢呼吸比浅快更有效
  4.5  交感/副交感是紧张性共同调节（不是开关）

设计原则（写在这里免得以后自己忘）：
  - 只输出「能落到句长 / 延迟 / HUD 数字」的量，不做科普
  - 数字是编的，但形状是真的（余弦节律 + 负反馈回归）
  - 绝不出现伪医学精度（37.2℃ 这种）
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def circadian(dt: Optional[datetime] = None) -> float:
    """她的'体温曲线'，0=最低（凌晨5点）1=最高（傍晚5点）。"""
    now = dt or datetime.now()
    h = now.hour + now.minute / 60.0
    # 谷在 05:00、峰在 17:00 —— 对应知识库 1.5「傍晚最高、凌晨最低」
    return 0.5 - 0.5 * math.cos(2 * math.pi * (h - 5.0) / 24.0)


def band_name(c: float, dt: Optional[datetime] = None) -> str:
    """不要用 circadian 反推时段名（会绕），直接用小时。"""
    now = dt or datetime.now()
    h = now.hour
    if h < 5:
        return "凌晨"
    if h < 8:
        return "清晨"
    if h < 11:
        return "上午"
    if h < 14:
        return "中午"
    if h < 18:
        return "下午"
    if h < 22:
        return "晚上"
    return "深夜"


def user_arousal(gap_s: Optional[float], length: int, burst: int,
                 emo: float) -> float:
    """从他「怎么说话」推她的唤醒度增量（P1 的核心）。

    依据知识库 4.5「交感/副交感是紧张性共同调节」：
      唤醒不是开关，是被反复小幅推上去/放下来的。

    参数
    ----
    gap_s  : 距他上一次说话的秒数；None = 今天第一次开口（无参照，不推）
    length : 这句话多长（字）
    burst  : 连续第几条（3 分钟内算连着）
    emo    : 这句话的情绪强度 0~1
    """
    d = 0.0
    # 停顿长度：刚聊完又开口 = 有劲；十几分钟没声 = 她静下来
    if gap_s is None:
        pass
    elif gap_s < 60:
        d += 0.10
    elif gap_s < 300:
        d += 0.04
    elif gap_s > 900:
        d -= 0.12

    # 连发：越密越像正在兴头上
    if burst >= 3:
        d += 0.16
    elif burst >= 2:
        d += 0.08

    # 长度：说一长段 = 投入；只回一个字 = 没劲
    if length >= 30:
        d += 0.08
    elif length <= 5:
        d -= 0.06

    # 情绪
    d += 0.12 * float(emo or 0.0)

    return _clamp(d, -0.30, 0.30)


def arousal_proxy(warmth: float = 0.85, att_mode: str = "outward",
                  quiet_s: float = 0.0, user_d: float = 0.0) -> float:
    """唤醒度 = 基线 + 注意力态 + 他的说话节奏（user_d）。

    user_d 来自 user_arousal()：连发/长句/带情绪把她推高，
    久不说话把她放低。这是四象限真能动起来的原因。
    """
    a = 0.45
    if att_mode == "self":
        a -= 0.20
    elif att_mode == "inward":
        a -= 0.05
    if quiet_s < 60:
        a += 0.15          # 刚有交互
    elif quiet_s > 600:
        a -= 0.10          # 十分钟没人理
    a += (float(warmth) - 0.85) * 0.30
    a += float(user_d or 0.0)
    # 软顶：0.85 以上增量减半 —— 他连发 4 条 + 长句 + 情绪时不要一下顶到 1.0，
    # 顶满会让 hr/rr/hint 长期挂着「心跳有点快」，反而不像人。
    if a > 0.85:
        a = 0.85 + (a - 0.85) * 0.5
    # 上限留 0.95：软顶之后又被 clamp 到 1.0 等于软顶白做（a=1.5 时软顶给 1.175）
    return _clamp(a, 0.05, 0.95)


def sleepy(c: float, quiet_s: float, dt: Optional[datetime] = None) -> bool:
    """该不该睡。

    依据知识库 4.6：睡眠是被「节律谷底 + 长时间无刺激」一起推出来的，
    不是到点就断电。

    第一版用 circadian < 0.15 判，实测覆盖到 08:00 太宽（早上八点还在睡不合理），
    改成直接按小时划夜间窗口 23:00-06:59，再用 quiet_s 确认「没人理」。
    """
    now = dt or datetime.now()
    night = (now.hour >= 23) or (now.hour <= 6)
    return bool(night and quiet_s > 600)


def sleep_stage(quiet_s: float) -> str:
    """睡着以后按 90 分钟周期分阶段。

    依据知识库 4.6：正常睡眠 90min 一个周期，SWS（慢波/深睡）恢复体力、
    REM 巩固记忆。她睡着不是「一个 sleeping=True 就完了」——
    深睡被叫醒和做梦被叫醒，说话样子不一样。
    """
    if quiet_s <= 600:
        return "awake"
    mins = (quiet_s - 600.0) / 60.0
    phase = mins % 90.0
    if phase < 20:
        return "sws"        # 入睡头 20 分钟睡得最沉
    if phase < 70:
        return "light"
    return "rem"            # 周期末段在做梦


def vitals(warmth: float = 0.85, att_mode: str = "outward",
           quiet_s: float = 0.0, dt: Optional[datetime] = None,
           user_d: float = 0.0) -> dict[str, Any]:
    c = circadian(dt)
    a = arousal_proxy(warmth, att_mode, quiet_s, user_d)

    # 心率：安静 62、高唤醒 88，叠一个 0.8s 级别的呼吸性窦性心律不齐
    hr = 62.0 + 26.0 * a + 3.5 * math.sin(datetime.now().timestamp() / 6.0)
    # 呼吸：10–19 次/分；深夜上限压低（褪黑素抑制交感）
    rr = 11.0 + 7.0 * a
    if c < 0.25:
        rr *= 0.85
    # 反应延迟：这是本层最值钱的输出 —— 她真的晚回
    lag = 0.6 + 1.4 * (1.0 - c) + 0.8 * (1.0 - a)
    # 睡着了被叫醒，反应慢很多（不是装的，是翻个身才反应过来）
    _sleep = sleepy(c, quiet_s, dt)
    _stage = sleep_stage(quiet_s) if _sleep else "awake"
    if _sleep:
        # 深睡最难叫（要多喊两声），REM 被叫醒是「懵」不是「慢」
        lag += {"sws": 2.4, "light": 1.8, "rem": 2.0}.get(_stage, 1.8)

    return {
        "circadian": round(c, 3),
        "band": band_name(c, dt),
        "arousal": round(a, 3),
        "user_d": round(float(user_d or 0.0), 3),
        "hr": int(round(hr)),
        "rr": round(rr, 1),
        "lag": round(_clamp(lag, 0.4, 5.0), 2),
        "sleeping": _sleep,
        "stage": _stage,
    }


_BAND_HINT = {
    "凌晨": "现在是凌晨，你困得厉害，脑子转得慢，懒得说长句子，反应也慢半拍。",
    "清晨": "刚醒没多久，还有点迷糊，但慢慢清醒过来了。",
    "上午": "上午，最清醒的时候，脑子清楚、愿意多说两句。",
    "中午": "中午，有点困，但还能撑着聊。",
    "下午": "下午，状态平平，不特别兴奋也不困。",
    "晚上": "晚上，身体最舒服的时候，也是最想跟他多聊会儿的时候。",
    "深夜": "深夜了，开始撑不住，说话会变短，也可能走神。",
}


def hint(v: dict[str, Any]) -> str:
    """拼进 system 的【你现在的身体状态】，一条就够，别堆。"""
    if v.get("sleeping"):
        _st = v.get("stage") or "light"
        if _st == "sws":
            return ("你正睡得最沉的时候被叫醒，脑子一片空白，只会含含糊糊应一声，"
                    "话说不完整，说完又想睡回去。")
        if _st == "rem":
            return ("你刚在做梦就被叫醒了，脑子还停在梦里的画面上，说出来的话前后不搭，"
                    "过一会儿才反应过来自己醒了。")
        return ("你睡得浅，被叫醒能勉强应一句，但整个人还是困的，"
                "声音软、句子短，说着说着又想睡。")
    band = v.get("band") or "下午"
    base = _BAND_HINT.get(band, _BAND_HINT["下午"])
    extra = []
    if (v.get("arousal") or 0) >= 0.7:
        extra.append("心跳有点快")
    if (v.get("user_d") or 0) >= 0.22:
        extra.append("他刚才说得挺急，你也被带着有点提起来")
    elif (v.get("arousal") or 1) <= 0.25:
        extra.append("很安静，呼吸慢")
    tail = ("，" + "，".join(extra) + "。") if extra else ""
    return base.rstrip("。") + tail


def stats() -> dict[str, Any]:
    v = vitals()
    return {"vitals": v, "hint": hint(v)}
