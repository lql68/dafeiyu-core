"""内核实现。

八块，互不 import（除了 selfmodel 直接读 selfstory 建的表、
autonomy 复用 perm / tuning）。这样想单独用哪块就搬哪块。
"""

from __future__ import annotations

from . import _state
from . import rhythm, selfstory, selfmodel, tuning, autonomy, growth, perm, school

__all__ = [
    "rhythm", "selfstory", "selfmodel", "tuning",
    "autonomy", "growth", "perm", "school",
    "configure", "state_dir",
]

# 宿主启动时调一次：内核自己不知道你把状态文件放哪
configure = _state.configure
state_dir = _state.state_dir
