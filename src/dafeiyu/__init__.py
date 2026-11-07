"""数字生命内核。

四块，互相不依赖（除了 selfmodel 读 selfstory 的表）：

    rhythm      生理节律 —— 几点犯困、反应快慢、睡着了没有
    selfstory   自传与信念 —— 她认为什么是对的、写下的章节
    selfmodel   自我模型 —— 「我现在觉得自己是个什么样的人」
    tuning      参数覆盖 —— 不改代码调阈值

全部只用标准库（json / sqlite3 / math / datetime / hashlib），不需要任何 API key。

最小用法：

    from dafeiyu import rhythm

    v = rhythm.vitals(warmth=0.85, att_mode="outward", quiet_s=12)
    # {'circadian': 0.72, 'band': '晚上', 'arousal': 0.58,
    #  'hr': 77, 'rr': 15.1, 'lag': 1.31, 'sleeping': False, 'stage': 'awake'}

    rhythm.hint(v)
    # '晚上，身体最舒服的时候，也是最想跟他多聊会儿的时候。'
"""

from __future__ import annotations

from .kernel import rhythm, selfmodel, selfstory, tuning

__version__ = "0.1.0"

__all__ = [
    "rhythm",
    "selfstory",
    "selfmodel",
    "tuning",
    "__version__",
]
