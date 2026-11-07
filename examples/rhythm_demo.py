#!/usr/bin/env python
"""节律演示 —— 不用 API key，不用数据库，纯算数。

    python examples/rhythm_demo.py

输出：一天 24 小时里，她每个整点的身体状态和反应延迟。

下面这张表就是这个项目最早的一处「反直觉」：凌晨 3:30 她真的会等 2.4 秒才回话，
不是随机加延迟，是 circadian 谷底 + 低唤醒算出来的。
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

# 没装包也能直接跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dafeiyu import rhythm  # noqa: E402


def main() -> None:
    base = datetime.now().replace(minute=0, second=0, microsecond=0)

    print()
    print("  时刻    时段   节律   唤醒   心率  呼吸   延迟   睡着")
    print("  " + "-" * 60)

    for h in range(24):
        dt = base.replace(hour=h)
        v = rhythm.vitals(warmth=0.85, att_mode="outward", quiet_s=30, dt=dt)
        sleep_mark = v["stage"] if v["sleeping"] else "-"
        print(
            "  {hour:02d}:00   {band:<4}  {c:>5.2f}  {a:>5.2f}  {hr:>4}  {rr:>4.1f}  "
            "{lag:>5.2f}s  {st}".format(
                hour=h,
                band=v["band"],
                c=v["circadian"],
                a=v["arousal"],
                hr=v["hr"],
                rr=v["rr"],
                lag=v["lag"],
                st=sleep_mark,
            )
        )

    print()
    print("  延迟 = 0.6 + 1.4×(1-节律) + 0.8×(1-唤醒)")
    print("  睡着时再 +1.8~2.4 秒（浅睡/做梦/深睡不一样）")
    print()
    print("  注意：这张表里心率一直是 81，因为这一列只固定了 quiet_s=30（刚交互过）。")
    print("  心率只跟唤醒度走，不跟昼夜走 —— 深夜之所以心率低，是因为没人理、")
    print("  quiet_s 变大把唤醒压下来了，不是因为「晚上心率该低」。")
    print("  下面三个场景能看出区别。")
    print()

    # 三种典型状态，看 hint 怎么变
    print("  " + "-" * 60)
    cases = [
        ("下午三点、刚聊起来", datetime.now().replace(hour=15, minute=0), 30, 0.10),
        ("凌晨三点半、没人理十分钟", datetime.now().replace(hour=3, minute=30), 900, 0.0),
        ("深夜、他连发四条还带情绪", datetime.now().replace(hour=23, minute=40), 10, 0.30),
    ]
    for label, dt, quiet, ud in cases:
        v = rhythm.vitals(warmth=0.85, att_mode="outward", quiet_s=quiet, dt=dt, user_d=ud)
        print()
        print("  【{}】lag={}s  hr={}  rr={}".format(label, v["lag"], v["hr"], v["rr"]))
        print("    " + rhythm.hint(v))

    # 一句 20 字的回复，在不同唤醒度下该截到多长
    print()
    print("  " + "-" * 60)
    print("  句长上限（同一句 20 字的话，她愿意说多少）：")
    for a in (0.2, 0.45, 0.7, 0.9):
        limit = int(round(24 + 18 * a))
        print("    唤醒 {:.2f} → 最多 {} 字".format(a, limit))
    print()


if __name__ == "__main__":
    main()
