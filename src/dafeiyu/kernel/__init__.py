"""内核实现。

拆成四个独立模块，互不 import（除了 selfmodel 直接读 selfstory 建的表）。
这样做的好处：想单独用哪块就搬哪块，不用连带整个包。
"""

from __future__ import annotations

__all__ = ["rhythm", "selfstory", "selfmodel", "tuning"]
