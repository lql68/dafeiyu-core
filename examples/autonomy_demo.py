"""autonomy 最小示例：提一件事 → 变成问题 → 想过一遍 → 自己注意到什么。

跑法（在仓库根目录）：
    python examples/autonomy_demo.py

临时目录建空库，不碰你任何真实数据。
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dafeiyu.kernel import autonomy  # noqa: E402


def line(ch="-", n=64):
    print(ch * n)


def main():
    tmp = tempfile.mkdtemp(prefix="dafeiyu-auto-")
    db = os.path.join(tmp, "autonomy.db")
    st = autonomy.IntentionStore(db)
    print("空库建在:", db)
    line("=")

    # ---- 1. 意图：先提出来，不直接开做 ----
    print("\n【1】提三件事（status 先是 proposed，不是马上做）\n")
    i1 = st.add("explore", "查清楚那个按钮到底开没开",
                why="他说设成 true 了，但我不确定有没有真的生效",
                first_step="先看配置文件有没有被重载")
    i2 = st.add("act", "跟他说一声我今天想早点歇",
                why="我已经困了，再撑着说话会越来越短")
    i3 = st.add("plan", "把这几天的对话整理成一条时间线",
                why="我总觉得记了不少，但串不起来",
                first_step="先按日期把记忆分桶")
    for i in (i1, i2, i3):
        row = [x for x in st.list(limit=10) if x["id"] == i][0]
        print("  #%d [%-9s] %-22s status=%s" % (
            row["id"], row["kind"], row["title"], row["status"]))

    # ---- 2. 问题池：试过没成的，记成缺口 ----
    print("\n【2】试过没成的事记成缺口（会按「想不想弄明白」排序）\n")
    st.add_gap("重载到底做没做", tries=3,
               note="看了两次配置都是 true，但行为没变",
               origin="try", interest=0.70, importance=0.80)
    st.add_gap("为什么他总在半夜说这些", tries=3,
               note="问了三次都被岔过去",
               origin="try", interest=0.55, importance=0.50)
    # 同一件事换个说法 —— 2-gram 应该合并成一条，不新增
    st.add_gap("重载到底做没做过", tries=1, origin="try")
    for g in st.list_gaps(status="open", limit=10):
        print("  #%-3d [%.2f] %-18s tries=%d" % (
            g["id"], (g["interest"] or 0.5) * (g["importance"] or 0.5),
            g["topic"], g["tries"]))
    top = st.pick_gap(min_score=0.30, within_hours=24.0)
    print("  → 现在最想弄明白的:", top.get("topic") if top else "（都问过了）")

    # ---- 3. 认知会话：想出几种可能，各说清怎么验证 ----
    print("\n【3】对一个缺口想过一遍（2-3 条假设 + 各自怎么验证）\n")
    cid = st.add_cognition(
        "重载到底做没做？",
        hypotheses=[
            {"h": "命令发出去了但没落到这个实例上",
             "check": "看进程 PID 和启动时间是不是在命令之后"},
            {"h": "命令根本没发出去",
             "check": "找发送记录或返回日志"},
            {"h": "重载过了，但那个开关是另一个配置文件管的",
             "check": "对比实际生效的配置路径"},
        ],
        best="更倾向第一条：设参数和重载是两件事",
        next_step="先看 PID 和启动时间",
        gap_id=top.get("id") if top else 0,
    )
    print("  存了一条 cognition #%d" % cid)
    # 过 6 小时之后才有资格被回访（这里把时间往前挪模拟）
    import sqlite3
    con = sqlite3.connect(db, timeout=15)
    con.execute("UPDATE cognition_sessions SET created_at = ? WHERE id = ?",
                (time.time() - 7 * 3600, cid))
    con.commit()
    con.close()
    q = st.pick_question()
    how = "回访旧问题" if q.get("revisit_id") else "想新问题"
    print("  pick_question() → %s #%s：%s" % (
        how, q.get("revisit_id") or q.get("gap_id"), q.get("question")))

    # 回访得出「还看不出来」，就挂成 unresolved，之后可以在对话里问他
    st.resolve_cognition(cid, "三条都还看不出来，得看日志才可能定", status="unresolved")
    un = st.pick_unresolved_cognition(min_age_s=0.0)
    print("  想不清楚的（可以拿来问他）:", (un or {}).get("question") or "（没有）")
    st.mark_cognition_asked(cid)
    print("  问过一次之后 12 小时内不会再提:",
          "是" if not st.pick_unresolved_cognition(min_age_s=0.0) else "否")

    # ---- 4. 观察：她自己注意到的，不是别人告诉的 ----
    print("\n【4】她自己注意到的（只记变化，不记「一直在」）\n")
    st.add_observation("presence", "他来了")
    st.add_observation("light", "灯暗下来了", dur=0)
    st.add_observation("brightness", "72% -> 38%")
    for o in st.recent_observations(hours=6, limit=6):
        print("  [%s] %s%s" % (
            o["kind"], o["detail"],
            "（%d 分钟）" % (o["dur"] / 60) if o.get("dur") else ""))

    # ---- 5. 回看：动作做完了 ≠ 想办的事办成了 ----
    print("\n【5】回看一件「以为做完了」的事\n")
    st.set_status(i1, "done")
    rv = st.pick_done_to_review(days=7.0)
    if rv:
        print("  待回看 #%d %s" % (rv["id"], rv["title"]))
        st.mark_reviewed(rv["id"], "只发了一次搜索，没真去读结果", still_done=False)
        row = [x for x in st.list(status="active", limit=10) if x["id"] == rv["id"]]
        print("  回看后状态: %s" % (row[0]["status"] if row else "?"))
        print("  进度: %s" % (row[0]["progress"] if row else ""))

    line("=")
    print("\n最后看看库里有几行\n")
    for name, n in (("intentions", len(st.list(limit=99))),
                    ("gaps", len(st.list_gaps(status="", limit=99))),
                    ("cognition", len(st.list_cognition(99))),
                    ("observations", len(st.recent_observations(hours=9999, limit=99)))):
        print("  %-14s %d" % (name, n))


if __name__ == "__main__":
    main()
