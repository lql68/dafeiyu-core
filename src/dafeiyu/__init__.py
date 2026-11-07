"""数字生命内核。

八块，全部只用标准库（json / sqlite3 / math / datetime / hashlib / re），
不需要任何 API key，也不需要 AstrBot / QQ / 模型服务。

    rhythm      生理节律 —— 几点犯困、反应快慢、睡着了没有
    selfstory   自传与信念 —— 她认为什么是对的、写下的章节、反证与衰减
    selfmodel   自我模型 —— 「我现在觉得自己是个什么样的人」
    autonomy    自主性 —— 意图 / 问题池 / 认知会话 / 自我模型 / 日记快照
    growth      成长面板 —— 九块只读聚合，不做任何写入
    perm        权限 —— 五档权限，她能不能改自己的代码
    school      技能学校 —— 从对话里攒出「下次该怎么做」
    tuning      参数覆盖 —— 不改代码调阈值

依赖：`school` 真正落库时需要 `aiosqlite`（`pip install dafeiyu-core[asy]`），
其余七块零依赖。

最小用法：

    from dafeiyu import rhythm

    v = rhythm.vitals(warmth=0.85, att_mode="outward", quiet_s=12)
    # {'circadian': 0.72, 'band': '晚上', 'arousal': 0.58,
    #  'hr': 77, 'rr': 15.1, 'lag': 1.31, 'sleeping': False, 'stage': 'awake'}

    rhythm.hint(v)
    # '晚上，身体最舒服的时候，也是最想跟他多聊会儿的时候。'

要用带库的模块（selfstory / selfmodel / autonomy / school / growth），
先把库的位置告诉它们：

    from dafeiyu import selfstory, growth

    story = selfstory.SelfStory(r"D:\\somewhere\\autonomy.db")
    growth.configure(plugin_dir=r"D:\\somewhere", data_db=r"D:\\x\\data_v4.db")
"""

from __future__ import annotations

from .kernel import (
    autonomy,
    growth,
    perm,
    rhythm,
    school,
    selfmodel,
    selfstory,
    tuning,
)

__version__ = "0.2.0"

__all__ = [
    "rhythm",
    "selfstory",
    "selfmodel",
    "autonomy",
    "growth",
    "perm",
    "school",
    "tuning",
    "__version__",
]
