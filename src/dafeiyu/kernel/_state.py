"""给 dafeiyu.kernel 里的「会写状态文件」的模块指定状态目录。

搬进 pip 包之后，`os.path.dirname(__file__)` 指向 site-packages，
状态文件会被写进虚拟环境里 —— 那是别人的目录，重装就没了。

宿主要做的只有一件事：在启动时告诉我状态放哪。

    from dafeiyu import kernel
    kernel.configure(state_dir=r"D:\\...\\astrbot_plugin_live_avatar")

不调用也能跑（写到当前目录下的 `.dafeiyu/`），但宿主一定要调。
"""
from __future__ import annotations

import os

# 默认落在 cwd 下的 .dafeiyu/，至少不会污染 site-packages
_DEFAULT = os.path.join(os.getcwd(), ".dafeiyu")

STATE_DIR = ""

_configured = {"done": False}


def configure(state_dir: str | None = None) -> str:
    """指定状态目录（perm_mode.json / tuning.json / school_state.json 都放这里）。

    幂等：重复调用只会更新目录。返回最终生效的目录。
    """
    global STATE_DIR
    if state_dir:
        STATE_DIR = os.path.abspath(state_dir)
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
        except Exception:
            pass
    elif not STATE_DIR:
        STATE_DIR = _DEFAULT
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
        except Exception:
            pass
    if STATE_DIR:
        # 通知已经 import 过、把路径缓存下来的模块
        try:
            from . import perm as _perm
            _perm.set_dir(STATE_DIR)
        except Exception:
            pass
        try:
            from . import tuning as _tuning
            _tuning.set_dir(STATE_DIR)
        except Exception:
            pass
    _configured["done"] = True
    return STATE_DIR


def state_dir() -> str:
    """拿当前状态目录（没 configure 过就返回默认值并落盘建目录）。"""
    if not STATE_DIR:
        configure(None)
    return STATE_DIR


def path(name: str) -> str:
    """拼一个状态文件的绝对路径。"""
    return os.path.join(state_dir(), name)
