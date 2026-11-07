# -*- coding: utf-8 -*-
"""方案 8 · 学校：课程式技能生成（接方案 5 的 procedures 表）

小凌的「学校」是**排课**（有教学目标、有教材、学完才放风），
我们的 memory_consolidation 是**整理**（把今天的话压成摘要）。
这里做的是前者：拿她自己的记忆当教材，每节课产出一条「以后该怎么做」。

三条硬约束（否则会退化成"用 LLM 生成一堆正确的废话"）：
1. 必须有 evidence —— 每条都要指向具体 document id，没有出处的直接丢
2. cap 每天 3–5 节 —— 181 条 document 排不出 100 节有料可讲的课
3. 不覆盖只叠加 —— 同名同 session 时抬 mastery，旧的降权而不是删
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

# 每节课：标题 / 教材 SQL / 教学目标（写进 prompt，约束产出方向）
COURSES: list[dict[str, Any]] = [
    {
        "title": "深夜回来时怎么开口",
        "goal": "他深夜回来时，先给台阶还是先追问？写成具体的第一步该说什么。",
        "sql": (
            "SELECT id, text FROM documents "
            "WHERE (CAST(strftime('%H', created_at) AS INT) >= 22 "
            "   OR CAST(strftime('%H', created_at) AS INT) <= 3) "
            "ORDER BY created_at DESC LIMIT 4"
        ),
    },
    {
        "title": "他在意的事（高权重）",
        "goal": "从他反复提起、权重最高的事里，总结一条「怎么接他的话」。",
        "sql": (
            "SELECT id, text FROM documents "
            "ORDER BY CAST(json_extract(metadata, '$.importance') AS REAL) DESC, "
            "created_at DESC LIMIT 4"
        ),
    },
    {
        "title": "别人在场时怎么说话",
        "goal": "群聊里有别人时，她的分寸该怎么拿？写成能照做的规则。",
        "sql": (
            "SELECT id, text FROM documents "
            "WHERE metadata LIKE '%GroupMessage%' "
            "ORDER BY created_at DESC LIMIT 4"
        ),
    },
    {
        "title": "他情绪不好那次",
        "goal": "挑出他语气不对的时刻，总结「不该说什么」和「该先做什么」。",
        "sql": (
            "SELECT id, text FROM documents "
            "WHERE (text LIKE '%累%' OR text LIKE '%烦%' OR text LIKE '%难受%' "
            "   OR text LIKE '%撑不住%' OR text LIKE '%压力%') "
            "ORDER BY created_at DESC LIMIT 4"
        ),
    },
    {
        "title": "最近这几天在忙什么",
        "goal": "从最近的事里，总结一条「主动关心该怎么问」的具体说法。",
        "sql": (
            "SELECT id, text FROM documents "
            "ORDER BY created_at DESC LIMIT 4"
        ),
    },
]

def _atomic_json(path, obj) -> bool:
    """原子写 JSON：.tmp -> os.replace，并留一份 .bak。"""
    tmp = str(path) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        try:
            if os.path.exists(path):
                with open(path, "rb") as src, open(str(path) + ".bak", "wb") as dst:
                    dst.write(src.read())
        except Exception:
            pass
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


SYS_PROMPT = """你是「大肥鱼」的学习委员，负责整理她自己的经验。

下面是从她的记忆里挑出来的几件真实发生过的事（带 id）。
请从中总结**一条以后该怎么做**的经验 —— 不要复述事情本身，不要写感悟。

硬要求：
- name：不超过 12 个字，像个小标题
- trigger：2-4 个关键词，用逗号分隔（他会在什么情况下用得上）
- body：20-60 字，必须写成**可执行的步骤**（先做什么、再做什么、别做什么），
  不要出现"要多关心他""要更用心"这种正确但没用的话
- evidence：这一段真正引用到的事的 id 列表（只能从上面给的 id 里选，不能编）

只输出一个 JSON 对象，不要任何解释、不要 markdown 围栏：
{"name": "...", "trigger": "...", "body": "...", "evidence": [182, 191]}"""


def _strip_fence(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse_json(s: str) -> dict | None:
    s = _strip_fence(s)
    try:
        j = json.loads(s)
        return j if isinstance(j, dict) else None
    except Exception:
        pass
    # 兜底：抠第一个 {...}
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            j = json.loads(m.group(0))
            return j if isinstance(j, dict) else None
        except Exception:
            pass
    return None


class School:
    """一天几节课，教材是 her 自己的记忆。"""

    def __init__(self, store, db_path: str, state_dir: str = ""):
        self.store = store              # ProcedureStore 实例
        self.db_path = db_path
        self.state_path = os.path.join(
            state_dir or os.path.dirname(os.path.abspath(__file__)), "school_state.json")

    # ---------------- 每日配额 ----------------
    def _state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _save_state(self, st: dict) -> None:
        _atomic_json(self.state_path, st)

    def _todays_count(self) -> int:
        st = self._state()
        today = time.strftime("%Y-%m-%d")
        return int(st.get(today, 0)) if isinstance(st.get(today), int) else 0

    def _bump(self, n: int = 1) -> None:
        st = self._state()
        today = time.strftime("%Y-%m-%d")
        st[today] = self._todays_count() + n
        # 只留最近 7 天
        keep = sorted(st.keys())[-7:]
        self._save_state({k: st[k] for k in keep})

    # ---------------- 跑课 ----------------
    async def run(
        self,
        provider,
        session_id: str = "",
        max_lessons: int = 4,
        daily_cap: int = 5,
    ) -> dict:
        """跑一轮课。返回 {ok, lessons, learned:[...], skipped:[...]}"""
        import aiosqlite  # noqa: F401  延迟导入：只有真要写技能表时才需要它

        already = self._todays_count()
        budget = max(0, daily_cap - already)
        if budget <= 0:
            return {"ok": True, "lessons": 0,
                    "note": f"今天的课已经上满 {already} 节（cap {daily_cap}）",
                    "learned": [], "skipped": []}

        want = min(max_lessons, budget)
        learned: list[dict] = []
        skipped: list[dict] = []

        picked = COURSES[:want]
        for course in picked:
            # 1) 取教材
            try:
                async with aiosqlite.connect(self.db_path, timeout=15) as db:
                    await db.execute("PRAGMA temp_store = MEMORY")
                    cur = await db.execute(course["sql"])
                    rows = await cur.fetchall()
            except Exception as e:
                skipped.append({"title": course["title"], "why": f"取教材失败: {e}"})
                continue

            if not rows:
                skipped.append({"title": course["title"], "why": "没素材"})
                continue

            ids = [int(r[0]) for r in rows]
            material = "\n".join(
                f"[id={r[0]}] {(r[1] or '').strip()[:160]}" for r in rows)

            # 2) 一节课一次 LLM
            user = f"教材（{course['title']}）：\n{material}\n\n教学目标：{course['goal']}"
            try:
                resp = await provider.text_chat(
                    prompt=user, contexts=[], system_prompt=SYS_PROMPT)
                raw = (getattr(resp, "completion_text", "") or "").strip()
            except Exception as e:
                skipped.append({"title": course["title"], "why": f"LLM 失败: {e}"})
                continue

            j = _parse_json(raw)
            if not j:
                skipped.append({"title": course["title"], "why": "输出不是 JSON"})
                continue

            name = str(j.get("name", "")).strip()[:24]
            trig = str(j.get("trigger", "")).strip()[:60]
            body = str(j.get("body", "")).strip()[:200]
            ev = j.get("evidence") or []

            # 3) 硬约束：必须有真实出处
            ev_ids = []
            for x in ev if isinstance(ev, list) else []:
                try:
                    xi = int(x)
                except Exception:
                    continue
                if xi in ids:
                    ev_ids.append(xi)
            if not (name and body and ev_ids):
                skipped.append({"title": course["title"],
                                "why": f"缺出处或字段不全（evidence={ev}）"})
                continue

            # 4) 写进技能表（mastery 0.3：学过了但还没验证过）
            ev_text = "doc#" + ",doc#".join(str(i) for i in ev_ids)
            try:
                pid = await self.store.add(
                    name, trig, body, session_id=session_id,
                    mastery=0.3, evidence=ev_text)
            except Exception as e:
                skipped.append({"title": course["title"], "why": f"入库失败: {e}"})
                continue

            if pid:
                learned.append({"id": pid, "course": course["title"],
                                "name": name, "trigger": trig,
                                "body": body, "evidence": ev_text})
                self._bump(1)

        return {"ok": True, "lessons": len(learned),
                "today": self._todays_count(), "cap": daily_cap,
                "learned": learned, "skipped": skipped}
