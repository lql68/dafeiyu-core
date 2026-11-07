"""P1 自我模型（信念驱动版）。

设计要点：不是「每天定时拍一张」，而是**信念变了才重算**。
- belief_fp = 当前 active 信念的 (id, 置信度两位小数) 排序后的 sha1 前 12 位
- 指纹没变 → 不动；变了且距上次 >= 30 分钟 → 出一版新的
- 每版都记 diff：这次信念比上次多了什么、掉了什么、谁变强了
"""

import hashlib
import json
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS self_model (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     INTEGER NOT NULL,
    payload     TEXT    NOT NULL,
    belief_fp   TEXT    NOT NULL,
    trigger     TEXT,
    reason      TEXT,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_self_model_at ON self_model(created_at DESC);
"""

MIN_REBUILD_GAP = 1800.0      # 两次重建至少隔 30 分钟，防抖
SELF_LIMIT = 600             # 两版之间最短 10 分钟（同一次心跳里别连建）


def _conn(path):
    c = sqlite3.connect(path, timeout=20)
    return c

def _fp(pairs):
    raw = "|".join("%s=%.2f" % (i, c) for i, c in pairs)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def install(story, gaps_provider=None, meta_provider=None):
    """把 self_model 能力挂到 SelfStory 实例上（不继承，直接补方法）。"""
    with _conn(story.db_path) as c:          # noqa: SLF001
        c.executescript(_SCHEMA)

    def belief_fp() -> str:
        rows = story.list_beliefs(status="active", limit=200)
        return _fp([(r["id"], float(r.get("confidence") or 0)) for r in rows])

    def latest() -> dict:
        with _conn(story.db_path) as c:      # noqa: SLF001
            c.row_factory = None
            r = c.execute(
                "SELECT id, version, payload, belief_fp, trigger, reason, created_at "
                "FROM self_model ORDER BY id DESC LIMIT 1").fetchone()
        if not r:
            return {}
        try:
            p = json.loads(r[2])
        except Exception:
            p = {}
        p.update({"id": r[0], "version": r[1], "belief_fp": r[3],
                  "trigger": r[4], "reason": r[5], "created_at": r[6]})
        return p

    def history(n: int = 6) -> list:
        with _conn(story.db_path) as c:      # noqa: SLF001
            rs = c.execute(
                "SELECT id, version, payload, reason, created_at "
                "FROM self_model ORDER BY id DESC LIMIT ?", (int(n),)).fetchall()
        out = []
        for r in rs:
            try:
                p = json.loads(r[2])
            except Exception:
                p = {}
            p.update({"id": r[0], "version": r[1], "reason": r[3], "created_at": r[4]})
            out.append(p)
        return out

    def compose(trigger: str, reason: str) -> dict:
        """纯结构化聚合，不调 LLM。"""
        bel = story.list_beliefs(status="active", limit=200)
        bel.sort(key=lambda x: -(x.get("confidence") or 0))
        bs = story.belief_stats()
        prev = latest()

        value = [{"id": b["id"], "belief": b["belief"],
                  "confidence": round(float(b.get("confidence") or 0), 3)}
                 for b in bel[:5]]
        limit = []
        if gaps_provider:
            try:
                limit = [g for g in (gaps_provider() or []) if g][:3]
            except Exception:
                limit = []

        # 变化：跟上一版比
        prev_value = {v["id"]: v for v in (prev.get("value") or [])}
        cur_value = {v["id"]: v for v in value}
        added = [cur_value[i]["belief"] for i in cur_value if i not in prev_value]
        dropped = [prev_value[i]["belief"] for i in prev_value if i not in cur_value]
        moved = []
        for i in cur_value:
            if i in prev_value:
                d = cur_value[i]["confidence"] - prev_value[i]["confidence"]
                if abs(d) >= 0.05:
                    moved.append({"belief": cur_value[i]["belief"],
                                  "from": round(prev_value[i]["confidence"], 3),
                                  "to": cur_value[i]["confidence"]})

        payload = {
            "value": value,
            "limit": limit,
            "belief_stats": bs,
            "diff": {"added": added, "dropped": dropped, "moved": moved},
            "since": prev.get("created_at") or 0,
            "fp_prev": prev.get("belief_fp") or "",
        }
        if meta_provider:
            try:
                payload.update(meta_provider() or {})
            except Exception:
                pass

        # 一句话概述（给人看的，也是给小肥鱼自己看的）
        bits = []
        if added:
            bits.append("多了%d条想法" % len(added))
        if dropped:
            bits.append("%d条不在最前面了" % len(dropped))
        if moved:
            up = [m for m in moved if m["to"] > m["from"]]
            down = [m for m in moved if m["to"] < m["from"]]
            if up:
                bits.append("%d条更确定了" % len(up))
            if down:
                bits.append("%d条动摇了" % len(down))
        if not bits:
            bits.append("想法没大变化")
        payload["summary"] = "；".join(bits)

        ver = int(prev.get("version") or 0) + 1
        with _conn(story.db_path) as c:      # noqa: SLF001
            c.execute(
                "INSERT INTO self_model (version, payload, belief_fp, trigger, reason, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (ver, json.dumps(payload, ensure_ascii=False),
                 belief_fp(), trigger, reason, time.time()))
        payload["version"] = ver
        return payload

    def maybe_rebuild(trigger: str = "loop", force: bool = False) -> dict:
        fp = belief_fp()
        prev = latest()
        if not prev:
            return {"ok": True, "built": True, **compose("first", "第一次建立")}
        if not force and prev.get("belief_fp") == fp:
            return {"ok": True, "built": False, "skipped": "信念没变", "version": prev["version"]}
        gap = time.time() - float(prev.get("created_at") or 0)
        if not force and gap < MIN_REBUILD_GAP:
            return {"ok": True, "built": False,
                    "skipped": "距上一版 %d 秒，太近" % int(gap), "version": prev["version"]}
        reason = "信念变了：%d → %d 条" % (
            len(prev.get("value") or []), len(story.list_beliefs("active", 200)))
        return {"ok": True, "built": True, **compose(trigger, reason)}

    story.belief_fp = belief_fp
    story.latest_self = latest
    story.self_history = history
    story.rebuild_self = maybe_rebuild
    return story
