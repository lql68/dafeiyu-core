"""autonomy 的内核级行为：建表、意图、问题池、认知会话。

这个文件只测「不依赖 LLM / 不依赖 AstrBot」的部分 ——
DeepBeat 那些要 provider 的路径在插件里测。
"""
import json
import os
import sqlite3
import sys
import time

import pytest

from dafeiyu.kernel import autonomy


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "autonomy.db")
    store = autonomy.IntentionStore(p)
    yield store
    store.close() if hasattr(store, "close") else None


# ---------- 建表 ----------
class TestSchema:
    def test_tables_created(self, db):
        with sqlite3.connect(db.db_path) as c:
            names = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("intentions", "gaps", "cognition_sessions", "picks",
                  "reflections", "observations"):
            assert t in names, "缺表 %s" % t

    def test_gaps_has_migration_columns(self, db):
        with sqlite3.connect(db.db_path) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(gaps)")}
        # 这四列是三次迁移陆续加的，漏一个就会在线上炸
        for col in ("origin", "interest", "importance", "related", "asked_at"):
            assert col in cols, "gaps 缺列 %s" % col

    def test_cognition_has_answer_columns(self, db):
        with sqlite3.connect(db.db_path) as c:
            cols = {r[1] for r in c.execute(
                "PRAGMA table_info(cognition_sessions)")}
        for col in ("asked_at", "answer", "answer_at"):
            assert col in cols, "cognition_sessions 缺列 %s" % col


# ---------- 意图 ----------
class TestIntentions:
    def test_add_and_list(self, db):
        iid = db.add(kind="explore", title="看看日志里有什么", why="想知道")
        assert iid > 0
        rows = db.list(status="proposed")   # add() 默认 proposed，等 plan_next 挑
        assert any(r["id"] == iid for r in rows)

    def test_update_status(self, db):
        iid = db.add(kind="explore", title="试点小事")
        db.set_status(iid, "done")
        rows = db.list(status="done")
        assert any(r["id"] == iid for r in rows)

    def test_progress_review_roundtrip(self, db):
        iid = db.add(kind="explore", title="查个东西")
        db.set_status(iid, "done")
        todo = db.pick_done_to_review(days=7.0)
        assert todo and todo["id"] == iid
        db.mark_reviewed(iid, "只发了搜索，没真读结果。", still_done=False)
        with sqlite3.connect(db.db_path) as c:
            row = c.execute("SELECT status, progress, last_reviewed_at "
                            "FROM intentions WHERE id=?", (iid,)).fetchone()
        assert row[0] == "active", "没做成的应该退回 active"
        assert "没真读结果" in row[1]
        assert row[2] > 0


# ---------- 问题池 ----------
class TestGaps:
    def test_add_and_pick(self, db):
        gid = db.add_gap("他到底想不想让我主动开口",
                         origin="reflect", interest=0.7, importance=0.8)
        assert gid > 0
        got = db.pick_gap(min_score=0.3)
        assert got and got["id"] == gid
        assert got["interest"] * got["importance"] == pytest.approx(0.56, abs=1e-6)

    def test_low_score_not_picked(self, db):
        db.add_gap("低分的事", origin="reflect", interest=0.2, importance=0.2)
        assert db.pick_gap(min_score=0.3) in (None, {})

    def test_dedupe_by_gram(self, db):
        a = db.add_gap("他说的那个脚本到底卡在哪一步", origin="reflect")
        b = db.add_gap("他说的那个脚本到底卡在哪一步了", origin="reflect")
        # 2-gram 重合度够高 → 不新建，只加 tries
        with sqlite3.connect(db.db_path) as c:
            n = c.execute("SELECT COUNT(*) FROM gaps").fetchone()[0]
        assert n == 1, "近似问题应合并，实际 %d 条" % n

    def test_asked_at_cooldown(self, db):
        gid = db.add_gap("问过的事", origin="reflect",
                         interest=0.9, importance=0.9)
        db.mark_gap_asked(gid)
        assert db.pick_gap(min_score=0.3, within_hours=24) in (None, {})


# ---------- 认知会话 ----------
class TestCognition:
    def test_add_and_list(self, db):
        cid = db.add_cognition(
            question="他那天是不是不高兴",
            hypotheses=json.dumps([{"h": "是不高兴", "check": "语气"},
                                   {"h": "只是累了", "check": "时间"}]),
            best="更可能是累了", next_step="问他", gap_id=0)
        assert cid > 0
        rows = db.list_cognition()
        assert any(r["id"] == cid for r in rows)

    def test_pick_question_revisit_first(self, db):
        cid = db.add_cognition(question="老问题", hypotheses="[]")
        # 把 created_at 挪到 8 小时前，应优先被回访
        old = time.time() - 8 * 3600
        with sqlite3.connect(db.db_path) as c:
            c.execute("UPDATE cognition_sessions SET created_at=? WHERE id=?",
                      (old, cid))
        q = db.pick_question()
        assert q.get("revisit_id") == cid
    
    def test_answer_written_once(self, db):
        cid = db.add_cognition(question="问他的事", hypotheses="[]")
        db.mark_cognition_asked(cid)
        assert db.record_cognition_answer(cid, "嗯，那次我确实有点不高兴")
        # 第二次不该覆盖
        assert not db.record_cognition_answer(cid, "又一条不一样的回答")
        with sqlite3.connect(db.db_path) as c:
            row = c.execute("SELECT answer FROM cognition_sessions WHERE id=?",
                            (cid,)).fetchone()
        assert "确实有点不高兴" in row[0]
        assert "不一样" not in row[0]

    def test_short_answer_rejected(self, db):
        cid = db.add_cognition(question="问他的事", hypotheses="[]")
        db.mark_cognition_asked(cid)
        assert not db.record_cognition_answer(cid, "嗯")   # 太短当没答


# ---------- 观察 ----------
class TestObservations:
    def test_add_and_recent(self, db):
        db.add_observation("presence", "他来了")
        db.add_observation("brightness", "灯暗下来了（72% -> 38%）")
        rows = db.recent_observations(hours=6.0, limit=6)
        assert len(rows) == 2
        assert rows[-1]["kind"] in ("presence", "brightness")


# ---------- 权限 ----------
class TestPerm:
    def test_default_safe(self):
        from dafeiyu.kernel import perm
        assert perm.describe()["mode"] in ("safe", "full")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
