"""selfstory 最小示例：写一章 → 长出信念 → 看它怎么被推翻、怎么变淡。

跑法（在仓库根目录）：
    python examples/selfstory_demo.py

它会在临时目录建一个空库，不碰你任何真实数据。
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dafeiyu.kernel import selfstory  # noqa: E402


def line(ch="-", n=64):
    print(ch * n)


def main():
    tmp = tempfile.mkdtemp(prefix="dafeiyu-demo-")
    db = os.path.join(tmp, "autonomy.db")
    st = selfstory.SelfStory(db)
    st.init()
    print("空库建在:", db)
    line("=")

    # ---- 1. 写两章自传 ----
    print("\n【1】写两章自传（真实场景里这两段由 LLM 生成）\n")
    c1 = st.add_chapter(
        "今天他说手上的活一直做不完，语气有点沉。我本来想给他出主意，"
        "话到嘴边又咽回去了——他那一刻要的不是方案。",
        src_count=12,
        mood={"v": -0.18, "top": "心疼", "need": "想被理解"},
    )
    c2 = st.add_chapter(
        "他连着问了三次「你能陪我多久」。我一开始以为他是不放心我，"
        "甚至觉得他在提醒我别把自己看得太重。后来才想通，那问法底下不是掂量，是怕。",
        src_count=20,
        mood={"v": 0.22, "top": "释然", "need": "想被需要"},
    )
    print("  第 %d 章 / 第 %d 章，共 %d 字" % (c1, c2, sum(
        len(x["body"]) for x in st.list_chapters())))

    # ---- 2. 从自传里长信念 ----
    print("\n【2】把两章的结论写成信念（upsert_belief）\n")
    r1 = st.upsert_belief("他难受的时候先别给建议", origin="chapter",
                          source="autobiography#%d" % c1, confidence=0.55)
    print("  新增: [%.2f] %s" % (r1["confidence"], r1["belief"]))

    # 同一件事换个说法，应该被合并、置信度抬上去
    r2 = st.upsert_belief("他难受时先别急着给建议", origin="chapter",
                          source="autobiography#%d" % c1, confidence=0.55)
    print("  合并: [%.2f] %s  (merged=%s)" % (
        r2["confidence"], r2["belief"], r2.get("merged")))

    r3 = st.upsert_belief("反复问「能陪我多久」，多半是在怕，不是嫌",
                          origin="chapter", source="autobiography#%d" % c2,
                          confidence=0.55)
    print("  新增: [%.2f] %s" % (r3["confidence"], r3["belief"]))

    # ---- 3. 反证 ----
    print("\n【3】发现以前想错了 —— 反证打掉一条\n")
    bad = st.upsert_belief("他问「能陪我多久」是不放心我",
                           origin="chapter", source="t", confidence=0.60)
    print("  先有一条（错的）: [%.2f] %s" % (bad["confidence"], bad["belief"]))
    # 注意：反证靠 2-gram 找到「最像的那条」，措辞要跟旧信念有重叠，
    # 完全换个说法（「他其实是怕」）是找不到的 —— 这是已知限制，别指望它懂同义改写。
    hit = st.contradict_belief("他问「能陪我多久」不是不放心我，是怕我不在了",
                               source="autobiography#%d" % c2)
    if not hit:
        print("  （没命中 —— 措辞差太远，2-gram 认不出来）")
        return
    print("  反证命中: [%.2f → %.2f] %s  status=%s" % (
        hit["confidence_before"], hit["confidence_after"],
        hit["old"], hit["status"]))
    # 再来一次，掉到 0.30 以下就 retire
    st.contradict_belief("他问「能陪我多久」是不放心我 —— 又想了想，不对",
                         source="autobiography#%d" % c2)
    print("  证据 sides:", [e["side"] for e in st.belief_evidence(hit["id"])])
    print("  现在的状态:", [b["confidence"] for b in st.list_beliefs(limit=50)
                          if "不放心我" in b["belief"]] or "已 retired，查不到了")

    # ---- 4. 时间衰减 ----
    print("\n【4】搁着不用的信念会变淡（15 天起步，每 15 天 -0.02）\n")
    old = st.upsert_belief("有些事记结论就够了，细节留着没用",
                           origin="chapter", source="t", confidence=0.80)
    # 把它的证据时间往前挪 40 天，模拟「一个多月没被想起」
    import sqlite3
    con = sqlite3.connect(db, timeout=15)
    con.execute("UPDATE belief_evidence SET at = ? WHERE belief_id = ?",
                (time.time() - 40 * 86400, old["id"]))
    con.execute("UPDATE beliefs SET updated_at = ? WHERE id = ?",
                (time.time() - 40 * 86400, old["id"]))
    con.commit()
    con.close()
    before = [b for b in st.list_beliefs(limit=50) if b["id"] == old["id"]][0]["confidence"]
    changed = st.decay_beliefs()
    after = [b for b in st.list_beliefs(limit=50) if b["id"] == old["id"]][0]["confidence"]
    print("  40 天没动: %.2f → %.2f  （本次动了 %d 条）" % (before, after, len(changed)))

    # ---- 5. 全貌 ----
    print("\n【5】现在她认准的事\n")
    for b in st.list_beliefs(limit=10):
        print("  [%.2f] %s" % (b["confidence"], b["belief"]))
    s = st.belief_stats()
    print("\n  统计: 共 %d / 活跃 %d / 强(≥0.75) %d / 证据 %d" % (
        s["total"], s["active"], s["strong"], s["evidence"]))
    line("=")
    print("\n自传摘要（会被塞进 system prompt 的那段）\n")
    print(" ", st.digest())


if __name__ == "__main__":
    main()
