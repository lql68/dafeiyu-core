"""成长档案：从已有的痕迹里**算**出来，不让模型重新写一遍。

为什么是「算」不是「生成」
------------------------
让模型每天写一份「我的核心价值观是……」= 她会写出你想看的话。
这些数据她本来就留下了 —— 人设意见、日记、反思、自传、选择记录、
技能表、缺口表 —— 差别只在没人把它们摆到一起。

所以本模块只做三件事：**读、归类、标注出处**。
凡是本模块里出现的一句「她说的话」，都带 `src`（哪张表哪条）和 `at`。

五块
----
A 基础性格   personas.system_prompt + persona_versions
B 价值观     persona_patches(op=opinion) + 自传里的认账句
C 世界观     procedures(body) 与 gaps 成对出现 —— 只列知道的是吹牛
D 兴趣审美   intentions(kind=interest) + picks(她真挑过什么) + 日记高频比喻
E 成长时间轴 autobiography + persona_versions + reflections 按时间排

另附两块只读视图
F 她怎么认知你  graph_nodes(node_type='person')
G 生命状态      vitality() —— 由 rhythm/warmth/attention 算，本模块只做派生
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import datetime

# 这些库的位置由调用方决定 —— 内核不该假设自己躺在插件目录里。
# 全部默认空串；**必须**由宿主调 configure() 注入，否则每个 block 都返回空。
# （早先默认值是 dirname(__file__)，装进 venv 后会去 site-packages 里找库，
#   结果是「面板一片空白但也不报错」—— 比直接报错更难查。）
PLUGIN_DIR = ""
AUTO_DB = ""
DATA_DB = ""
LM_HINT = ""
DIARY_DIR = ""


def configure(plugin_dir=None, auto_db=None, data_db=None, lm_db=None,
              diary_dir=None) -> None:
    """告诉内核：库在哪。

    plugin_dir 给了就按它的相对位置推 auto_db / diary_dir；
    单独给 auto_db / data_db / lm_db 则各管各的。
    """
    global PLUGIN_DIR, AUTO_DB, DATA_DB, LM_HINT, DIARY_DIR
    if plugin_dir:
        PLUGIN_DIR = os.path.abspath(plugin_dir)
        AUTO_DB = os.path.join(PLUGIN_DIR, "autonomy.db")
        DIARY_DIR = os.path.join(PLUGIN_DIR, "diary")
    if auto_db:
        AUTO_DB = auto_db
    if data_db:
        DATA_DB = data_db
    if lm_db:
        LM_HINT = lm_db
    if diary_dir:
        DIARY_DIR = diary_dir

# 自传里「认账」的标记 —— 认错、承认不懂、欠着没做
_ACK = ("我承认", "算欠着", "我不", "没弄明白", "没做到", "错了",
        "并不是", "其实我", "我不敢", "我怕")

# 日记里的比喻常常就是这个角色的核心意象。
# 第一版用 `像(一条|一只|…)?([\u4e00-\u9fa5]{2,8})` 抽出来的是
# 「鱼在玻璃缸里找根」「邀功或者诉苦」这类半句话 —— 不是比喻本身。
# 改成：抓「像……」到最近的标点为止，再去掉虚词开头，只留 3–14 字的整句。
_METAPHOR_RE = re.compile(r"像([^。！？，,；;\n]{3,14})")
_META_STOP = ("一条", "一只", "一个", "这个", "那个", "是被", "是")


def _ro(path: str):
    """只读连接；库不在就返回 None，绝不抛。"""
    if not path or not os.path.exists(path):
        return None
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True,
                            timeout=5)
        c.row_factory = sqlite3.Row
        return c
    except Exception:
        return None


def _rows(conn, sql, args=()):
    if conn is None:
        return []
    try:
        return conn.execute(sql, args).fetchall()
    except Exception:
        return []


def _iso(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return ""


# ---------------------------------------------------------------- A 基础性格
def block_personality(data_db=None, persona="大肥鱼"):
    data_db = data_db or DATA_DB
    c = _ro(data_db)
    prompt, chars = "", 0
    for r in _rows(c, "SELECT system_prompt FROM personas WHERE persona_id=?",
                   (persona,)):
        prompt = r["system_prompt"] or ""
        chars = len(prompt)
    if c:
        c.close()

    # 从人设正文里抽「她说话时用的词」不是重点；重点是**人设管到哪些维度**
    dims = []
    for kw, label in (("温柔", "温柔"), ("照顾", "照顾"), ("啰嗦", "话多"),
                      ("撒娇", "撒娇"), ("正经", "正经"), ("陪伴", "陪伴"),
                      ("鲸", "鲸鱼意象"), ("女儿", "女儿身份")):
        if kw in prompt:
            dims.append(label)

    c2 = _ro(AUTO_DB)
    vers = []
    for r in _rows(c2, "SELECT name,traits,note,created_at FROM persona_versions "
                       "ORDER BY id"):
        vers.append({"at": _iso(r["created_at"]), "name": r["name"],
                     "traits": r["traits"], "note": r["note"]})
    if c2:
        c2.close()

    return {"prompt_chars": chars, "dimensions": dims,
            "versions": vers, "version_n": len(vers),
            "src": "data_v4.db/personas + autonomy.db/persona_versions"}


# ---------------------------------------------------------------- B 价值观
def block_values(limit=8):
    out = []
    c = _ro(AUTO_DB)

    # 1) 她自己提过的人设意见 —— 这是「她真信什么」最硬的证据
    for r in _rows(c, "SELECT id,op,opinion,find_txt,replace_txt,status,created_at "
                      "FROM persona_patches ORDER BY id"):
        body = (r["opinion"] or "").strip()
        if r["op"] == "patch":
            body = "把「%s」改成「%s」" % (r["find_txt"] or "", r["replace_txt"] or "")
        if body:
            out.append({"kind": "她自己提的", "text": body,
                        "status": r["status"], "at": _iso(r["created_at"]),
                        "src": "persona_patches#%s" % r["id"]})

    # 2) 自传里认账的句子 —— 不是「我会努力」，是「我承认没弄明白」
    for r in _rows(c, "SELECT chapter,body,created_at FROM autobiography "
                      "ORDER BY chapter"):
        for sent in re.split(r"[。！？\n]", r["body"] or ""):
            s = sent.strip()
            if len(s) >= 8 and any(k in s for k in _ACK):
                out.append({"kind": "自传里认了的", "text": s,
                            "at": _iso(r["created_at"]),
                            "src": "autobiography ch%s" % r["chapter"]})
    if c:
        c.close()
    return {"items": out[:limit], "n": len(out),
            "src": "persona_patches(opinion) + autobiography(认账句)"}


# ---------------------------------------------------------------- C 世界观
def block_worldview(lm_db=None, limit=8):
    lm_db = lm_db or LM_HINT
    """成对：知道的（技能正文） vs 不知道的（缺口）。"""
    known, unknown = [], []
    c = _ro(lm_db)
    for r in _rows(c, "SELECT id,name,body,mastery,uses,COALESCE(evidence,'') AS ev "
                      "FROM procedures ORDER BY mastery DESC LIMIT ?", (limit,)):
        known.append({"id": r["id"], "what": r["name"], "how": r["body"],
                      "mastery": round(float(r["mastery"] or 0), 2),
                      "used": r["uses"], "evidence": r["ev"],
                      "src": "procedures#%s" % r["id"]})
    if c:
        c.close()

    c2 = _ro(AUTO_DB)
    for r in _rows(c2, "SELECT id,topic,tries,status,note,created_at FROM gaps "
                       "ORDER BY id"):
        unknown.append({"id": r["id"], "about": r["topic"], "tries": r["tries"],
                        "status": r["status"], "note": r["note"],
                        "at": _iso(r["created_at"]), "src": "gaps#%s" % r["id"]})
    if c2:
        c2.close()

    return {"known": known, "unknown": unknown,
            "balance": "%d 件会做 / %d 件承认不会" % (len(known), len(unknown)),
            "src": "procedures + gaps（必须成对看）"}


# ---------------------------------------------------------------- D 兴趣审美
def block_interests(lm_db=None, limit=10):
    lm_db = lm_db or LM_HINT
    c = _ro(AUTO_DB)
    wanted = []
    for r in _rows(c, "SELECT id,title,kind,status,created_at FROM intentions "
                      "WHERE kind='interest' OR kind='curiosity' ORDER BY id"):
        wanted.append({"id": r["id"], "about": r["title"], "kind": r["kind"],
                       "status": r["status"], "at": _iso(r["created_at"]),
                       "src": "intentions#%s" % r["id"]})

    # 选择记录：她真挑了什么、为什么不挑别的 —— 比自陈可靠
    picks = []
    for r in _rows(c, "SELECT id,chosen_title,why_this,picked_by,created_at "
                      "FROM picks ORDER BY id"):
        picks.append({"at": _iso(r["created_at"]), "chose": r["chosen_title"],
                      "why": r["why_this"], "by": r["picked_by"],
                      "src": "picks#%s" % r["id"]})
    if c:
        c.close()

    # 日记：**不做比喻抽取**。
    # 试过两版正则，抽出来都是半句话（「鱼在玻璃缸里找根本不存在」
    # 「在邀功或者诉苦」），拿去当"审美"会误导。这里只给客观量：
    # 写了多少天、一共多少字、最近一句原话。
    ddir = DIARY_DIR
    days, total, latest, latest_at = 0, 0, "", ""
    if os.path.isdir(ddir):
        for fn in sorted(os.listdir(ddir)):
            if not fn.endswith(".md"):
                continue
            try:
                txt = open(os.path.join(ddir, fn), encoding="utf-8").read()
            except Exception:
                continue
            days += 1
            total += len(txt)
            tail = [s.strip() for s in re.split(r"[。！？\n]", txt) if len(s.strip()) >= 6]
            if tail:
                latest = tail[-1][:70]
                latest_at = fn[:-3]

    return {"wanted": wanted[:limit], "picks": picks,
            "diary": {"days": days, "chars": total,
                      "latest_at": latest_at, "latest_line": latest},
            "note": "「审美」目前没有可靠数据源 —— 日记比喻抽取试过两版都抽不准，已放弃；"
                    "偏好只能从 picks（她真挑了什么）间接看。",
            "src": "intentions(interest/curiosity) + picks + diary 统计"}


# ---------------------------------------------------------------- E 成长轴
def block_timeline(limit=20):
    ev = []
    c = _ro(AUTO_DB)
    for r in _rows(c, "SELECT chapter,body,src_count,created_at FROM autobiography "
                      "ORDER BY chapter"):
        first = (re.split(r"[。！？\n]", r["body"] or "")[0] or "")[:60]
        ev.append({"at": _iso(r["created_at"]), "kind": "自传",
                   "what": "第%s章写完了" % r["chapter"], "brief": first,
                   "n": r["src_count"], "src": "autobiography ch%s" % r["chapter"]})
    for r in _rows(c, "SELECT body,care_about,src_count,created_at FROM reflections "
                      "ORDER BY id"):
        ev.append({"at": _iso(r["created_at"]), "kind": "想明白",
                   "what": "夜里回看了 %s 条记忆" % (r["src_count"] or "?"),
                   "brief": (r["body"] or "")[:60],
                   "src": "reflections"})
    for r in _rows(c, "SELECT name,note,created_at FROM persona_versions ORDER BY id"):
        ev.append({"at": _iso(r["created_at"]), "kind": "人格快照",
                   "what": r["name"], "brief": r["note"],
                   "src": "persona_versions"})
    if c:
        c.close()
    ev.sort(key=lambda x: x["at"])
    return {"events": ev[-limit:], "n": len(ev),
            "src": "autobiography + reflections + persona_versions 按时间排"}


# ---------------------------------------------------------------- F 认知对象
def block_people(lm_db=None, limit=8):
    lm_db = lm_db or LM_HINT
    """graph_nodes 上**没有** degree 列（踩过），度数要从 graph_edges 现算。"""
    c = _ro(lm_db)
    ppl = []
    sql = ("SELECT n.node_value AS who, COUNT(e.id) AS deg "
           "FROM graph_nodes n LEFT JOIN graph_edges e "
           "  ON e.source_node_id = n.id OR e.target_node_id = n.id "
           "WHERE n.node_type='person' GROUP BY n.id "
           "ORDER BY deg DESC LIMIT ?")
    for r in _rows(c, sql, (limit,)):
        ppl.append({"who": r["who"], "weight": r["deg"]})
    if c:
        c.close()
    return {"top": ppl,
            "src": "livingmemory.graph_nodes + graph_edges（度数现算）"}


# ---------------------------------------------------------------- G 生命状态
def vitality(vitals=None, warmth=None, attention=None, last_reflect_at=0):
    """把身体/心情/注意力算成两个**能直接回答问题的**派生量。

    energy  : 现在适不适合让她干复杂活
    mood_span: 现在这个心情是短暂波动还是长期倾向

    不建表、不存状态 —— 每次现算，跟 rhythm.vitals() 一个思路。
    """
    v = vitals or {}
    circa = float(v.get("circadian") or 0.5)
    arous = float(v.get("arousal") or 0.5)
    stage = v.get("stage") or "awake"
    wal = 0.85 if warmth is None else float(warmth)

    # 精力：昼夜 × 唤醒，睡着时几乎归零
    energy = arous * (1.0 - (1.0 - circa) * 0.4)
    if stage != "awake":
        energy *= 0.2
    energy = max(0.0, min(1.0, energy))
    if energy >= 0.60:
        fit = "适合做复杂的事"
    elif energy >= 0.35:
        fit = "勉强，别给太绕的活"
    else:
        fit = "现在不适合，简单回应就行"

    # 心境：跟 0.80 这个基准比；离得远 + 持续久 = 长期倾向
    dev = wal - 0.80
    if abs(dev) < 0.08:
        span = "稳定"
    elif abs(dev) < 0.20:
        span = "有波动，但还在正常范围"
    else:
        span = "偏向%s，不是一时的" % ("黏" if dev > 0 else "冷")

    att = (attention or {})
    return {
        "energy": round(energy, 2), "fit": fit,
        "mood": round(wal, 2), "mood_span": span,
        "band": v.get("band"), "stage": stage,
        "arousal": round(arous, 2), "circadian": round(circa, 2),
        "hr": v.get("hr"), "rr": v.get("rr"), "lag": v.get("lag"),
        "attention": {"mode": att.get("mode"), "cn": att.get("cn"),
                      "since_s": att.get("since_s")},
        "src": "rhythm.vitals + warmth + attention 现算",
    }


# ---------------------------------------------------------------- 汇总
# ───────────── vitals_log：心情/精力的历史（trend 用） ─────────────
_VLOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS vitals_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    warmth REAL DEFAULT 0, energy REAL DEFAULT 0,
    hr REAL DEFAULT 0, rr REAL DEFAULT 0, lag REAL DEFAULT 0,
    arousal REAL DEFAULT 0, circadian REAL DEFAULT 0,
    stage TEXT DEFAULT 'awake', sleeping INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vlog_at ON vitals_log(at);
"""


def _rw(path):
    """可写连接；库里没这张表就建。"""
    if not path or not os.path.exists(path):
        return None
    c = sqlite3.connect(path, timeout=10.0)
    c.execute("PRAGMA busy_timeout = 10000")
    c.execute("PRAGMA temp_store = MEMORY")
    for stmt in filter(None, (x.strip() for x in _VLOG_SCHEMA.split(";"))):
        try:
            c.execute(stmt)
        except Exception:
            pass
    return c


def log_vitals(vitals: dict, warmth=None, energy=None, keep_days: int = 30) -> bool:
    """记一行。同一分钟内只留一条（10 分钟一轮，理论不会撞，防御一下）。"""
    c = _rw(AUTO_DB)
    if c is None:
        return False
    try:
        v = vitals or {}
        now = time.time()
        row = c.execute("SELECT id FROM vitals_log WHERE at > ? ORDER BY at DESC LIMIT 1",
                        (now - 60,)).fetchone()
        vals = (now,
                float(warmth if warmth is not None else 0),
                float(energy if energy is not None else 0),
                float(v.get("hr") or 0), float(v.get("rr") or 0), float(v.get("lag") or 0),
                float(v.get("arousal") or 0), float(v.get("circadian") or 0),
                str(v.get("stage") or "awake"), 1 if v.get("sleeping") else 0)
        if row:
            c.execute("UPDATE vitals_log SET at=?,warmth=?,energy=?,hr=?,rr=?,lag=?,"
                      "arousal=?,circadian=?,stage=?,sleeping=? WHERE id=?",
                      vals + (row[0],))
        else:
            c.execute("INSERT INTO vitals_log(at,warmth,energy,hr,rr,lag,arousal,"
                      "circadian,stage,sleeping) VALUES(?,?,?,?,?,?,?,?,?,?)", vals)
        c.execute("DELETE FROM vitals_log WHERE at < ?", (now - keep_days * 86400,))
        c.commit()
        return True
    except Exception:
        return False
    finally:
        try:
            c.close()
        except Exception:
            pass


def trend(days: int = 7) -> dict:
    """按天聚合。返回 [{d:'11-03', w:0.8, e:0.49, n:36}, ...]，缺的日子 w/e 为 None。"""
    c = _ro(AUTO_DB)
    if c is None:
        return {"days": [], "filled": 0, "note": "还没有历史"}
    try:
        now = time.time()
        start = now - (days - 1) * 86400
        rows = c.execute(
            "SELECT date(at,'unixepoch','localtime') d, AVG(warmth) w, AVG(energy) e, "
            "COUNT(*) n FROM vitals_log WHERE at >= ? GROUP BY d ORDER BY d",
            (start,)).fetchall()
        got = {}
        for r in rows:
            got[str(r["d"])] = {"w": round(float(r["w"] or 0), 3),
                                "e": round(float(r["e"] or 0), 3),
                                "n": int(r["n"] or 0)}
        out = []
        for i in range(days - 1, -1, -1):
            dt = datetime.datetime.fromtimestamp(now - i * 86400)
            g = got.get(dt.strftime("%Y-%m-%d"))
            out.append({"d": dt.strftime("%m-%d"),
                        "w": g["w"] if g else None,
                        "e": g["e"] if g else None,
                        "n": g["n"] if g else 0})
        filled = sum(1 for x in out if x["w"] is not None)
        return {"days": out, "filled": filled,
                "note": "" if filled >= 2 else "从今天开始记录，攒够两天才连线"}
    except Exception:
        return {"days": [], "filled": 0, "note": "读取失败"}
    finally:
        try:
            c.close()
        except Exception:
            pass



# ---------------------------------------------------------------- H 信念
def block_beliefs(limit=12):
    """她自己认准的判断 —— 不是发生过什么，是她现在认为什么是对的。"""
    items, ev = [], []
    c = _ro(AUTO_DB)
    for r in _rows(c, "SELECT id,belief,confidence,origin,status,created_at,updated_at "
                      "FROM beliefs WHERE status='active' "
                      "ORDER BY confidence DESC, id DESC LIMIT ?", (limit,)):
        items.append({"id": r["id"], "what": r["belief"],
                      "conf": round(float(r["confidence"] or 0), 2),
                      "origin": r["origin"],
                      "since": _iso(r["created_at"]),
                      "src": "beliefs#%s" % r["id"]})
    for r in _rows(c, "SELECT belief_id,side,content,source,at FROM belief_evidence "
                      "ORDER BY id DESC LIMIT 40"):
        ev.append({"id": r["belief_id"], "side": r["side"],
                   "text": r["content"], "at": _iso(r["at"])})
    stat = {"n": len(items), "strong": 0, "avg": 0.0}
    try:
        row = _rows(c, "SELECT COUNT(*) AS n, "
                       "SUM(CASE WHEN confidence>=0.75 THEN 1 ELSE 0 END) AS s, "
                       "AVG(confidence) AS a FROM beliefs WHERE status='active'")[0]
        stat = {"n": int(row["n"] or 0), "strong": int(row["s"] or 0),
                "avg": round(float(row["a"] or 0), 2)}
    except Exception:
        pass
    if c:
        c.close()
    return {"items": items, "evidence": ev, "stat": stat,
            "src": "beliefs（写自传时顺带产出；15 天不起作用会自己变淡）"}


# ---------------------------------------------------------------- I 自我模型
def block_self(limit=3):
    """最新的几版「我是个什么样的人」，只增不改，能看到她怎么变的。"""
    items = []
    c = _ro(AUTO_DB)
    for r in _rows(c, "SELECT id,version,payload,trigger,reason,created_at "
                      "FROM self_model ORDER BY version DESC LIMIT ?", (limit,)):
        try:
            p = json.loads(r["payload"] or "{}")
        except Exception:
            p = {}
        vals = [v.get("belief") for v in (p.get("value") or [])
                if isinstance(v, dict) and v.get("belief")]
        items.append({"version": r["version"],
                      "summary": p.get("summary") or "",
                      "values": vals,
                      "limit": [str(x) for x in (p.get("limit") or [])],
                      "diff": p.get("diff") or {},
                      "mood": p.get("mood") or {},
                      "stats": p.get("belief_stats") or {},
                      "trigger": r["trigger"], "reason": r["reason"],
                      "at": _iso(r["created_at"])})
    if c:
        c.close()
    return {"items": items, "n": len(items),
            "src": "self_model（信念指纹变了才重算，只增不改）"}


def build(lm_db=None, persona="大肥鱼"):
    lm_db = lm_db or LM_HINT
    return {
        "trend": trend(7),
        "at": _iso(time.time()),
        "personality": block_personality(persona=persona),
        "values": block_values(),
        "worldview": block_worldview(lm_db),
        "interests": block_interests(lm_db),
        "timeline": block_timeline(),
        "people": block_people(lm_db),
        "vitality": vitality(),
        "beliefs": block_beliefs(),
        "self": block_self(),
    }


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
