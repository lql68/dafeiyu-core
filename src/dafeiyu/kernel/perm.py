"""权限总闸 —— 一个文件决定她此刻能伸多长的手。

safe（默认）
    explore / interact 开
    execute   只跑只读白名单命令（dir / git status / pip list …）
    self_modify 禁

full
    全部放开：命令不限、可写可删可联网、能改自己的代码和权限

切换立刻生效，**不用重启**（这里每次读盘，带 2 秒缓存）。

用法（插件目录下）：
    python perm.py               看当前
    python perm.py full          全开
    python perm.py safe          收回
    python perm.py full "理由"   全开并记一笔

也可以直接调 7902（只绑 127.0.0.1，只有本机能碰）：
    GET /perm?mode=full
    GET /perm
"""
from __future__ import annotations

import json
import os
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "perm_mode.json")

_CACHE = {"at": 0.0, "mode": "safe"}


def _read() -> str:
    now = time.time()
    if now - _CACHE["at"] < 2.0:
        return _CACHE["mode"]
    mode = "safe"
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            mode = str((json.load(f) or {}).get("mode") or "safe").lower()
    except Exception:
        mode = "safe"
    if mode not in ("safe", "full"):
        mode = "safe"
    _CACHE["at"] = now
    _CACHE["mode"] = mode
    return mode


def is_full() -> bool:
    return _read() == "full"


def mode() -> str:
    return _read()


def set_mode(m: str, note: str = "") -> dict:
    m = (m or "").strip().lower()
    if m not in ("safe", "full"):
        return {"ok": False, "err": "mode 只能是 safe / full"}
    old = {}
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            old = json.load(f) or {}
    except Exception:
        old = {}
    data = {"mode": m, "at": time.time(), "note": note[:200],
            "history": (old.get("history") or [])[-19:] + [{
                "from": old.get("mode") or "safe", "to": m,
                "at": time.time(), "note": note[:200]}]}
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _PATH)
    _CACHE["at"] = 0.0
    _CACHE["mode"] = m
    return {"ok": True, "mode": m, "from": old.get("mode") or "safe"}


def describe() -> dict:
    m = _read()
    if m == "full":
        return {"mode": "full",
                "can": "命令不限、可写可删可联网、可改自己的代码和权限",
                "note": "她现在能做的和你能做的一样多"}
    return {"mode": "safe",
            "can": "翻记忆 / 搜网页 / 读文件 / 写日记 / 主动发消息（每天 2 条）"
                   " / 只读命令（每天 4 条）",
            "note": "execute 只走白名单，self_modify 禁"}


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if not args:
        print(json.dumps(describe(), ensure_ascii=False, indent=2))
    elif args[0] in ("safe", "full"):
        note = args[1] if len(args) > 1 else ""
        print(json.dumps(set_mode(args[0], note), ensure_ascii=False))
        print(json.dumps(describe(), ensure_ascii=False, indent=2))
    else:
        print("用法: python perm.py [safe|full] [理由]")
