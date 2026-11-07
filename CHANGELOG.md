# 变更记录

版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.2.0] — 2026-11-07

八块内核全部抽出，从 AstrBot 插件独立成包。

### 新增

- `kernel/autonomy.py` — 意图 / 问题池（gaps）/ 认知会话 / 观察 / 权限门
- `kernel/growth.py` — 九块只读聚合（人格 / 世界观 / 兴趣 / 人物 / 信念 / 自我模型 / 时间线 / 技能 / 摘要）
- `kernel/perm.py` — 五档权限（plan / explore / interact / execute / self_modify）+ full 例外
- `kernel/school.py` — 技能学校（`aiosqlite` 可选）
- `kernel/_state.py` — 状态目录可配置，`configure(state_dir=...)`
- `tests/test_autonomy.py` — 17 个用例
- `py.typed` — PEP 561 类型标记

### 变更

- `growth.py` 五个库路径常量改为**空串**，必须由宿主 `configure()` 注入
- `perm.py` / `tuning.py` / `school.py` 不再用 `dirname(__file__)` 定位状态文件

### 修复

三个「从插件里搬出来才暴露」的 bug：

- `selfstory` 的 `_SCHEMA` 里 `autobiography` 缺 `mood` 列（线上库是 ALTER 加的），新建库 `add_chapter(mood=...)` 直接 `OperationalError`
- `rhythm.arousal_proxy` 的软顶被末尾 `_clamp(a, 0.05, 1.0)` 抹平，「不要顶满」的设计失效（上限改为 0.95）
- `autonomy.record_cognition_answer` 无条件 `return True`，已经答过的问题也说「记下了」（改为 `rowcount > 0`）

---

## [0.1.0] — 2026-11-07

第一次从 AstrBot 插件里抽出零依赖内核。

### 新增

- `kernel/rhythm.py` — 生理节律（circadian / arousal / hr / rr / lag / 句长上限），纯数学
- `kernel/selfstory.py` — 自传 / 反思 / 信念（合并 / 反证 / 15 天衰减）
- `kernel/selfmodel.py` — 自我模型（信念指纹变了才重建，只增不改）
- `kernel/tuning.py` — 运行时可调参数
- `examples/rhythm_demo.py` — 24 小时节律表 + 三个场景
- `tests/test_rhythm.py` / `test_selfstory.py` — 51 个用例

### 设计决定

三个反直觉的地方，写在这里免得以后有人「优化」掉：

1. **`hr` 只跟 `arousal` 走，不跟昼夜走。** 深夜心率低是因为「安静」压低了唤醒，不是因为「晚上心率该低」。
2. **信念被用到时不涨分，只刷新 `updated_at`。** 2-gram 判「用上了」必然误判（「在乎」和「在意」一个字都不撞），涨分等于往库里灌噪声。刷时间没有假阳性成本 —— 错判最多「这条不衰减」。
3. **衰减按「最近一条证据的时间」算，不按创建时间。** 常用的信念永远不到 15 天，一直用不上的 15 天后每 15 天 −0.02。

---

## 更早

0.1.0 之前的实现活在 AstrBot 插件 `astrbot_plugin_live_avatar` 里（7254 行），
本仓库只收 3086 行零依赖部分。剩下 `main.py`(3407) / `actor.py`(756) /
`persona_patch.py`(267) 因为依赖 `astrbot.api` 没搬。
