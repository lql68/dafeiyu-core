# dafeiyu-core

[![test](https://github.com/lql68/dafeiyu-core/actions/workflows/test.yml/badge.svg)](https://github.com/lql68/dafeiyu-core/actions/workflows/test.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![deps](https://img.shields.io/badge/dependencies-0-green.svg)](pyproject.toml)

数字生命内核 —— 从 [dafeiyu-blueprint](https://github.com/lql68/dafeiyu-blueprint) 那套设计里抽出来的**能跑的部分**。

**这不是一个聊天机器人框架。** 它不管你接 QQ 还是命令行，也不管你用什么模型。它只做一件事：

> 给一个 LLM 套上「时间感 / 心情 / 自己认准的事 / 自己是谁」。

---

## ⚠️ 先看这里：装完必须 `configure()`

内核**不假设自己躺在哪个目录里**。`perm` / `tuning` / `school` / `growth` 要知道文件写哪，
而在 `pip install` 之后 `os.path.dirname(__file__)` 指向的是 **site-packages** ——
不配的结果是：状态文件写进 site-packages、`growth` 每个 block 返回空（**不报错，只是空**）。

```python
from dafeiyu import kernel as k

k.configure(state_dir=r"D:\my-bot\plugin")          # perm_mode.json / tuning.json 落这里

k.growth.configure(                                  # growth 的库路径，不配就是全空
    plugin_dir=r"D:\my-bot\plugin",
    auto_db=r"D:\my-bot\plugin\autonomy.db",
    data_db=r"D:\my-bot\data\data_v4.db",
    lm_db=r"D:\my-bot\data\plugin_data\astrbot_plugin_livingmemory\livingmemory.db",
)
```

只 import 纯函数（`rhythm` / `selfstory` / `selfmodel` / `autonomy`）**不需要 configure** ——
它们接受 `db_path` 参数，或者自己建库。上面这段只对「状态落盘 / 读别人的库」那几个块是必需的。

---

## 环境要求

| | |
|---|---|
| Python | **≥ 3.10**（CI 覆盖 3.10 / 3.11 / 3.12 / 3.13） |
| 第三方依赖 | **零** —— 只用标准库（`json` / `sqlite3` / `math` / `datetime` / `hashlib` / `re`） |
| AstrBot | **不需要**。它原本长在 AstrBot 插件里，抽出来之后不认识 AstrBot |
| 模型 / API key | **不需要**。三个 example 全是本地算的，不调任何模型 |
| 数据库 | **不需要**。example 自己在临时目录建空库，不碰你的数据 |

`aiosqlite` / `httpx` 只在你要用 `school` 的异步落库、或者接真模型时才装：

```bash
pip install dafeiyu-core[asy]     # 只要异步落库
pip install dafeiyu-core[llm,asy] # 要接真模型 + 异步落库
```

**clone 下来什么都不用装**，三个 example 都能直接跑：

```bash
git clone https://github.com/lql68/dafeiyu-core
cd dafeiyu-core
python examples/rhythm_demo.py
```

（每个 example 开头都有 `sys.path.insert(0, .../src)`，所以不用 `pip install` 也不用设 `PYTHONPATH`。）

---

## 它现在能干什么

```bash
pip install -e .          # 可选：想让别处 import 得到才需要
python examples/rhythm_demo.py
```

输出（真实运行，2026-11-07）：

```
  时刻    时段   节律   唤醒   心率  呼吸   延迟   睡着
  ------------------------------------------------------------
  03:00   凌晨     0.07   0.60    81  12.9   2.23s  -
  ...
  17:00   下午     1.00   0.60    81  15.2   0.92s  -

  【下午三点、刚聊起来】lag=0.93s  hr=83  rr=15.9
    下午，状态平平，不特别兴奋也不困，心跳有点快。

  【凌晨三点半、没人理十分钟】lag=4.87s  hr=74  rr=11.4
    你正睡得最沉的时候被叫醒，脑子一片空白，只会含含糊糊应一声，
    话说不完整，说完又想睡回去。
```

**同一个她，凌晨三点半要等 4.87 秒才回话，下午三点只要 0.93 秒。** 这个数不是随机的，是 `circadian` 谷底 + 低唤醒算出来的。

---

## 八块

| 模块 | 一句话 | 依赖 |
|---|---|---|
| `rhythm` | 几点犯困、反应快慢、睡着了没有、睡到第几阶段 | **纯数学**，无 DB |
| `selfstory` | 自传章节 + 信念 + 证据 + 反证 + 15 天衰减 | sqlite3 |
| `selfmodel` | 「我现在觉得自己是个什么样的人」，按信念指纹变化重建 | sqlite3 |
| `autonomy` | 意图 / 问题池 / 认知会话 / 观察记录 / 日记快照 | sqlite3 |
| `growth` | 成长面板的九块只读聚合（不做任何写入） | sqlite3 |
| `perm` | 五档权限 —— 她能不能改自己的代码 | json 文件 |
| `school` | 技能学校 —— 从对话里攒「下次该怎么做」 | aiosqlite（可选） |
| `tuning` | 不改代码调阈值 | json 文件 |

**七块零依赖**，全部标准库（`json / sqlite3 / math / datetime / hashlib / re`）。
只有 `school` 真正落库时需要 `aiosqlite`（`pip install dafeiyu-core[asy]`）。

```bash
pytest        # 68 passed
```

---

## 三个反直觉的设计决策

这些是踩过坑才定下来的，不是拍脑袋：

### 1. 信念「被用上了」不涨分，只刷新时间

听起来「常用的信念应该更强」很对。但判定「这条信念被用上了」只能靠 2-gram 匹配 —— 而「在乎」和「在意」一个字都不撞。误判会往库里灌噪声。

改成**只刷新 `updated_at`，不动 `confidence`**：常用的永远够不着 15 天的衰减线，一直用不上的到点掉分。误判的成本从「凭空涨分」降到「这次没衰减」，可以接受。

### 2. 强制产出必然导致质量下降

反思时要求「写出 1-2 个你想弄明白的问题」，产出的全是：

> 「梦里那条消息我到底看了没」
> 「当时我是在投什么投不出去」

永远没有答案。改成**允许空数组**之后，问题变成了：

> 「我答应得那么顺，是不是因为我其实想离他近一点」

### 3. 心率不跟昼夜走，只跟唤醒度走

深夜心率低，不是因为在「晚上」，而是因为没人理 → `quiet_s` 变大 → 唤醒被压下来。这两件事在生理上当然相关，但在代码里**只有一个因果关系**。多写一条「晚上心率低」只会让两个因素打架。

---

## 目录

```
src/dafeiyu/
├── __init__.py
└── kernel/
    ├── rhythm.py        188 行  生理节律
    ├── selfstory.py     530 行  自传 + 信念
    ├── selfmodel.py     145 行  自我模型
    ├── autonomy.py     1250 行  意图 / 问题池 / 认知 / 观察
    ├── growth.py        520 行  成长面板聚合
    ├── perm.py          106 行  五档权限
    ├── school.py        263 行  技能学校
    └── tuning.py         84 行  参数覆盖
examples/rhythm_demo.py          不用 key 的演示
tests/                          68 个测试
```

---

## 带库的模块要先告诉它库在哪

`selfstory` / `selfmodel` / `autonomy` / `school` 都在构造时给路径，不用 configure：

```python
from dafeiyu import growth, selfstory

panel = growth.build()                             # 九块字典，前端直接用（需先 configure，见文首）
story = selfstory.SelfStory(r"D:\my-bot\plugin\autonomy.db")
story.chapter("今天…")                              # 写一章
```

**为什么不能自动猜** —— 这是从插件里搬出来时踩到的第一件事：

```python
# 在插件里是对的
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))   # → .../data/plugins/astrbot_plugin_live_avatar

# 装成包之后
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))   # → .../site-packages/dafeiyu/kernel
```

五个库全指错，而且 **`growth.build()` 不会报错，只是每个 block 返回空** —— 面板一片空白，
比直接崩掉难查得多。所以现在这些常量默认是空串，宁可让宿主显式注入。

---

## 从 blueprint 搬过来时做的取舍

原项目是 7254 行的 AstrBot 插件。这次搬了 **3086 行零框架依赖**的部分：

| 搬了 | 没搬（绑框架或绑服务） |
|---|---|
| `rhythm` / `selfstory` / `selfmodel` / `tuning` | `main.py`（3407 行，绑 AstrBot 的 filter / provider / event） |
| `autonomy` / `growth` / `perm` / `school` | `actor.py`（756 行，绑 httpx + astrbot logger） |
| | `persona_patch.py`（绑 AstrBot 的 personas 表） |

---

## 搬出来才暴露的三个真 bug

这些在插件里都活着，只是没人建过干净库 / 没跑过边界值：

**1. `autobiography` 表缺 `mood` 列**
线上库是 `ALTER TABLE` 加的列，`_SCHEMA` 里没有。新库一调 `add_chapter(mood=...)` 就 `no such column: mood`。修法：schema 补齐 + `init()` 里 `PRAGMA table_info` 给老库 ALTER。

**2. `arousal_proxy` 的软顶白做**
```python
if a > 0.85:
    a = 0.85 + (a - 0.85) * 0.5    # 1.5 → 1.175
return _clamp(a, 0.05, 1.0)        # 又被拉回 1.0
```
软顶是为了「别顶满」，clamp 上限 1.0 直接抵消。改成 0.95。

**3. `record_cognition_answer` 无条件返回 `True`**
```python
c.execute("UPDATE ... WHERE id = ? AND IFNULL(answer,'') = ''")
return True          # ← 已经答过的问题也说「记下了」
```
改成 `return cur.rowcount > 0`。

---

## 路线图

- [x] 八个内核 + 68 个测试
- [ ] 插件改成 `pip install -e` 依赖本包（单一源码，避免两边分叉）
- [ ] `brain.py` —— 从 `main.py` 抽出 `_turn` / `_attribute` / `_emo_*` / `_belief_*` / `_cog_*` 约 1200 行业务逻辑
- [ ] `provider.py` —— `Provider` 协议 + OpenAI 兼容实现 + `MockProvider`
- [ ] `memory.py` —— 简化版记忆（sqlite + 2-gram 检索），替代第三方 livingmemory
- [ ] `chat_cli.py` —— 命令行里跟她聊，30 秒看到情绪/信念

---

## 背景

完整的设计文档、踩坑记录和实测数据在 [dafeiyu-blueprint](https://github.com/lql68/dafeiyu-blueprint)：

- `01-生理节律/` 机制筛选（19 章知识库 → 只留 8 个能落到行为的）
- `02-情绪内核/` 三段式归因、三层保护、519 行变更记录
- `04-自主思考/` 九个子系统的实现记录和闭环缺口

MIT
