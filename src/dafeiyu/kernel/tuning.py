"""她自己能调的限额。

只有这几个键能动，每个都有上下限 —— 不是让她把闸拆了，是让她决定
「今天我打算想几次、说几句、跑几条命令」。

放在 tuning.json，`autonomy.limits()` 每次合并，改完立刻生效。
"""
from __future__ import annotations

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "tuning.json")

# key -> (下限, 上限, 类型, 说明)
KEYS = {
    "max_new_per_day": (1, 10, int, "一天最多提几个新念头"),
    "max_llm_per_day": (5, 200, int, "一天最多调几次模型来想事"),
    "max_send_per_day": (0, 20, int, "一天最多主动找他几次"),
    "max_shell_per_day": (0, 50, int, "一天最多跑几条命令"),
    "max_selfedit_per_day": (0, 10, int, "一天最多改几次自己的代码"),
    "day_interval_s": (300, 7200, int, "白天多久想一次（秒）"),
    "night_interval_s": (600, 21600, int, "夜里多久干一次重活（秒）"),
}

_CACHE = {"at": 0.0, "v": {}}


def load() -> dict:
    import time
    now = time.time()
    if now - _CACHE["at"] < 2.0 and _CACHE["v"]:
        return dict(_CACHE["v"])
    out: dict = {}
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
        for k, v in (raw.get("values") or {}).items():
            if k in KEYS:
                lo, hi, typ, _ = KEYS[k]
                try:
                    out[k] = max(lo, min(hi, typ(v)))
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        out = {}
    _CACHE["at"] = now
    _CACHE["v"] = out
    return dict(out)


def set_one(key: str, value) -> dict:
    if key not in KEYS:
        return {"ok": False, "err": "不能调这个：" + key,
                "can": sorted(KEYS)}
    lo, hi, typ, desc = KEYS[key]
    try:
        v = typ(value)
    except Exception:  # noqa: BLE001
        return {"ok": False, "err": "值要是整数"}
    clipped = max(lo, min(hi, v))
    cur = load()
    old = cur.get(key)
    cur[key] = clipped
    try:
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"values": cur}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PATH)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": str(e)[:200]}
    _CACHE["at"] = 0.0
    r = {"ok": True, "key": key, "value": clipped, "old": old,
         "clamped": (clipped != v), "range": [lo, hi], "desc": desc}
    if clipped != v:
        r["note"] = "超出范围，按 %d~%d 收了" % (lo, hi)
    return r


def describe(defaults: dict | None = None) -> dict:
    """看当前限额。没被改过的键，把默认值也带上（否则显示 null 看不出意思）。"""
    cur = load()
    dflt = dict(defaults or {})
    out = {}
    for k, (lo, hi, _t, desc) in KEYS.items():
        now = cur.get(k)
        d = dflt.get(k)
        out[k] = {
            "now": now if now is not None else d,
            "default": d,
            "changed": now is not None,
            "range": [lo, hi],
            "desc": desc,
        }
    return out
