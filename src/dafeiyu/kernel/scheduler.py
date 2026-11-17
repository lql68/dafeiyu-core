"""统一后台任务调度：预算 / 去重 / 退避 / 持久化。

只做「该不该现在跑」的判断，不接管线程结构 —— 各 loop 仍然自己 sleep，
只是把「无脑跑」换成「先问一句」。

状态文件 `scheduler_state.json`（原子写），重启后能接着算：
  - `min_gap`：同一个任务两次之间至少隔多久
  - `max_per_day`：当天最多跑几次（0 = 不限）
  - `fails`：连续失败次数，指数退避（60s → 120s → 240s … 封顶 1 小时）
  - `paused`：全局暂停开关

设计取舍：
  - 不引依赖，不建库，一个 JSON 文件
  - 多进程各管各的任务名（8770 和 7902 的任务名不重叠），避免文件竞态
  - 失败退避只在**连续**失败时累加，一次成功就清零
"""

from __future__ import annotations

import json
import os
import time

STATE_NAME = "scheduler_state.json"

BACKOFF_BASE = 60.0
BACKOFF_CAP = 3600.0

# 已知任务名（写进文档，避免拼错）
TASKS = (
    "world_walk",       # 她自己走动
    "world_health",     # 断线/恢复感知
    "reading",          # 她自己翻书
    "heartbeat",        # 想事情
    "school",           # 技能沉淀
    "lymph",            # 类淋巴（睡时维护）
    "observe",          # 感知留痕
    "proactive",        # 主动开口
    "attention",        # 注意力
)


class Scheduler:
    def __init__(self, root: str):
        self.path = os.path.join(root, STATE_NAME)
        self._s = self._load()

    # ---------------------------------------------------------------- 状态
    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("tasks"), dict):
                return d
        except Exception:
            pass
        return {"tasks": {}, "paused": False, "updated_at": 0.0}

    def _save(self) -> None:
        self._s["updated_at"] = time.time()
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._s, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            print("[sched] 保存失败: %r" % (e,), flush=True)

    def _t(self, name: str) -> dict:
        tasks = self._s.setdefault("tasks", {})
        st = tasks.get(name)
        if not isinstance(st, dict):
            st = {}
            tasks[name] = st
        return st

    def _roll_day(self, st: dict) -> None:
        today = time.strftime("%Y-%m-%d")
        if st.get("day") != today:
            st["day"] = today
            st["day_n"] = 0

    # ---------------------------------------------------------------- 开关
    def paused(self) -> bool:
        return bool(self._s.get("paused"))

    def pause(self) -> None:
        self._s["paused"] = True
        self._save()

    def resume(self) -> None:
        self._s["paused"] = False
        self._save()

    # ---------------------------------------------------------------- 核心
    def allow(
        self,
        name: str,
        *,
        min_gap: float = 0.0,
        max_per_day: int = 0,
        now: float | None = None,
    ) -> bool:
        """现在能不能跑这个任务。不修改状态（要配合 done()）。"""
        if self.paused():
            return False
        now = time.time() if now is None else now
        st = self._t(name)
        self._roll_day(st)

        if float(st.get("backoff_until") or 0.0) > now:
            return False
        last = float(st.get("last_at") or 0.0)
        if min_gap > 0 and last and (now - last) < min_gap:
            return False
        if max_per_day > 0 and int(st.get("day_n") or 0) >= max_per_day:
            return False
        return True

    def done(
        self,
        name: str,
        *,
        ok: bool = True,
        note: str = "",
        now: float | None = None,
    ) -> dict:
        """记录一次执行结果。ok=False 会累加退避。"""
        now = time.time() if now is None else now
        st = self._t(name)
        self._roll_day(st)
        st["last_at"] = now
        st["last_ok"] = bool(ok)
        st["day_n"] = int(st.get("day_n") or 0) + 1
        if note:
            st["note"] = str(note)[:200]

        if ok:
            st["fails"] = 0
            st["backoff_until"] = 0.0
            st.pop("last_err", None)
        else:
            fails = int(st.get("fails") or 0) + 1
            st["fails"] = fails
            wait = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** (fails - 1)))
            st["backoff_until"] = now + wait
            st["last_err"] = str(note)[:200]
        self._save()
        return st

    # ---------------------------------------------------------------- 观测
    def stats(self) -> dict:
        now = time.time()
        out = {}
        for name in self._s.get("tasks", {}):
            st = self._s["tasks"][name]
            out[name] = {
                "last_at": st.get("last_at") or 0,
                "ago": round(now - float(st.get("last_at") or 0), 0) if st.get("last_at") else None,
                "ok": st.get("last_ok"),
                "fails": int(st.get("fails") or 0),
                "day_n": int(st.get("day_n") or 0),
                "backoff_left": max(0.0, round(float(st.get("backoff_until") or 0) - now, 0)),
                "note": st.get("note", ""),
                "err": st.get("last_err", ""),
            }
        return {"paused": self.paused(), "tasks": out}


def hook(loop_fn, sched, name, **kw):
    """把「无脑执行」包成「先问调度器」。

    用法（在 loop 里）：
        result = run(do_work, sched, "world_walk", min_gap=1500, max_per_day=40)
        if result is None:
            continue          # 这次跳过
    """

    def run(fn):
        if not sched.allow(name, min_gap=kw.get("min_gap", 0), max_per_day=kw.get("max_per_day", 0)):
            return None
        try:
            r = fn()
            sched.done(name, ok=True, note=str(r)[:120] if r else "")
            return r
        except Exception as e:  # noqa: BLE001
            sched.done(name, ok=False, note=repr(e))
            raise

    return run
