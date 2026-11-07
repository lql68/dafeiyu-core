"""信念与自传的单元测试。

用临时 sqlite 文件，跑完就删 —— 不碰任何线上数据。

这里的数字全是真跑出来的：
  - 「他难受的时候先别给建议」和「他难受时先别急着给建议」Jaccard 只有 0.33，
    当初就是差了 0.01 让两条一样的话各自入库。所以加了包含度兜底。
  - 反证一次 ×0.6，两次从 0.60 打到 0.216，跌破 0.30 标 retired。
"""

import os
import tempfile
import time

import pytest

from dafeiyu import selfstory


@pytest.fixture()
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = selfstory.SelfStory(path)
    s.init()
    yield s
    try:
        os.unlink(path)
    except OSError:
        pass


class TestBeliefText:
    def test_bigrams_are_2char(self):
        assert selfstory._bigrams("他难受") == {"他难", "难受"}

    def test_bigrams_empty(self):
        assert selfstory._bigrams("") == set()

    def test_short_sentence_no_grams(self):
        # 长度 1 的串没有 2-gram
        assert selfstory._bigrams("嗯") == {"嗯"}


class TestBeliefsSimilar:
    """Jaccard 对长短句不公平 —— 短句的 gram 大半落在长句里，但并集也被拉大。"""

    def test_same_sentence(self):
        assert selfstory.beliefs_similar("他难受的时候先别给建议", "他难受的时候先别给建议")

    def test_reworded_should_match(self):
        # 这一对 Jaccard = 0.33，只靠 Jaccard(thr=0.34) 会漏
        a = "他难受的时候先别给建议"
        b = "他难受时先别急着给建议"
        assert selfstory.beliefs_similar(a, b)

    def test_different_should_not_match(self):
        assert not selfstory.beliefs_similar("陪着他折腾小事比说漂亮话实在", "记住号码不等于问清了关系")

    def test_empty_is_false(self):
        assert not selfstory.beliefs_similar("", "随便一句话")


class TestUpsert:
    def test_insert_new(self, store):
        r = store.upsert_belief("陪着他折腾这些小事比说漂亮话实在", origin="test")
        assert r.get("id")
        assert store.list_beliefs()[0]["belief"].startswith("陪着他")

    def test_too_short_rejected(self, store):
        assert store.upsert_belief("嗯", origin="test") == {}

    def test_similar_merges_and_boosts(self, store):
        a = store.upsert_belief("他难受的时候先别给建议", confidence=0.55)
        b = store.upsert_belief("他难受时先别急着给建议", confidence=0.55)
        # 应该合并，不是新增
        assert len(store.list_beliefs()) == 1
        assert b["confidence"] > a["confidence"]

    def test_confidence_capped(self, store):
        t = "陪着他折腾这些小事比说漂亮话实在"
        for _ in range(20):
            store.upsert_belief(t, confidence=0.9)
        assert store.list_beliefs()[0]["confidence"] <= 0.95


class TestContradict:
    def test_halves_then_retires(self, store):
        store.upsert_belief("他总是嫌我话多", confidence=0.60)
        r1 = store.contradict_belief("以前以为他总是嫌我话多，其实他只是累了", source="ch1")
        assert r1 and r1["confidence_after"] < 0.60
        assert r1["status"] == "active"

        r2 = store.contradict_belief("以前以为他总是嫌我话多，其实他只是累了", source="ch2")
        assert r2["confidence_after"] < 0.30
        assert r2["status"] == "retired"

    def test_no_match_returns_empty(self, store):
        store.upsert_belief("陪着他折腾这些小事比说漂亮话实在", confidence=0.6)
        # 完全不相干的反证，不该随便打掉谁
        assert store.contradict_belief("以前以为太阳是方的，其实是圆的") == {}

    def test_against_evidence_recorded(self, store):
        store.upsert_belief("他总是嫌我话多", confidence=0.60)
        r = store.contradict_belief("以前以为他总是嫌我话多，其实他只是累了", source="ch1")
        ev = store.belief_evidence(r["id"])
        assert any(e["side"] == "against" for e in ev)


class TestDecay:
    def test_within_15_days_untouched(self, store):
        store.upsert_belief("陪着他折腾这些小事比说漂亮话实在", confidence=0.60)
        out = store.decay_beliefs()
        assert out == [] or all(x["confidence"] == pytest.approx(0.60, abs=0.01) for x in out)

    def test_16_days_drops(self, store):
        bid = store.upsert_belief("陪着他折腾这些小事比说漂亮话实在", confidence=0.60)["id"]
        # 把 updated_at 和证据时间都挪到 16 天前
        import sqlite3

        with sqlite3.connect(store.db_path) as c:
            old = time.time() - 16 * 86400
            c.execute("UPDATE beliefs SET updated_at = ? WHERE id = ?", (old, bid))
            c.execute("UPDATE belief_evidence SET at = ? WHERE belief_id = ?", (old, bid))
        out = store.decay_beliefs()
        assert out and out[0]["to"] < 0.60

    def test_floor_holds(self, store):
        bid = store.upsert_belief("陪着他折腾这些小事比说漂亮话实在", confidence=0.40)["id"]
        import sqlite3

        with sqlite3.connect(store.db_path) as c:
            old = time.time() - 400 * 86400
            c.execute("UPDATE beliefs SET updated_at = ? WHERE id = ?", (old, bid))
            c.execute("UPDATE belief_evidence SET at = ? WHERE belief_id = ?", (old, bid))
        store.decay_beliefs()
        got = [b for b in store.list_beliefs() if b["id"] == bid]
        if got:
            assert got[0]["confidence"] >= selfstory.DECAY_FLOOR - 1e-6

    def test_touch_delays_decay(self, store):
        bid = store.upsert_belief("陪着他折腾这些小事比说漂亮话实在", confidence=0.60)["id"]
        import sqlite3

        with sqlite3.connect(store.db_path) as c:
            old = time.time() - 16 * 86400
            c.execute("UPDATE beliefs SET updated_at = ? WHERE id = ?", (old, bid))
        store.touch_belief(bid)  # 用上了，刷新时间
        store.decay_beliefs()
        got = [b for b in store.list_beliefs() if b["id"] == bid][0]
        assert got["confidence"] == pytest.approx(0.60, abs=0.01)


class TestSplitBeliefs:
    def test_no_tag_returns_body_only(self):
        body, new, against = selfstory.split_beliefs("今天有点累。")
        assert body == "今天有点累。"
        assert new == [] and against == []

    def test_belief_tag(self):
        text = "正文。\n【我现在认为】\n- 陪着他比说漂亮话实在\n- 答应做不到不如直说"
        body, new, against = selfstory.split_beliefs(text)
        assert "【我现在认为】" not in body
        assert len(new) == 2
        assert against == []

    def test_against_tag(self):
        text = "正文。\n【我以前想错了】\n- 以前以为他只是客气，其实他真在意"
        body, new, against = selfstory.split_beliefs(text)
        assert len(against) == 1
        assert new == []

    def test_both_tags(self):
        text = "正文。\n【我现在认为】\n- 新的判断\n【我以前想错了】\n- 以前以为 X 其实 Y"
        body, new, against = selfstory.split_beliefs(text)
        assert len(new) == 1 and len(against) == 1


class TestChapters:
    def test_add_and_list(self, store):
        store.add_chapter("第一章的正文", mood={"v": -0.3, "top": "委屈"})
        store.add_chapter("第二章的正文")
        assert store.count() == 2
        chs = store.list_chapters()
        assert len(chs) == 2

    def test_digest_mentions_mood(self, store):
        store.add_chapter("有点难受的一天", mood={"v": -0.4, "top": "委屈"})
        d = store.digest()
        assert "委屈" in d
