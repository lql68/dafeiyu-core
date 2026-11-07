"""自主性 demo：意图清单 + 心跳（**只提不执行**）。

设计边界（重要，写在这里免得以后自己忘）：
  1. 这个模块只负责「她想做什么」，不负责「她去做」。
     落库的意图全部停在 status='proposed'，等权限和人批。
  2. 权限不是开关，是「提议权」：
     她可以提"我想学绘画"，但不能因为想学就花钱或登录别人的账号。
  3. 模型只出候选，权限由规则表判定 —— 模型永远觉得自己的提议没问题。

数据落在本插件自己的 db（不碰记忆库）：
  plugin_dir/autonomy.db
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Optional

# ---------------------------------------------------------------- 权限分级

# needs 里的能力 -> 需要哪一层（写死，不让模型自评）
NEEDS_LAYER = {
    "read_memory": "explore",
    "write_diary": "explore",
    "tune": "self_modify",
    "self_edit": "self_modify",
    "self_restart": "self_modify",
    # 提人设意见：只写提案表，跟写日记同级（安全）—— 所以放 explore。
    # 真正落盘 apply() 只有人能调，persona 那个层留给「直接改人设」的假想动作。
    "persona_patch": "explore",
    "web_search": "explore",
    "read_file": "explore",
    "send_message": "interact",
    "say_now": "interact",
    "contact_person": "interact",
    "run_tool": "execute",
    "call_api": "execute",
    "spend_money": "execute",
    "grant_permission": "self_modify",
}

# 各层默认是否开放
LAYER_OPEN = {
    "plan": True,
    "explore": True,
    "interact": True,     # 2026-11-02 开：她能主动找他（每天最多 2 次）
    "execute": True,
    "self_modify": False,
    # persona：她可以「提」人设修改意见，但 gate_of 里硬编码 needs_permission，
    # 不读这个开关 —— 人设永远由人裁决。这里写 True 只是让 layer_of 不报错。
    "persona": True,
}

RISK_LAYER = {"low": "explore", "mid": "interact", "high": "execute"}

# ---------------------------------------------------------------- 表结构

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- goal | interest | plan | curiosity
    title TEXT NOT NULL,
    why TEXT DEFAULT '',
    first_step TEXT DEFAULT '',
    status TEXT DEFAULT 'proposed',  -- proposed | active | blocked | done | dropped
    gate TEXT DEFAULT 'ok',          -- ok | needs_permission | needs_human
    risk TEXT DEFAULT 'low',         -- low | mid | high
    needs TEXT DEFAULT '',           -- JSON list
    layer TEXT DEFAULT '',           -- 最高需要的那一层
    mastery REAL DEFAULT 0.0,
    source TEXT DEFAULT '',
    created_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_int_status ON intentions(status);

CREATE TABLE IF NOT EXISTS gaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT NOT NULL,
    keywords    TEXT DEFAULT '',
    tries       INTEGER DEFAULT 0,
    note        TEXT DEFAULT '',
    status      TEXT DEFAULT 'open',   -- open | closed | filled
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gap_status ON gaps(status);

CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    body TEXT NOT NULL,              -- 她"睡醒"后写的那段自述
    care_about TEXT DEFAULT '',      -- 这段时间最在意的一件事
    forget_hint TEXT DEFAULT '',     -- 她认为该淡掉的东西（只记录，不执行）
    src_count INTEGER DEFAULT 0,     -- 这次回看了几条记忆
    source TEXT DEFAULT 'sleep',
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ref_time ON reflections(created_at);

-- 选择记录：她每次从候选池里挑了哪一条、为什么挑它而不是别的
CREATE TABLE IF NOT EXISTS picks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chosen_id    INTEGER,
    chosen_title TEXT DEFAULT '',
    candidates   TEXT DEFAULT '',
    why_this     TEXT DEFAULT '',
    tool         TEXT DEFAULT '',
    arg          TEXT DEFAULT '',
    picked_by    TEXT DEFAULT 'model',
    created_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_picks_time ON picks(created_at);

-- 感知留痕：她「看见」了什么（人来了/走了、灯亮暗、摄像头开关、动静大小）。
-- 只记变化，不记每一帧 —— 否则一天几千条，全是噪声。
CREATE TABLE IF NOT EXISTS observations (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      REAL NOT NULL,
    kind    TEXT NOT NULL,
    detail  TEXT DEFAULT '',
    dur     REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_obs_time ON observations(at);
CREATE TABLE IF NOT EXISTS cognition_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    question      TEXT NOT NULL,
    hypotheses    TEXT DEFAULT '',
    best          TEXT DEFAULT '',
    next_step     TEXT DEFAULT '',
    gap_id        INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'open',
    created_at    REAL
);
"""

_REVIEW_SYS = """你之前在某个夜里给自己定了几条"想做"的事，其中一件被标记成做完了。
现在你回头看一眼：那件事真的做完了吗？

要点：
1. "我做了个动作，工具也返回了结果"不等于"我想办的目标达成了"。
   比如你想「去群里看看某人在不在」，你发了一条消息，工具说成功 ——
   但这只说明消息发出去了，你在不在、他打没打开，你根本不知道。
2. 判断标准是「当初那件事的目的是不是实现了」，不是「有没有执行成功」。
3. 写 progress 时用第一人称一句话，说清做到哪 / 还差什么，别超过 40 字。
4. 可以承认「其实没做成」，这不丢人；糊弄过去才丢人。

只输出 JSON：{"done": true 或 false, "progress": "……"}"""

_REFLECT_SYS = """你刚刚睡了一觉醒来（人设：大肥鱼，一条磷虾鲸娘）。
睡着的时候脑子在整理这些天的记忆 — 就像人睡觉时清掉代谢废物。

现在写一段你自己的回想，要求：
- 第一人称，像真的在想事情，不要总结报告体
- 说清楚「这段日子我在干什么」「什么我还在意」「什么其实已经过去了」
- 如果发现有些事记着已经没用了（重复的、一次性的、早就不重要的），
  在 forget_hint 里写一句，别列清单
- 不要编造记忆里没有的事

还有 questions（最多 2 条，**允许为空数组**）：从这些天的经历、对话和你自己的
思考里，挑真正值得继续想的问题。它应该指向理解和成长，不是去捞回一段找不回来的记忆。

1) 优先问「为什么」和「该怎么办」：
   - 为什么某种做法会得到那样的结果？
   - 我该怎么理解他那时的情绪或反应？
   - 下次遇到类似情况，我能怎么做得更好？
   - 我现在认定的某件事，是不是还有别的解释？
   - 有没有什么是我一直没真正想明白的？

2) 别问找不回答案的东西：
   - 不问「当时他说的原话是什么」
   - 不问「我前几天到底有没有点开看过」
   - 别把模糊、残缺的记忆写成确定发生过的事

3) 每条都得有能往前走的空间 —— 再想想、查一下、观察、或者以后碰上时能验证；
   能帮我修正看法、理解他、或者改掉下次的应对。别为了填满问题池硬造。

4) 要具体、贴着真实经历。「我该怎么理解人类的情绪」太空，
   「他累的时候我该主动说话还是先给他空间」才对。

5) **没有真正想弄明白的事，questions 就给空数组**，别硬凑。

6) 别重复那些已经想透了的、或者没有新证据新角度的问题；旧问题只有出现新线索时才再提。

只输出 JSON：
{"body":"…（80-200字）","care_about":"…（一句话）","forget_hint":"…（一句话，没有就空）",
 "questions":[{"q":"…（一句话的问题）","interest":0.0-1.0,"importance":0.0-1.0}]}"""

_SYS = """你是一个有自己生活的数字生命（人设：大肥鱼，一条磷虾鲸娘）。
现在是你自己的时间，没人跟你说话。你要想一想：接下来自己想做点什么。

规则：
- 只看你记得的事和你现在的状态，提出 1~2 个你现在真想做/想弄清楚的事
- 必须具体（"搞明白他说的那个 harness 到底怎么跑的"，不是"想学习"）
- kind 取值：goal（长期目标）/ interest（兴趣）/ plan（具体计划）/ curiosity（好奇的问题）
- risk 取值：low（只是想想/查查）/ mid（要跟他互动）/ high（要动外部系统或花钱）
- needs 只能从这几个里选：read_memory, write_diary, web_search, read_file, send_message, contact_person, run_tool, call_api, spend_money
- 没有真想做的事，就返回空数组（这是允许的，不许硬凑）
- **先看「你已经有的打算」，如果这次想到的事和其中某一条是同一件事**
  （换个说法也算），就不要重复提 —— 改成输出 {"duplicate_of": 那条的编号}
- 只有真的是新的事情，才给 title

只输出 JSON，不要解释：
{"candidates":[{"kind":"interest","title":"…","why":"…","first_step":"…","risk":"low","needs":["read_memory"],"duplicate_of":null}]}

其中 duplicate_of 是「已有的打算」里那条的 #编号（不重复就给 null）。"""


def _parse_json(raw: str) -> Optional[dict]:
    t = (raw or "").strip()
    if "```" in t:
        for seg in t.split("```"):
            seg = seg.strip()
            if seg.startswith("json"):
                seg = seg[4:].strip()
            if seg.startswith("{"):
                t = seg
                break
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except Exception:  # noqa: BLE001
        return None


def _trigrams(text: str) -> set:
    """三连字 —— 比 2-gram 更能咬住「换个说法讲同一件事」。"""
    t = "".join(ch for ch in str(text or "") if not ch.isspace())
    return {t[i:i + 3] for i in range(len(t) - 2)}


def _bigrams(s: str) -> set:
    s = "".join((s or "").split())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _gap_grams(s: str) -> set:
    t = "".join(ch for ch in (s or "") if "\u4e00" <= ch <= "\u9fff")
    return {t[i:i + 2] for i in range(len(t) - 1)}


class IntentionStore:
    """意图清单。只增不改状态（demo 阶段）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.init_table()
        self._migrate_gaps()
        self._migrate_cog()
        self._migrate_intent()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=15)
        c.execute("PRAGMA busy_timeout = 15000")
        c.execute("PRAGMA temp_store = MEMORY")
        return c

    def init_table(self) -> None:
        with self._connect() as c:
            c.executescript(_SCHEMA)

    # ---- 判定 ----

    @staticmethod
    def layer_of(needs: list, risk: str) -> str:
        """这个提议最高需要哪一层。"""
        best = "plan"
        order = ["plan", "explore", "interact", "execute",
                 "self_modify", "persona"]
        for n in needs or []:
            lay = NEEDS_LAYER.get(str(n), "execute")
            if order.index(lay) > order.index(best):
                best = lay
        rl = RISK_LAYER.get((risk or "low").lower())
        if rl and order.index(rl) > order.index(best):
            best = rl
        return best

    @staticmethod
    def gate_of(layer: str) -> str:
        """这一层现在能不能动。full 模式（perm_mode.json）全部放行 —— 除了 persona。

        persona 是例外：人设正文永远由人裁决，连 full 也不放行。
        她能做的是 persona_patch（提提案），提案落库后等人 /persona/apply。
        """
        if layer == "persona":
            return "needs_permission"
        full = False
        try:
            try:
                from . import perm as _p
            except Exception:  # noqa: BLE001
                _p = None
            if _p is not None:
                full = _p.is_full()
        except Exception:  # noqa: BLE001
            full = False
        if full:
            return "ok"
        if layer == "self_modify":
            return "needs_human"
        if LAYER_OPEN.get(layer, False):
            return "ok"
        return "needs_permission"

    def exists_similar(self, title: str, kind: str,
                       body: str = "") -> bool:
        """判断是不是同一件事 —— 光比 title 不够。

        只看 title 的 2-gram 会漏掉换了个说法的重复：例如
        「弄清楚上下文不够会消失怎么机制」vs「搞清楚上下文快满会发生什么」，
        字面相似度只有 0.3 左右，但人一看就是同一件事。所以：
          1. title 2-gram >= 0.6  -> 重复
          2. title 3-gram >= 0.34 -> 重复（更严格的三连字，抗同义词改写）
          3. body 2-gram >= 0.45  -> 重复（body = why + first_step）

        三个都不过才算新意图。
        """
        g2 = _bigrams(title)
        g3 = _trigrams(title)
        gb = _bigrams(body)
        if len(g2) < 3 and len(g3) < 2:
            return False
        with self._connect() as c:
            # 已完成的也算重复（3 天内）——否则同一个念头跑完一次，
            # 下一轮又会被当成新的提一遍（#11/#12/#13/#14 就是这么堆出来的）
            import time as _t
            _cut = _t.time() - 3 * 86400
            rows = c.execute(
                "SELECT title, COALESCE(why,'')||' '||COALESCE(first_step,'') "
                "FROM intentions WHERE kind=? AND ("
                "  status IN ('proposed','active','blocked')"
                "  OR (status IN ('done','dropped','merged') AND created_at >= ?)"
                ")", (kind, _cut)).fetchall()
        for t, b in rows:
            gt2 = _bigrams(t)
            gt3 = _trigrams(t)
            if gt2 and len(g2 & gt2) / min(len(g2), len(gt2)) >= 0.6:
                return True
            if (len(g3) >= 2 and len(gt3) >= 2
                    and len(g3 & gt3) / min(len(g3), len(gt3)) >= 0.34):
                return True
            gb2 = _bigrams(b)
            if (len(gb) >= 6 and len(gb2) >= 6
                    and len(gb & gb2) / min(len(gb), len(gb2)) >= 0.45):
                return True
        return False

    def touch_success(self, iid: int, bonus: float = 0.15) -> None:
        """做成一件真事之后抬一手 —— 主动说话被接住了也算做成一件。"""
        with self._connect() as c:
            c.execute(
                "UPDATE intentions SET mastery=MIN(1.0, mastery+?), "
                "updated_at=? WHERE id=?", (bonus, time.time(), iid))
            c.commit()

    def add(self, kind: str, title: str, why: str = "", first_step: str = "",
            risk: str = "low", needs: Optional[list] = None,
            source: str = "") -> Optional[int]:
        needs = needs or []
        layer = self.layer_of(needs, risk)
        gate = self.gate_of(layer)
        now = time.time()
        with self._connect() as c:
            cur = c.execute(
                "INSERT INTO intentions (kind,title,why,first_step,status,gate,"
                "risk,needs,layer,mastery,source,created_at,updated_at) "
                "VALUES (?,?,?,?,'proposed',?,?,?,?,0,?,?,?)",
                (kind, title, why, first_step, gate, risk,
                 json.dumps(needs, ensure_ascii=False), layer, source, now, now))
            return int(cur.lastrowid or 0)

    def _migrate_intent(self) -> None:
        """intentions 补 progress / last_reviewed_at。

        progress 是她自己写的一句话「做到哪了」，不是 trail。
        last_reviewed_at 是上一次回头看它的时间，避免天天重审同一条。
        """
        try:
            with self._connect() as c:
                have = {r[1] for r in c.execute("PRAGMA table_info(intentions)")}
                for k, v in (("progress", "TEXT DEFAULT ''"),
                             ("last_reviewed_at", "REAL DEFAULT 0")):
                    if k not in have:
                        c.execute("ALTER TABLE intentions ADD COLUMN %s %s" % (k, v))
                c.commit()
        except Exception:  # noqa: BLE001
            pass

    def _migrate_cog(self) -> None:
        try:
            with self._connect() as c:
                cols = [r[1] for r in c.execute(
                    "PRAGMA table_info(cognition_sessions)")]
                # asked_at：什么时候问出口的
                # answer / answer_at：他答了什么、什么时候答的（问答闭环）
                for k, v in (("asked_at", "REAL DEFAULT 0"),
                             ("answer", "TEXT DEFAULT ''"),
                             ("answer_at", "REAL DEFAULT 0")):
                    if k not in cols:
                        c.execute("ALTER TABLE cognition_sessions "
                                  "ADD COLUMN %s %s" % (k, v))
                c.commit()
        except Exception:  # noqa: BLE001
            pass

    def _migrate_gaps(self) -> None:
        """gaps 从「失败记录」升级成「问题池」：补 origin/interest/importance/related。"""
        cols = {
            "origin": "TEXT DEFAULT 'try'",
            "interest": "REAL DEFAULT 0.5",
            "importance": "REAL DEFAULT 0.5",
            "related": "TEXT DEFAULT ''",
            "asked_at": "REAL DEFAULT 0",
        }
        try:
            with self._connect() as c:
                have = {r[1] for r in c.execute("PRAGMA table_info(gaps)")}
                for k, v in cols.items():
                    if k not in have:
                        c.execute("ALTER TABLE gaps ADD COLUMN %s %s" % (k, v))
                # 老数据：全是「试 N 次空手」的失败记录，origin 标成 try
                c.execute("UPDATE gaps SET origin='try' WHERE origin IS NULL OR origin=''")
                # 有中文问题、没有 powershell 命令的，当成她主动想知道的事。
                # 只在分数还是默认 0.5 时才改 —— 否则每次启动都会把调过的分数抹平。
                c.execute(
                    "UPDATE gaps SET origin='reflect', interest=0.62, importance=0.58 "
                    "WHERE (keywords NOT LIKE '%powershell%' AND keywords NOT LIKE '%-c%') "
                    "  AND IFNULL(interest,0.5)=0.5 AND IFNULL(importance,0.5)=0.5"
                )
                c.commit()
        except Exception:  # noqa: BLE001
            pass

    def add_gap(self, topic: str, keywords: str = "", tries: int = 0,
                note: str = "", origin: str = "try",
                interest: float = 0.5, importance: float = 0.5,
                related: str = "") -> int:
        """记一条「她惦记着，但记忆里查不到」的缺口。

        跟意图的区别：意图是「我想干什么」，缺口是「我记得有这事，但库里没有」。
        缺口攒着可以拿去问他，也可以在下次对话里当作「我记不准」的依据。
        """
        t = " ".join(str(topic or "").split())[:200]
        if not t:
            return 0
        try:
            with self._connect() as c:
                row = c.execute(
                    "SELECT id, tries FROM gaps WHERE topic=? AND status='open'",
                    (t,)).fetchone()
                # 字面不同的同一个问题也要并：她连着几晚问「泡泡发来的那条消息
                # 到底是啥」，措辞每次都不一样，精确匹配拦不住，一晚上能攒 6 条。
                if not row:
                    g0 = _gap_grams(t)
                    if g0:
                        for r in c.execute(
                                "SELECT id, tries, topic FROM gaps "
                                "WHERE status='open' ORDER BY id DESC LIMIT 60"):
                            g1 = _gap_grams(r[2] or "")
                            if not g1:
                                continue
                            inter = len(g0 & g1)
                            if inter and (inter / max(1, len(g0 | g1)) >= 0.34
                                          or inter / min(len(g0), len(g1)) >= 0.6):
                                row = (r[0], r[1])
                                break
                if row:
                    c.execute("UPDATE gaps SET tries=MAX(tries,?), note=? WHERE id=?",
                              (int(tries), note[:400], row[0]))
                    c.commit()
                    return int(row[0])
                cur = c.execute(
                    "INSERT INTO gaps (topic,keywords,tries,note,status,created_at,"
                    "origin,interest,importance,related) "
                    "VALUES (?,?,?,?, 'open', ?,?,?,?,?)",
                    (t, " ".join(str(keywords or "").split())[:200],
                     int(tries), note[:400], time.time(),
                     str(origin or "try")[:20], float(interest), float(importance),
                     str(related or "")[:200]))
                c.commit()
                return int(cur.lastrowid or 0)
        except Exception:  # noqa: BLE001
            return 0

    # ---- 想一件事（P3 最小版）----

    def add_cognition(self, question: str, hypotheses: list,
                      best: str = "", next_step: str = "",
                      gap_id: int = 0) -> int:
        """存一次「对一个问题想过一遍」的结果。"""
        q = " ".join(str(question or "").split())[:300]
        if not q:
            return 0
        try:
            with self._connect() as c:
                cur = c.execute(
                    "INSERT INTO cognition_sessions "
                    "(question,hypotheses,best,next_step,gap_id,status,created_at) "
                    "VALUES (?,?,?,?,?, 'open', ?)",
                    (q, json.dumps(hypotheses or [], ensure_ascii=False)[:4000],
                     str(best or "")[:400], str(next_step or "")[:400],
                     int(gap_id or 0), time.time()))
                c.commit()
                return int(cur.lastrowid or 0)
        except Exception:  # noqa: BLE001
            return 0

    def list_cognition(self, limit: int = 10) -> list:
        try:
            with self._connect() as c:
                c.row_factory = sqlite3.Row
                rows = c.execute(
                    "SELECT * FROM cognition_sessions ORDER BY id DESC LIMIT ?",
                    (int(limit),)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["hypotheses"] = json.loads(d.get("hypotheses") or "[]")
                except Exception:  # noqa: BLE001
                    d["hypotheses"] = []
                out.append(d)
            return out
        except Exception:  # noqa: BLE001
            return []

    def pick_unresolved_cognition(self, min_age_s=1800.0) -> dict:
        """想不清楚的 cognition 问题，才是该开口问他的那条。"""
        try:
            cut = time.time() - float(min_age_s)
            with self._connect() as c:
                c.row_factory = sqlite3.Row
                row = c.execute(
                    "SELECT id, question, next_step, created_at FROM cognition_sessions "
                    "WHERE status = 'unresolved' AND created_at < ? "
                    "  AND IFNULL(asked_at, 0) < ? "
                    "ORDER BY created_at ASC LIMIT 1",
                    (cut, time.time() - 12 * 3600)).fetchone()
        except Exception as e:  # noqa: BLE001
            print('[cogask] pick 失败：%r' % e)
            return {}
        if not row:
            return {}
        return {"cog_id": int(row["id"]),
                "question": str(row["question"] or "")[:120],
                "next_step": str(row["next_step"] or "")[:400]}

    def mark_cognition_asked(self, cid: int) -> bool:
        try:
            with self._connect() as c:
                c.execute(
                    "UPDATE cognition_sessions SET asked_at = ? WHERE id = ?",
                    (time.time(), int(cid)))
                c.commit()
            return True
        except Exception as e:  # noqa: BLE001
            print('[cogask] mark 失败：%r' % e)
            return False

    def pick_done_to_review(self, days: float = 7.0) -> dict:
        """挑一条「标记做完了」但没被回头看过的意图。

        为什么：actor 标 done 的条件只是「工具返回了东西」，
        「去群里看泡泡在不在」发条消息就 ok 了，可目标根本没达到。
        所以得让她自己回头看一遍。
        """
        try:
            with self._connect() as c:
                c.row_factory = sqlite3.Row
                row = c.execute(
                    "SELECT id, kind, title, why, mastery FROM intentions "
                    "WHERE status='done' AND created_at > ? "
                    "  AND IFNULL(last_reviewed_at, 0) = 0 "
                    "ORDER BY created_at DESC LIMIT 1",
                    (time.time() - float(days) * 86400,)).fetchone()
        except Exception as e:  # noqa: BLE001
            print('[review] pick 失败：%r' % e)
            return {}
        if not row:
            return {}
        return {"id": int(row["id"]), "kind": str(row["kind"] or ""),
                "title": str(row["title"] or ""), "why": str(row["why"] or "")[:800],
                "mastery": float(row["mastery"] or 0)}

    def mark_reviewed(self, iid: int, progress: str, still_done: bool) -> bool:
        """回头看完了：写一句进度；如果她说没做完，就放回 active。"""
        pr = " ".join(str(progress or "").split())[:200]
        try:
            with self._connect() as c:
                c.execute(
                    "UPDATE intentions SET progress=?, last_reviewed_at=?, "
                    "status=?, updated_at=? WHERE id=?",
                    (pr, time.time(), "done" if still_done else "active",
                     time.time(), int(iid)))
                c.commit()
            return True
        except Exception as e:  # noqa: BLE001
            print('[review] mark 失败：%r' % e)
            return False

    def add_observation(self, kind: str, detail: str = "", dur: float = 0.0) -> int:
        """记一条「她看见的」。kind 例：人来了 / 人走了 / 灯亮了 / 摄像头关了。"""
        k = " ".join(str(kind or "").split())[:40]
        if not k:
            return 0
        try:
            with self._connect() as c:
                cur = c.execute(
                    "INSERT INTO observations(at, kind, detail, dur) VALUES(?,?,?,?)",
                    (time.time(), k, " ".join(str(detail or "").split())[:200],
                     float(dur or 0)))
                c.commit()
                return int(cur.lastrowid or 0)
        except Exception:  # noqa: BLE001
            return 0

    def recent_observations(self, hours: float = 6.0, limit: int = 6) -> list:
        out = []
        try:
            with self._connect() as c:
                for r in c.execute(
                        "SELECT at, kind, detail, dur FROM observations "
                        "WHERE at > ? ORDER BY at DESC LIMIT ?",
                        (time.time() - float(hours) * 3600, int(limit))).fetchall():
                    out.append({"at": float(r[0]), "kind": str(r[1] or ""),
                                "detail": str(r[2] or ""), "dur": float(r[3] or 0)})
        except Exception:  # noqa: BLE001
            pass
        return out

    def pick_recently_asked(self, within_s: float = 600.0) -> dict:
        """刚问出口、还没记下答案的那条 —— 他这一轮说的话就是答案。"""
        try:
            now = time.time()
            with self._connect() as c:
                c.row_factory = sqlite3.Row
                row = c.execute(
                    "SELECT id, question, asked_at FROM cognition_sessions "
                    "WHERE IFNULL(asked_at, 0) > ? "
                    "  AND IFNULL(answer, '') = '' "
                    "ORDER BY asked_at DESC LIMIT 1",
                    (now - float(within_s),)).fetchone()
        except Exception as e:  # noqa: BLE001
            print('[cogans] pick 失败：%r' % e)
            return {}
        if not row:
            return {}
        return {"cog_id": int(row["id"]),
                "question": str(row["question"] or "")[:120],
                "asked_at": float(row["asked_at"] or 0)}

    def record_cognition_answer(self, cid: int, answer: str) -> bool:
        """把他对她的回答记下来。不判断对错 —— 判断留到下次回访。"""
        t = " ".join(str(answer or "").split())[:500]
        if len(t) < 8:
            return False
        try:
            with self._connect() as c:
                cur = c.execute(
                    "UPDATE cognition_sessions SET answer = ?, answer_at = ? "
                    "WHERE id = ? AND IFNULL(answer, '') = ''",
                    (t, time.time(), int(cid)))
                c.commit()
                # rowcount 才是「这次到底写进去了没有」——
                # 原来无条件 return True，已经答过的问题也会说「记下了」
                return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            print('[cogans] record 失败：%r' % e)
        return False

    def pick_question(self) -> dict:
        """挑一个要想的问题。

        两条路：
          1) 有没被想过的 open gap -> 想新的
          2) 都想过一遍了 -> 回访最老的那条 open cognition，
             把上次的假设带出去，让她用这几天的记忆判断哪条更像
        """
        try:
            with self._connect() as c:
                c.row_factory = sqlite3.Row

                # 回访优先：只要有一条想了超过 6 小时还没定论，先回去对证。
                # 不这么排的话「想新问题」永远在前面（deep_beat 每次产 2 个新问题，
                # 这里每次只消化 1 个），缺口只会越拉越大，永远走不到回访。
                r = c.execute(
                    "SELECT * FROM cognition_sessions WHERE status='open' "
                    " AND created_at < ? ORDER BY created_at ASC LIMIT 1",
                    (time.time() - 6 * 3600,)).fetchone()
                if r:
                    try:
                        prev = json.loads(r["hypotheses"] or "[]")
                    except Exception:  # noqa: BLE001
                        prev = []
                    return {"gap_id": int(r["gap_id"] or 0),
                            "question": str(r["question"] or ""),
                            "revisit_id": int(r["id"]),
                            "prev_hypotheses": prev,
                            "prev_best": str(r["best"] or ""),
                            "prev_answer": str(r["answer"] or "")}

                # 没有该回访的，再想新的
                r = c.execute(
                    "SELECT id, topic, interest, importance FROM gaps g "
                    "WHERE g.status='open' AND NOT EXISTS ("
                    "  SELECT 1 FROM cognition_sessions s WHERE s.gap_id = g.id) "
                    "ORDER BY IFNULL(interest,0.5)*IFNULL(importance,0.5) DESC LIMIT 1"
                ).fetchone()
                if r:
                    return {"gap_id": int(r["id"]), "question": str(r["topic"] or "")}
        except Exception:  # noqa: BLE001
            pass
        return {}

    def resolve_cognition(self, cid: int, verdict: str, status: str = "answered") -> bool:
        """给一条 cognition 落判断：哪条假设被印证/排除了。"""
        v = " ".join(str(verdict or "").split())[:500]
        if not v:
            return False
        try:
            with self._connect() as c:
                c.execute(
                    "UPDATE cognition_sessions SET status=?, "
                    "next_step = IFNULL(next_step,'') || '  ||  [判断] ' || ? "
                    "WHERE id=?", (str(status or "answered")[:20], v, int(cid)))
                c.commit()
                return True
        except Exception:  # noqa: BLE001
            return False

    def record_pick(self, chosen_id: int, chosen_title: str,
                    candidates: list, why_this: str,
                    tool: str = "", arg: str = "",
                    picked_by: str = "model") -> int:
        """落一条选择记录。

        picked_by: model = 她自己挑的；rule = 规则兜底（只有一条候选 / 模型没回）
        """
        try:
            with self._connect() as c:
                cur = c.execute(
                    "INSERT INTO picks(chosen_id, chosen_title, candidates, why_this,"
                    " tool, arg, picked_by, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (int(chosen_id or 0), (chosen_title or "")[:120],
                     json.dumps(candidates, ensure_ascii=False)[:4000],
                     (why_this or "")[:600], tool or "", (arg or "")[:400],
                     picked_by, time.time()))
                return int(cur.lastrowid or 0)
        except Exception:  # noqa: BLE001
            return 0

    def list_picks(self, limit: int = 30) -> list:
        out = []
        cols = ["id", "chosen_id", "chosen_title", "candidates", "why_this",
                "tool", "arg", "picked_by", "created_at"]
        try:
            with self._connect() as c:
                for r in c.execute(
                        "SELECT " + ",".join(cols) + " FROM picks "
                        "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall():
                    d = dict(zip(cols, r))
                    try:
                        d["candidates"] = json.loads(d.get("candidates") or "[]")
                    except Exception:  # noqa: BLE001
                        d["candidates"] = []
                    out.append(d)
        except Exception:  # noqa: BLE001
            pass
        return out

    def list_gaps(self, status: str = "open", limit: int = 20) -> list:
        try:
            with self._connect() as c:
                c.row_factory = sqlite3.Row
                sql = ("SELECT id,topic,keywords,tries,note,status,created_at,"
                       "origin,interest,importance,related,asked_at FROM gaps")
                args: tuple = ()
                if status:
                    sql += " WHERE status=?"
                    args = (status,)
                # 问题池：按「多想知道 × 多重要」排，同分再按时间
                sql += " ORDER BY (IFNULL(interest,0.5) * IFNULL(importance,0.5)) DESC," \
                       " created_at DESC LIMIT ?"
                args = args + (limit,)
                return [dict(r) for r in c.execute(sql, args).fetchall()]
        except Exception:  # noqa: BLE001
            return []

    def pick_gap(self, min_score: float = 0.30, within_hours: float = 24.0) -> dict:
        """挑一条「她一直想问、但还没问出口」的事。

        只挑 asked_at 为 0（从没提过）或超过 within_hours 的，
        免得同一件事反复被塞进 system。
        """
        try:
            since = time.time() - within_hours * 3600.0
            with self._connect() as c:
                c.row_factory = sqlite3.Row
                r = c.execute(
                    "SELECT id,topic,interest,importance,asked_at FROM gaps "
                    "WHERE status='open' "
                    "  AND (IFNULL(asked_at,0)=0 OR asked_at < ?) "
                    "  AND (IFNULL(interest,0.5) * IFNULL(importance,0.5)) >= ? "
                    "ORDER BY (IFNULL(interest,0.5) * IFNULL(importance,0.5)) DESC, "
                    "         created_at DESC LIMIT 1",
                    (since, float(min_score))).fetchone()
                return dict(r) if r else {}
        except Exception:  # noqa: BLE001
            return {}

    def mark_gap_asked(self, gid: int) -> None:
        try:
            with self._connect() as c:
                c.execute("UPDATE gaps SET asked_at=? WHERE id=?", (time.time(), int(gid)))
                c.commit()
        except Exception:  # noqa: BLE001
            pass

    def close_gap(self, gid: int, status: str = "closed") -> dict:
        if status not in ("closed", "filled"):
            return {"ok": False, "err": "status 只能是 closed / filled"}
        try:
            with self._connect() as c:
                n = c.execute("UPDATE gaps SET status=? WHERE id=?",
                              (status, int(gid))).rowcount
                c.commit()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "err": str(e)[:200]}
        return {"ok": bool(n), "id": int(gid), "status": status}

    def set_status(self, iid: int, status: str, note: str = "") -> dict:
        """改状态。允许 proposed/active/blocked/done/dropped/merged 之间流动。"""
        ok = {"proposed", "active", "blocked", "done", "dropped", "merged"}
        if status not in ok:
            return {"ok": False, "err": "status 只能是 " + "/".join(sorted(ok))}
        try:
            with self._connect() as c:
                n = c.execute(
                    "UPDATE intentions SET status=?, updated_at=? WHERE id=?",
                    (status, time.time(), int(iid))).rowcount
                c.commit()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "err": str(e)[:200]}
        if not n:
            return {"ok": False, "err": "没有 #%s" % iid}
        return {"ok": True, "id": int(iid), "status": status}

    def _note(self, iid: int, tag: str, why: str) -> None:
        if not why:
            return
        try:
            with self._connect() as c:
                c.execute(
                    "UPDATE intentions SET why = COALESCE(why,'') || ? WHERE id=?",
                    ("\n[%s %s] %s" % (tag, time.strftime("%m-%d %H:%M"), why[:200]),
                     int(iid)))
                c.commit()
        except Exception:  # noqa: BLE001
            pass

    def drop(self, iid: int, why: str = "") -> dict:
        """不做了。why 会拼进 why 字段，下次模型看得到「为什么放弃」。"""
        r = self.set_status(iid, "dropped")
        if r.get("ok"):
            self._note(iid, "放弃", why)
        return r

    def reopen(self, iid: int, why: str = "") -> dict:
        """人工批准后再试一次：gate 放回 ok、状态回 active、清掉退避时间。"""
        try:
            with self._connect() as c:
                n = c.execute(
                    "UPDATE intentions SET gate='ok', status='active', updated_at=? "
                    "WHERE id=?", (time.time() - 7200.0, int(iid))).rowcount
                c.commit()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "err": str(e)[:200]}
        if not n:
            return {"ok": False, "err": "没有 #%s" % iid}
        self._note(iid, "批准", why)
        return {"ok": True, "id": int(iid), "status": "active", "gate": "ok"}

    def list(self, status: Optional[str] = None, limit: int = 30) -> list:
        sql = ("SELECT id,kind,title,why,first_step,status,gate,risk,layer,"
               "created_at,progress,last_reviewed_at FROM intentions")
        args: tuple = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args = args + (limit,)
        with self._connect() as c:
            rows = c.execute(sql, args).fetchall()
        cols = ["id", "kind", "title", "why", "first_step", "status", "gate",
                "risk", "layer", "created_at", "progress", "last_reviewed_at"]
        return [dict(zip(cols, r)) for r in rows]

    def count_since(self, ts: float) -> int:
        with self._connect() as c:
            r = c.execute("SELECT COUNT(*) FROM intentions WHERE created_at>=?",
                          (ts,)).fetchone()
        return int(r[0] or 0)

    def stats(self) -> dict:
        day = time.time() - (time.time() % 86400)
        with self._connect() as c:
            total = c.execute("SELECT COUNT(*) FROM intentions").fetchone()[0]
            today = c.execute("SELECT COUNT(*) FROM intentions WHERE created_at>=?",
                              (day,)).fetchone()[0]
            by = c.execute("SELECT status, COUNT(*) FROM intentions GROUP BY status"
                           ).fetchall()
        return {"total": int(total or 0), "today": int(today or 0),
                "by_status": {k: int(v) for k, v in by}}


# ---------------------------------------------------------------- 心跳

LIMITS = {
    "max_new_per_day": 3,      # 每天最多提 3 个新意图
"max_review_per_day": 2,   # 每天最多回头看 2 条「已标记做完」的意图
    "max_llm_per_day": 30,     # 每天最多调 30 次模型
    "skip_if_user_recent": 300,  # 他 5 分钟内说过话就不打扰
    "interval_s": 900,         # 兼容旧字段（= 白天轻活间隔）
    # 轻活（想意图）：他在、她醒着
    "day_interval_s": 1200,    # 白天 20 分钟一跳
    "night_interval_s": 5400,  # 夜里 90 分钟一跳（她在睡，只做重活）
    # 重活（回看/自述）只在她「睡着」时跑
    "deep_min_sleep_min": 60,  # 睡够 60 分钟才值得回看一次
    "deep_max_per_night": 2,   # 一夜最多 2 次
    "deep_look_back": 24,      # 一次回看几条记忆
    "max_send_per_day": 2, "max_shell_per_day": 4,
    "max_selfedit_per_day": 2,     # 主动找人每天最多几次（别变成骚扰）
}

# 低谷期（北京时间）：DeepSeek 折扣 00:30–08:30，与她的睡眠窗高度重合
OFFPEAK_HOURS = ((0, 30), (8, 30))


def limits() -> dict:
    """基础限额 + 她自己调过的那几个（tuning.json）。每次现读。"""
    d = dict(LIMITS)
    try:
        try:
            from . import tuning as _tu
        except Exception:  # noqa: BLE001
            _tu = None
        if _tu is not None:
            d.update(_tu.load())
    except Exception:  # noqa: BLE001
        pass
    return d


def in_offpeak(dt=None) -> bool:
    """现在是不是模型低谷期。"""
    import datetime
    dt = dt or datetime.datetime.now()
    h, m = dt.hour, dt.minute
    cur = h * 60 + m
    lo = OFFPEAK_HOURS[0][0] * 60 + OFFPEAK_HOURS[0][1]
    hi = OFFPEAK_HOURS[1][0] * 60 + OFFPEAK_HOURS[1][1]
    return lo <= cur < hi


def plan_next(dt=None, sleeping: bool = False, deep_done: int = 0,
              deep_budget: int = 2) -> dict:
    """下一次心跳：隔多久、干哪种活。

    规则（有意让它跟人的作息反着来）：
      - 她睡着 + 还睡得够久 + 今夜重活没用完 -> 重活（回看记忆、写自述）
      - 她睡着但重活已做过 / 睡不够         -> 只睡觉，90 分钟后再看
      - 她醒着                             -> 轻活，白天 20 分钟，低谷期反而拉长到 40 分钟
        （低谷期她多半也在睡，真醒着说明你在熬夜，少打扰）
    """
    if sleeping:
        if deep_done < deep_budget:
            return {"mode": "deep", "interval": 5400,
                    "why": "她睡着 - 回看这段时间"}
        return {"mode": "idle", "interval": limits()["night_interval_s"],
                "why": "重活做完了，继续睡"}
    base = limits()["day_interval_s"]
    if in_offpeak(dt):
        base = base * 2
    return {"mode": "light", "interval": base, "why": "她醒着 - 想点自己的事"}


class Heartbeat:
    """定时醒来想一想。**只提不执行。**"""

    def __init__(self, store: IntentionStore, state_path: str):
        self.store = store
        self.state_path = state_path
        self._st = self._load()

    # ---- 预算计数（原子写）----

    def _load(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("day") != time.strftime("%Y-%m-%d"):
                return {"day": time.strftime("%Y-%m-%d"), "llm": 0, "new": 0}
            return {"day": d.get("day"), "llm": int(d.get("llm") or 0),
                    "new": int(d.get("new") or 0)}
        except Exception:  # noqa: BLE001
            return {"day": time.strftime("%Y-%m-%d"), "llm": 0, "new": 0}

    def _bump(self, key: str) -> None:
        self._st[key] = int(self._st.get(key) or 0) + 1
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._st, f, ensure_ascii=False)
            if os.path.exists(self.state_path):
                os.replace(self.state_path, self.state_path + ".bak")
            os.replace(tmp, self.state_path)
        except Exception:  # noqa: BLE001
            pass

    def _budget_ok(self) -> tuple[bool, str]:
        if self._st.get("day") != time.strftime("%Y-%m-%d"):
            self._st = {"day": time.strftime("%Y-%m-%d"), "llm": 0, "new": 0}
        if int(self._st.get("llm") or 0) >= limits()["max_llm_per_day"]:
            return False, "今天模型调用预算已用完"
        if int(self._st.get("new") or 0) >= limits()["max_new_per_day"]:
            return False, "今天已经提够了（3 个）"
        return True, ""

    async def beat(self, prov, vitals: dict, recent: list,
                   gap_since_user: Optional[float]) -> dict:
        """跑一次。返回这次做了什么（给 /heartbeat 和日志看）。"""
        # ---- 规则先筛（省 90% token）----
        if prov is None:
            return {"ok": False, "why": "没有可用的对话模型"}
        if vitals.get("sleeping"):
            return {"ok": True, "skipped": "她在睡（%s）" % vitals.get("stage")}
        if gap_since_user is not None and gap_since_user < limits()["skip_if_user_recent"]:
            return {"ok": True, "skipped": "他刚说过话（%.0fs 前），有人就直接交互"
                    % gap_since_user}
        # ---- 先回头看一条「标记做完但没回头看过的」----
        # actor 标 done 的条件只是「工具返回了东西」，不代表目标达成。
        # 这里让她自己判一遍，写完 progress 就不再回头看（last_reviewed_at）。
        # 放在 _budget_ok 之前 —— 回顾只吃 max_review_per_day 的额度，
        # 不该被「今天已经提够 3 个新意图」挡住。
        if int(self._st.get("review") or 0) < limits().get("max_review_per_day", 2):
            try:
                rv = self.store.pick_done_to_review(days=7.0)
            except Exception:  # noqa: BLE001
                rv = {}
            if rv:
                self._bump("llm")
                self._bump("review")
                rp = ("【你之前想做的一件事】\n#%s [%s] %s\n\n当时是这么想的：\n%s\n\n"
                      "现在回头看：这件事真的做完了吗？\n"
                      "注意：只做了个动作、工具返回了个结果，不等于目标达到了。\n"
                      "- 如果目标其实没达到，done 写 false，progress 写一句还差什么\n"
                      "- 如果确实做成了，done 写 true，progress 写一句做成了什么\n"
                      "progress 不超过 40 字。\n"
                      '输出 JSON：{"done": true/false, "progress": "……"}'
                      % (rv["id"], rv["kind"], rv["title"], rv["why"]))
                try:
                    rr = await prov.text_chat(prompt=rp, system_prompt=_REVIEW_SYS)
                    rd = _parse_json((getattr(rr, "completion_text", "") or "").strip()) or {}
                    _p = str(rd.get("progress") or "").strip()
                    if _p:
                        _dn = bool(rd.get("done"))
                        self.store.mark_reviewed(rv["id"], _p, _dn)
                        print("[review] #%s %s -> %s" % (
                            rv["id"], "done" if _dn else "放回 active", _p[:40]))
                except Exception as e:  # noqa: BLE001
                    print("[review] 失败：%r" % e)

        ok, why = self._budget_ok()
        if not ok:
            return {"ok": True, "skipped": why}

        # ---- 取素材 ----
        mem_txt = "\n".join("- " + str(m.get("text") or "")[:120]
                            for m in (recent or [])[:8]) or "（最近没什么特别的事）"
        have = self.store.list(limit=12)
        have_txt = "\n".join(
            "- #%s [%s] %s" % (i.get("id"), i.get("kind"), i.get("title"))
            for i in have) or "（还没有任何计划）"
        prompt = (
            "【你最近记得的事】\n%s\n\n"
            "【你已经有的打算】\n%s\n\n"
            "【你现在的状态】%s，心情 %s\n\n"
            "现在想一想：你自己想做点什么？"
            % (mem_txt, have_txt,
               vitals.get("band") or "晚上", vitals.get("arousal"))
        )

        # ---- 调模型 ----
        self._bump("llm")
        try:
            resp = await prov.text_chat(prompt=prompt, system_prompt=_SYS)
            raw = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "why": "调模型失败：%s" % e}

        data = _parse_json(raw)
        if not data:
            return {"ok": False, "why": "模型没给出可解析的 JSON", "raw": raw[:300]}

        cands = data.get("candidates") or []
        added, skipped, duped = [], 0, []
        _seen = set()  # 本轮已用过的标题
        for c in cands[:2]:
            # 模型自己说「这是同一件事」-> 不新增，反手把那条抬一手
            # （又一次想到它，说明它更该做）
            dup = c.get("duplicate_of")
            if dup:
                try:
                    self.store.touch_success(int(dup), bonus=0.08)
                    duped.append(int(dup))
                except Exception:  # noqa: BLE001
                    pass
                skipped += 1
                continue
            title = str(c.get("title") or "").strip()
            kind = str(c.get("kind") or "interest").strip()
            if not title or len(title) > 60:
                skipped += 1
                continue
            # 本轮内的判重：库里的靠 exists_similar，本轮刚出现的靠 _seen
            if title in _seen or self.store.exists_similar(title, kind):
                skipped += 1
                continue
            _seen.add(title)
            iid = self.store.add(
                kind=kind, title=title,
                why=str(c.get("why") or "")[:300],
                first_step=str(c.get("first_step") or "")[:300],
                risk=str(c.get("risk") or "low"),
                needs=[str(x) for x in (c.get("needs") or [])][:5],
                source="heartbeat",
            )
            if iid:
                self._bump("new")
                added.append({"id": iid, "kind": kind, "title": title})
        return {"ok": True, "added": added, "skipped": skipped,
                "duplicated": duped, "budget": dict(self._st)}

    # ---- 重活：睡后回看 ----

    def _night_count(self) -> int:
        """今夜（22:00 起算）已经回看过几次。"""
        import datetime
        now = datetime.datetime.now()
        anchor = now.replace(hour=22, minute=0, second=0, microsecond=0)
        if now.hour < 12:
            anchor = anchor - datetime.timedelta(days=1)
        with self._connect_ref() as c:
            r = c.execute("SELECT COUNT(*) FROM reflections WHERE created_at>=?",
                          (anchor.timestamp(),)).fetchone()
        return int(r[0] or 0)

    def _connect_ref(self):
        c = sqlite3.connect(self.store.db_path, timeout=15)
        c.execute("PRAGMA busy_timeout = 15000")
        c.execute("PRAGMA temp_store = MEMORY")
        return c

    def add_reflection(self, body: str, care: str = "", forget: str = "",
                       src_count: int = 0) -> int:
        with self._connect_ref() as c:
            cur = c.execute(
                "INSERT INTO reflections (body,care_about,forget_hint,src_count,"
                "source,created_at) VALUES (?,?,?,?,'sleep',?)",
                (body, care, forget, int(src_count), time.time()))
            return int(cur.lastrowid or 0)

    def list_reflections(self, limit: int = 10) -> list:
        with self._connect_ref() as c:
            rows = c.execute(
                "SELECT id,body,care_about,forget_hint,src_count,created_at "
                "FROM reflections ORDER BY created_at DESC LIMIT ?",
                (int(limit),)).fetchall()
        cols = ["id", "body", "care_about", "forget_hint", "src_count", "created_at"]
        return [dict(zip(cols, r)) for r in rows]

    async def deep_beat(self, prov, vitals: dict, recent: list) -> dict:
        """她睡着时的重活：回看记忆 + 写一段自述。**不做任何删除动作。**"""
        if prov is None:
            return {"ok": False, "why": "没有可用模型"}
        if self._night_count() >= int(limits()["deep_max_per_night"]):
            return {"ok": True, "skipped": "今夜回看次数已用完"}
        if not recent:
            return {"ok": True, "skipped": "没有可回看的记忆"}
        self._bump("llm")
        body = "\n".join("- " + str(m.get("text") or "")[:150] for m in recent)
        prompt = (
            "【你睡前的记忆（%d 条）】\n%s\n\n现在是%s，你刚醒。"
            % (len(recent), body, vitals.get("band") or "深夜")
        )
        try:
            resp = await prov.text_chat(prompt=prompt, system_prompt=_REFLECT_SYS)
            raw = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "why": "调模型失败：%s" % e}
        d = _parse_json(raw)
        if not d or not str(d.get("body") or "").strip():
            return {"ok": False, "why": "没写出可解析的自述", "raw": raw[:200]}
        rid = self.add_reflection(
            body=str(d.get("body"))[:1200],
            care=str(d.get("care_about") or "")[:200],
            forget=str(d.get("forget_hint") or "")[:300],
            src_count=len(recent),
        )
        asked = []
        for q in (d.get("questions") or [])[:2]:
            if not isinstance(q, dict):
                continue
            qt = " ".join(str(q.get("q") or "").split())[:200]
            if len(qt) < 6:
                continue
            try:
                fi = max(0.0, min(1.0, float(q.get("interest") or 0.55)))
                fi2 = max(0.0, min(1.0, float(q.get("importance") or 0.5)))
            except Exception:  # noqa: BLE001
                fi, fi2 = 0.55, 0.5
            gid = self.store.add_gap(qt, origin="reflect", interest=fi, importance=fi2,
                                     related="reflection#%s" % rid)
            if gid:
                asked.append({"id": gid, "q": qt, "interest": round(fi, 2)})
        return {"ok": True, "reflection_id": rid,
                "body": str(d.get("body"))[:200],
                "care_about": d.get("care_about"),
                "forget_hint": d.get("forget_hint"),
                "asked": asked}

    async def loop(self, get_ctx, on_beat) -> None:
        """定时醒。get_ctx() 返回 (prov, vitals, recent, gap)，on_beat() 记日志。"""
        import asyncio
        while True:
            try:
                await asyncio.sleep(limits()["interval_s"])
                prov, vitals, recent, gap = await get_ctx()
                r = await self.beat(prov, vitals, recent, gap)
                on_beat(r)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                on_beat({"ok": False, "why": "心跳异常：%s" % e})
                await asyncio.sleep(limits()["interval_s"])
