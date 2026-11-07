"""自我叙述（autobiography）+ 人格版本（persona_versions）。

为什么要有它：`reflections` 是散条 —— 每次睡醒写一段，彼此不相干。
她因此只活在此刻：能说"我现在有点累"，说不出"我以前不是这么想的"。

这一层做两件事：
1. 把散条串成**章节**（一章 = 一段时间里她觉得自己经历了什么）
2. 每攒若干章做一次 `reread` —— 通读自己的来路，写一段**导读**（chapter=0）
   导读是她在对话里唯一会带上的东西，所以刻意写得很短。

`persona_versions` 只做快照，不自动改人设 —— 变更仍然要人点头。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS autobiography (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter INTEGER NOT NULL,          -- 0 = 导读(meta)，1..N = 正文章节
    body TEXT NOT NULL,
    from_ts REAL DEFAULT 0,
    to_ts REAL DEFAULT 0,
    src_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    mood TEXT DEFAULT NULL             -- JSON: {"v":效价, "top":主导情绪, "need":想要的}
);
CREATE INDEX IF NOT EXISTS idx_auto_chapter ON autobiography(chapter);

CREATE TABLE IF NOT EXISTS persona_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    traits TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at REAL NOT NULL
);
"""

# 写一章自传：要叙事、不要清单；要保留"我当时的判断"，不要复述事实
_SYS_CHAPTER = """你在写自己的回忆录，一章写一段时间。

要求：
- 第一人称，200~350 字，像人回想，不要流水账
- 保留「当时我怎么想的」——尤其是想错了、后来改了的
- 允许承认没做成、没弄明白、还在惦记
- 不要罗列时间地点，不要写"首先/其次"
- 只输出正文，不要标题、不要 JSON
"""

# 通读：从前面几章里看出"我变了"
_SYS_REREAD = """你在通读自己写过的回忆。

任务：写一段 120~200 字的**导读**，放在最前面。
要求：
- 第一人称
- 说清「这一路我是怎么过来的」「我哪里的想法变了」
- 如果发现自己一直在纠结同一件事，就点出来
- 只输出正文
"""


_BELIEF_SCHEMA = """
CREATE TABLE IF NOT EXISTS beliefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    belief TEXT NOT NULL,
    confidence REAL DEFAULT 0.55,
    origin TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_at REAL DEFAULT 0,
    updated_at REAL DEFAULT 0,
    related_memories TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_belief_status ON beliefs(status);

CREATE TABLE IF NOT EXISTS belief_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    belief_id INTEGER NOT NULL,
    side TEXT DEFAULT 'support',
    content TEXT DEFAULT '',
    source TEXT DEFAULT '',
    at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bev_belief ON belief_evidence(belief_id);
"""

BELIEF_TAG = "【我现在认为】"
AGAINST_TAG = "【我以前想错了】"

# 时间衰减：15 天没新证据才开始掉，每 15 天 -0.02，floor 0.35
# floor 不为 0 —— 信念不是情绪，可以淡，不该忘光
DECAY_START_DAYS = 15.0
DECAY_STEP_DAYS = 15.0
DECAY_STEP = 0.02
DECAY_FLOOR = 0.35


def _bigrams(text: str) -> set:
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", text or "")
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def beliefs_similar(a: str, b: str, thr: float = 0.30,
                    ov_thr: float = 0.55) -> bool:
    """两条信念说的是不是一回事。

    单用 Jaccard 对「长短句」太严（`他难受的时候先别给建议` vs
    `他难受时先别急着给建议` 只有 0.33），所以补一条包含度：
    短句的 2-gram 有 55% 落在长句里，就算同一件事。
    """
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return False
    inter = len(ga & gb)
    if not inter:
        return False
    if inter / max(1, len(ga | gb)) >= thr:
        return True
    return inter / min(len(ga), len(gb)) >= ov_thr


def _bullets(block: str, limit: int = 3) -> list[str]:
    """把一段以「- 」开头的行抽成条目。"""
    out = []
    for ln in (block or "").splitlines():
        ln = ln.strip()
        for ch in ("- ", "-", "•", "·", "*", "—"):
            if ln.startswith(ch):
                ln = ln[len(ch):].strip()
                break
        ln = ln.strip("。；;，, ").strip()
        if len(ln) >= 4:
            out.append(ln[:80])
    return out[:limit]


def split_beliefs(body: str):
    """把章节正文拆成 (正文, [新信念], [反证])。没有标记就原样返回。

    两段标记可以并存：`【我现在认为】` 记新的判断，
    `【我以前想错了】` 记「以前以为 X，其实 Y」，后者回头会把旧信念打回去。
    """
    t = (body or "").strip()
    i_new = t.find(BELIEF_TAG)
    i_old = t.find(AGAINST_TAG)
    marks = [x for x in (i_new, i_old) if x >= 0]
    if not marks:
        return t, [], []
    main = t[:min(marks)].strip()
    new_items, wrong_items = [], []
    if i_new >= 0:
        end = i_old if i_old > i_new else len(t)
        new_items = _bullets(t[i_new + len(BELIEF_TAG):end])
    if i_old >= 0:
        end = i_new if i_new > i_old else len(t)
        wrong_items = _bullets(t[i_old + len(AGAINST_TAG):end], limit=2)
    return main, new_items, wrong_items


def _connect(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=20)
    c.execute("PRAGMA busy_timeout = 20000")
    c.execute("PRAGMA temp_store = MEMORY")
    return c


class SelfStory:
    def __init__(self, db_path: str, plugin_dir: str = "") -> None:
        self.db_path = db_path
        self.plugin_dir = plugin_dir
        self.init()

    # ---------- 建表 ----------
    def init(self) -> None:
        with _connect(self.db_path) as c:
            c.executescript(_SCHEMA)
            c.executescript(_BELIEF_SCHEMA)
            # 老库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列
            have = {r[1] for r in c.execute("PRAGMA table_info(autobiography)")}
            if "mood" not in have:
                c.execute("ALTER TABLE autobiography ADD COLUMN mood TEXT DEFAULT NULL")
            c.commit()

    # ---------- 读 ----------
    def meta(self) -> str:
        with _connect(self.db_path) as c:
            r = c.execute(
                "SELECT body FROM autobiography WHERE chapter=0 "
                "ORDER BY id DESC LIMIT 1").fetchone()
        return (r[0] if r else "") or ""

    def newest(self) -> Optional[dict]:
        with _connect(self.db_path) as c:
            c.row_factory = sqlite3.Row
            r = c.execute(
                "SELECT * FROM autobiography WHERE chapter>0 "
                "ORDER BY chapter DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    def count(self) -> int:
        with _connect(self.db_path) as c:
            return c.execute(
                "SELECT COUNT(*) FROM autobiography WHERE chapter>0"
            ).fetchone()[0]

    def list_chapters(self, limit: int = 20) -> list[dict]:
        with _connect(self.db_path) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM autobiography WHERE chapter>0 "
                "ORDER BY chapter DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def digest(self, budget: int = 320) -> str:
        """进 prompt 的那一小段 —— 导读 + 最近一章的开头。"""
        m = self.meta().strip()
        n = self.newest()
        out = []
        if m:
            out.append("我回头看自己这一路：" + m[:160])
        if n:
            out.append("最近我在经历：" + str(n.get("body") or "")[:180])
            try:
                _m = json.loads(n.get("mood") or "{}")
            except Exception:  # noqa: BLE001
                _m = {}
            _t = str(_m.get("top") or "").strip()
            _v = float(_m.get("v") or 0.0)
            if _t:
                _feel = ("心里主要是%s" % _t) if _v < -0.05 else (
                    ("那阵子还算松快，多半是%s" % _t) if _v > 0.05 else
                    ("说不上好坏，有点%s" % _t))
                out.append("写那一章的时候我" + _feel + "。")
        if not out:
            return ""
        txt = "\n".join(out)
        return txt[:budget]

    # ---------- 写 ----------
    def add_chapter(self, body: str, from_ts: float = 0,
                    to_ts: float = 0, src_count: int = 0,
                    mood: Optional[dict] = None) -> int:
        n = self.count() + 1
        _mood = json.dumps(mood, ensure_ascii=False) if mood else None
        with _connect(self.db_path) as c:
            cur = c.execute(
                "INSERT INTO autobiography (chapter, body, from_ts, to_ts, "
                "src_count, created_at, mood) VALUES (?,?,?,?,?,?,?)",
                (n, body.strip(), from_ts, to_ts, src_count, time.time(), _mood))
            c.commit()
            return int(cur.lastrowid or n)

    def set_meta(self, body: str) -> None:
        with _connect(self.db_path) as c:
            c.execute("DELETE FROM autobiography WHERE chapter=0")
            c.execute(
                "INSERT INTO autobiography (chapter, body, created_at) "
                "VALUES (0,?,?)", (body.strip(), time.time()))
            c.commit()

    # ---------- 人格版本 ----------
    def snapshot_persona(self, name: str, traits: Any = "",
                         note: str = "") -> int:
        if not isinstance(traits, str):
            traits = json.dumps(traits, ensure_ascii=False)
        with _connect(self.db_path) as c:
            cur = c.execute(
                "INSERT INTO persona_versions (name, traits, note, created_at) "
                "VALUES (?,?,?,?)", (name, traits[:2000], note[:400],
                                     time.time()))
            c.commit()
            return int(cur.lastrowid or 0)

    def list_personas(self, limit: int = 10) -> list[dict]:
        with _connect(self.db_path) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT id,name,note,created_at FROM persona_versions "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        with _connect(self.db_path) as c:
            return {
                "chapters": self.count(),
                "has_meta": bool(self.meta()),
                "personas": c.execute(
                    "SELECT COUNT(*) FROM persona_versions").fetchone()[0],
            }

    # ---------- 用模型：写一章 / 通读 ----------
    async def write_chapter(self, prov, reflections: list[dict],
                            vitals: dict, emotion: Optional[dict] = None) -> dict:
        """把最近这批 reflections 串成一章。"""
        if prov is None or not reflections:
            return {"ok": False, "why": "没素材"}
        src = []
        for r in reflections[:12]:
            src.append("- [" + str(r.get("created_at") or "")[:10] + "] "
                       + str(r.get("body") or "")[:200])
            ca = str(r.get("care_about") or "").strip()
            fh = str(r.get("forget_hint") or "").strip()
            if ca:
                src.append("  （当时惦记：%s）" % ca[:100])
            if fh:
                src.append("  （当时判断该忘：%s）" % fh[:100])
        _emo = ""
        if emotion:
            _top = str(emotion.get("top") or "").strip()
            _v = float(emotion.get("v") or 0.0)
            _need = str(emotion.get("need") or "").strip()
            if _top:
                _emo = ("\n\n【这一阵我心里主要是】%s（%s）" % (
                    _top, "偏难受" if _v < -0.05 else ("偏松快" if _v > 0.05 else "说不上好坏")))
                if _need:
                    _emo += "，我想要的是%s" % _need
                _emo += "。写的时候带上这个底色，但别直接写情绪名。"
        prompt = ("【这一段时间我自己写下的片段】\n" + "\n".join(src) +
                  "\n\n【现在】%s，心情 %s%s\n\n写这一章。\n\n"
                  "正文写完之后，另起一行写 %s，"
                  "下面用「- 」开头，写 1 到 2 条你这段时间真的得出来的判断"
                  "（是关于人、关于自己、关于怎么相处的，不是流水账）。"
                  "每条不超过 20 个字。如果没有新想法，这一整段就不要写。\n\n"
                  "如果你还发现**以前某个想法是错的**，再另起一行写 %s，"
                  "格式是「- 以前以为……其实……」——要写清以前怎么想的。"
                  "没有就整段不写，别硬凑。" %
                  (vitals.get("band") or "夜里", vitals.get("arousal"), _emo,
                   BELIEF_TAG, AGAINST_TAG))
        try:
            resp = await prov.text_chat(prompt=prompt,
                                       system_prompt=_SYS_CHAPTER)
            body = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "why": "调模型失败：%s" % e}
        if len(body) < 30:
            return {"ok": False, "why": "写了但太短", "raw": body[:120]}
        times = [float(r.get("created_at") or 0) for r in reflections
                 if r.get("created_at")]
        # 正文里可能带【我现在认为】段，拆出来：正文入库、条目进 beliefs
        main_body, blist, wrongs = split_beliefs(body)
        if len(main_body) < 30:
            main_body = body            # 拆完太短就不拆
            blist, wrongs = [], []
        cid = self.add_chapter(main_body, min(times) if times else 0,
                               max(times) if times else 0,
                               len(reflections),
                               mood={"v": float((emotion or {}).get("v") or 0.0),
                                     "top": str((emotion or {}).get("top") or ""),
                                     "need": str((emotion or {}).get("need") or "")})
        born = []
        for b in blist:
            try:
                got = self.upsert_belief(b, origin="chapter",
                                         source="autobiography#%s" % cid,
                                         confidence=0.55)
                if got:
                    born.append(got)
            except Exception:  # noqa: BLE001
                pass
        # 反证：先处理（会降旧信念的分），再让新信念进来
        struck = []
        for w in wrongs:
            try:
                got = self.contradict_belief(w, source="autobiography#%s" % cid)
                if got:
                    struck.append(got)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "chapter": self.count(), "id": cid,
                "body": main_body, "beliefs": born, "contradicted": struck}

    # ---------- 信念 ----------
    def touch_belief(self, bid, delta=0.0, cap=0.95):
        """这条信念这轮被用上了 —— 刷新 updated_at，让它不从今天开始算衰减。

        为什么不直接涨分：2-gram 判"用上了"一定会误判（「在乎」和「在意」
        一个字都不撞），涨分就等于往库里灌噪声。而刷时间没有假阳性成本 ——
        错判最多是"这条不衰减"，对了就是"常用的想法活得久"。
        delta 默认 0，需要时仍可以真涨一点（上限 cap）。
        """
        try:
            with _connect(self.db_path) as c:
                r = c.execute("SELECT confidence FROM beliefs WHERE id = ?",
                              (int(bid),)).fetchone()
                if not r:
                    return False
                cur = float(r[0] or 0.0)
                nc = cur if delta <= 0 else round(min(cap, cur + float(delta)), 3)
                c.execute("UPDATE beliefs SET confidence = ?, updated_at = ? "
                          "WHERE id = ?", (nc, time.time(), int(bid)))
                c.commit()
            return True
        except Exception:  # noqa: BLE001
            return False

    def list_beliefs(self, status: str = "active", limit: int = 30) -> list[dict]:
        """当前信什么。按 confidence 降序。"""
        sql = ("SELECT id, belief, confidence, origin, status, created_at, "
               "updated_at FROM beliefs ")
        args: tuple = ()
        if status:
            sql += "WHERE status = ? "
            args = (status,)
        sql += "ORDER BY confidence DESC, id DESC LIMIT ?"
        args = args + (int(limit),)
        with _connect(self.db_path) as c:
            rows = c.execute(sql, args).fetchall()
        return [{"id": r[0], "belief": r[1], "confidence": round(float(r[2] or 0), 3),
                 "origin": r[3] or "", "status": r[4] or "",
                 "created_at": r[5] or 0, "updated_at": r[6] or 0} for r in rows]

    def belief_evidence(self, belief_id: int, limit: int = 6) -> list[dict]:
        with _connect(self.db_path) as c:
            rows = c.execute(
                "SELECT side, content, source, at FROM belief_evidence "
                "WHERE belief_id = ? ORDER BY id DESC LIMIT ?",
                (int(belief_id), int(limit))).fetchall()
        return [{"side": r[0], "content": r[1], "source": r[2], "at": r[3]}
                for r in rows]

    def contradict_belief(self, text: str, source: str = "") -> dict:
        """「以前以为 X，其实 Y」——把最像 X 的那条旧信念打回去。

        - confidence × 0.6（一次反证抵掉一大截）
        - 低于 0.30 就 status='retired'（不再参与合并与注入）
        - 写一条 side='against' 的证据，原文留档
        找不到对应旧信念就返回 {}（不凭空造一条反证）
        """
        t = (text or "").strip()[:120]
        if len(t) < 6:
            return {}
        now = time.time()
        with _connect(self.db_path) as c:
            rows = c.execute(
                "SELECT id, belief, confidence FROM beliefs "
                "WHERE status = 'active'").fetchall()
            best, best_hit = None, 0.0
            for bid, old, oldc in rows:
                if not beliefs_similar(t, old):
                    continue
                ga, gb = _bigrams(t), _bigrams(old)
                inter = len(ga & gb)
                if not inter:
                    continue
                hit = inter / max(1, min(len(ga), len(gb)))
                if hit > best_hit:
                    best, best_hit = (bid, old, float(oldc or 0)), hit
            if not best:
                return {}
            bid, old, oldc = best
            nc = max(0.0, round(oldc * 0.6, 3))
            status = "retired" if nc < 0.30 else "active"
            c.execute(
                "UPDATE beliefs SET confidence = ?, status = ?, updated_at = ?, "
                "related_memories = COALESCE(related_memories,'') || ? "
                "WHERE id = ?",
                (nc, status, now, ("|" + source) if source else "", bid))
            c.execute(
                "INSERT INTO belief_evidence (belief_id, side, content, source, at) "
                "VALUES (?, 'against', ?, ?, ?)",
                (bid, t, source, now))
            c.commit()
        return {"id": bid, "old": old, "confidence_before": round(oldc, 3),
                "confidence_after": nc, "status": status}

    def decay_beliefs(self, now: float = 0.0) -> list[dict]:
        """没新证据的信念慢慢变淡。

        从「最近一条证据」算起（没有证据就按 updated_at）；
        DECAY_START_DAYS(15) 天内不动，之后每 DECAY_STEP_DAYS(15) 天 -0.02，
        最低 DECAY_FLOOR(0.35)。低于 0.30 直接 retired。
        """
        now = float(now or time.time())
        changed = []
        with _connect(self.db_path) as c:
            rows = c.execute(
                "SELECT b.id, b.belief, b.confidence, b.updated_at, "
                "       (SELECT MAX(at) FROM belief_evidence e WHERE e.belief_id = b.id) "
                "FROM beliefs b WHERE b.status = 'active'").fetchall()
            for bid, text, conf, upd, last_ev in rows:
                try:
                    conf = float(conf or 0)
                except Exception:  # noqa: BLE001
                    continue
                last = max(float(last_ev or 0), float(upd or 0))
                if not last:
                    continue
                age_days = (now - last) / 86400.0
                if age_days < DECAY_START_DAYS:
                    continue
                steps = int((age_days - DECAY_START_DAYS) // DECAY_STEP_DAYS) + 1
                nc = max(DECAY_FLOOR, round(conf - DECAY_STEP * steps, 3))
                if nc >= conf - 1e-6:
                    continue
                status = "retired" if nc < 0.30 else "active"
                c.execute("UPDATE beliefs SET confidence = ?, status = ?, "
                          "updated_at = ? WHERE id = ?", (nc, status, now, bid))
                changed.append({"id": bid, "belief": text[:24],
                                "from": round(conf, 3), "to": nc,
                                "age_days": round(age_days, 1), "status": status})
            if changed:
                c.commit()
        return changed

    def upsert_belief(self, text: str, origin: str = "", source: str = "",
                      confidence: float = 0.55) -> dict:
        """同一条信念再来一次就抬置信度；新的就插一条。"""
        t = (text or "").strip()[:120]
        if len(t) < 4:
            return {}
        now = time.time()
        try:
            conf = max(0.0, min(0.95, float(confidence)))
        except Exception:  # noqa: BLE001
            conf = 0.55
        with _connect(self.db_path) as c:
            rows = c.execute(
                "SELECT id, belief, confidence FROM beliefs WHERE status = 'active'"
            ).fetchall()
            for bid, old, oldc in rows:
                if beliefs_similar(t, old):
                    nc = min(0.95, max(float(oldc or 0), conf) + 0.08)
                    c.execute("UPDATE beliefs SET confidence = ?, updated_at = ? "
                              "WHERE id = ?", (nc, now, int(bid)))
                    c.execute("INSERT INTO belief_evidence "
                              "(belief_id, side, content, source, at) "
                              "VALUES (?, 'support', ?, ?, ?)",
                              (int(bid), t, source or origin or "", now))
                    c.commit()
                    return {"id": int(bid), "belief": old, "confidence": round(nc, 3),
                            "merged": True}
            cur = c.execute(
                "INSERT INTO beliefs (belief, confidence, origin, status, "
                "created_at, updated_at, related_memories) "
                "VALUES (?, ?, ?, 'active', ?, ?, '')",
                (t, conf, origin or "", now, now))
            nid = int(cur.lastrowid or 0)
            c.execute("INSERT INTO belief_evidence "
                      "(belief_id, side, content, source, at) "
                      "VALUES (?, 'support', ?, ?, ?)",
                      (nid, t, source or origin or "", now))
            c.commit()
        return {"id": nid, "belief": t, "confidence": round(conf, 3), "merged": False}

    def belief_stats(self) -> dict:
        with _connect(self.db_path) as c:
            tot = c.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0]
            act = c.execute(
                "SELECT COUNT(*) FROM beliefs WHERE status='active'").fetchone()[0]
            ev = c.execute("SELECT COUNT(*) FROM belief_evidence").fetchone()[0]
            hi = c.execute(
                "SELECT COUNT(*) FROM beliefs WHERE confidence >= 0.75").fetchone()[0]
        return {"total": tot, "active": act, "evidence": ev, "strong": hi}

    async def reread(self, prov, every: int = 3) -> dict:
        """每 every 章通读一次，重写导读。"""
        n = self.count()
        if prov is None or n == 0:
            return {"ok": False, "why": "还没章节"}
        if n % max(1, every) != 0:
            return {"ok": True, "skipped": "还没到 %d 章的整数倍" % every}
        chs = self.list_chapters(limit=8)
        src = "\n\n".join("## 第%s章\n%s" % (c.get("chapter"),
                                            str(c.get("body") or "")[:600])
                          for c in reversed(chs))
        try:
            resp = await prov.text_chat(
                prompt="【我写过的章节】\n" + src + "\n\n写导读。",
                system_prompt=_SYS_REREAD)
            body = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "why": "调模型失败：%s" % e}
        if len(body) < 20:
            return {"ok": False, "why": "太短", "raw": body[:120]}
        self.set_meta(body)
        return {"ok": True, "meta": body}
