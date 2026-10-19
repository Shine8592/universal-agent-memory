# Changelog

本文件记录 Universal Agent Memory 的版本迭代历史，遵循 **本地迭代 → 验证 → 发布 GitHub 最新 → 旧版本归档** 的开发原则。

## [v3.2.1] - 2026-10-19

> 主题：修复 **`pip install --target` 部署形态下的向量依赖断裂**（社区报告 5 条，逐一核实：4 条属实 + 1 条有效加固建议）。

### 为什么

上游反馈：容器里用 `PIP_TARGET=/app/user-packages/python` 装依赖后，`memory_reindex` 必崩，
且 `memory_status` 完全看不出缺什么。核实后确认三条互相叠加的结构问题：

- `build_full_index.py` 顶层硬导入 `sentence_transformers` / `faiss`，而 `do_reindex`
  在 MCP 进程内 `from build_full_index import ...` → 依赖不在 `sys.path` 时整个模块 import 失败，
  连纯文本抽取（不需要模型）都一起用不了。
- `_bootstrap_sys_path()` 的注释写着"PIP_TARGET 指定的 user site…这里提前补回"，
  但**代码从未读取 `PIP_TARGET`**（只加了 `UAM_EXTRA_SYS_PATH` / `VIRTUAL_ENV` /
  user site / `PYTHONPATH`）。`site.getusersitepackages()` ≠ `pip --target` 目录 ——
  注释与实现不符，把排查方向直接带偏。
- 依赖自检只 `print(..., file=sys.stderr)`，MCP 宿主不转发 stderr，用户只看到"检索失效"。

另外 v3.1.1 删掉后台重建后，TOOL_DEFS 里的 `background` 参数没同步删除 ——
传了不报错也不生效，属于本项目一直在修的那类静默不一致。

### 变更

1. `build_full_index.py`：重依赖改为 `_require_embed_deps()` 惰性导入，缺谁报谁并附修复命令；
   `extract_core_and_logs()` 不再被迫加载 torch。
2. `_bootstrap_sys_path()`：新增读取 `PIP_TARGET`（`os.pathsep` 分隔，支持多路径），
   并修正误导性注释。
3. `do_reindex`：捕获 `ImportError`，返回可执行的降级提示，而不是把栈抛给宿主。
4. `memory_status`：新增「向量依赖: ✅ OK / ❌ 缺失 X」与 `PIP_TARGET` 行。
5. `do_reindex`：重建前后比对 `semantic_index.faiss` 的 mtime，**没前进就报失败**（防假成功）。
6. TOOL_DEFS：删除 `memory_reindex` 的 `background` 参数。

### 验证

- 模拟缺依赖（`sys.modules[...] = None`）：模块导入成功、纯文本抽取仍产出 94 块、
  `_require_embed_deps()` 抛出带修复命令的 `ImportError`、`memory_reindex` 返回中文降级提示。
- 模拟 `PIP_TARGET`：临时目录进入 `sys.path`，`import dummy_pkg` 成功（值 42）。
- 实机重建：97 块 / 14.8s，`semantic_index.faiss` mtime 从 `12:59:26` 前进到 `13:43:08`。

## [v3.2.0] - 2026-10-19

> 主题：**两个记忆 Skill 合并为一个**（删除 `semantic-memory-auto`）。属破坏性变更。

### 为什么

仓库里曾有两个 SKILL.md：

- `memory-system` —— 架构说明（存储结构 / 双引擎 / 工具清单）
- `semantic-memory-auto` —— 行为触发策略（何时检索 / 何时不检索 / 结果评分）

两者描述都含「记忆 / 历史决策 / 语义检索」，**同一个问题会同时命中两个**，各加载一遍 ——
上下文浪费，且命令/口径可能打架。实测 `semantic-memory-auto` 的独特价值只有约 15 行
（触发清单 + 不触发清单 + 评分口径），不值得单开一个 skill。

### 变更（破坏性）

- 触发条件、不触发条件、结果评分口径**并入 `memory-system`**；description 改为中文触发词。
- **删除 `skills/semantic-memory-auto/`**（仓库与 `~/.config/opencode/skills/` 各一份）。
- 修正 `memory-system` 两处失真：
  1. 存储树原画了 `warm/` `cold/` 目录 —— **实际不存在**；三层存储是**内存 LRU 缓存**
     （`storage_tiers.py` 的 `get_cold_items` / `auto_demote`），不是磁盘目录。
  2. MCP 工具清单从 8 个补全到 **12 个**（补 `memory_prime` / `memory_transfer` /
     `memory_reflect` / `memory_session_save`）。
- 补充 v3.1.0 的检索口径（评分 = 0.70 相关性 + 0.18 实体 + 0.12 新近度）、
  `supersedes` 用法、`MEMORY_GLOBAL` 说明。

### 验证

- 仓库 `skills/` 只剩 `memory-system/`（SKILL.md 5.1KB + `references/deployment-guide.md`）。
- `~/.config/opencode/skills/` 已同步删除 `semantic-memory-auto`；重启后技能列表不再出现它。
- 部署版与仓库的 `memory-system/SKILL.md` 哈希一致。

## [v3.1.1] - 2026-10-19

> 修复：`memory_reindex` 的**后台重建在 MCP 环境下会静默卡死**。

### 问题

重启后执行 `memory_reindex`，返回"✅ 已在后台启动"，但索引文件**始终不更新**：
子进程（`build_full_index.py`）CPU 仅 0.1~0.2s、内存 20MB，**永不结束**。
同一脚本在普通进程里前台跑只要 4~16s —— 说明是 MCP 进程 spawn 子进程时的
环境问题（stdin/stdout 管道继承），不是构建本身慢。

典型的**静默失败**：工具回了"成功"，实际什么也没做。

### 修复

- 改为**在 MCP 进程内同步重建**（实测 95 块 / 5.2s，远低于 MCP 超时 600s）。
- 重建完成后**就地失效缓存**（`searcher` / `_bm25_index` / `_full_cache`），
  下一次检索直接加载新索引，**无需重启**。
- 去掉了"后台模式 + DEVNULL"这条不可靠路径（顺带删掉 `MAX_CHUNKS` 截断逻辑，
  全量重建才符合"重建"的语义）。

### 验证

- 前台构建：95 块 / 3.9s ✅
- MCP 内同步构建：95 块 / 5.2s ✅，返回明确用时
- 索引 mtime 由 `10-17 01:01` 更新为 `10-19 10:59`，`memory_status` 的
  "索引落后 57.8 小时"告警消失 ✅

## [v3.1.0] - 2026-10-19

> 主题：**检索质量三件套**（时间感知 / 实体加权 / 矛盾屏蔽）+ 双记忆引擎修复 + 全局共享库。
> 起因：深度体检 + 对标 mem0(67k★) / cognee(32k★) 后发现 —— 问题不在"能不能存"，
> 而在 **"存了以后能不能按人的预期排出来"**。

### 体检结论（先说事实）

- 检索链路本身没问题：BM25 + 向量 + RRF + CrossEncoder，实测召回 relv 1.00。
- 但索引 94 条里 **93 条是 `MEMORY.md/SOUL.md/USER.md/AGENTS.md` 切出来的 chunk** ——
  长期记忆实际上是「一个大 Markdown 分块检索」，不是带类型的记忆库。
- 三个真实痛点：① 无时间权重 → 旧结论会压新结论；② 无实体信号 → 多项目混在一起时排序靠运气；
  ③ 只增不删 → 互相矛盾的旧结论永久留存。

### 新增

- **时间感知排序**：`_recency_score()` 半衰期 180 天衰减，权重 12%。
  （借鉴 mem0 的 temporal reasoning）
- **实体加权**：内置项目/工具/平台词典（duduhaibao、duduppt、opencode、快团团…），
  「查询实体在结果中的覆盖率」占 18% 权重，命中项打 `★实体` 标记。
  （借鉴 mem0 的 entity linking —— 但用词典匹配，**零 LLM**）
- **矛盾记忆屏蔽**：`memory_remember(supersedes="旧结论特征串")` 声明推翻；
  检索默认屏蔽被推翻的条目，`include_superseded=true` 可查看。
  ⚠️ 关键修复：比对时**回查元数据全文** —— 检索结果里的 text 被截断到 200/500 字，
  直接比对会漏掉位于截断之后的关键词。
- **索引健康度自检**：`memory_status` 新增「索引条目 / MEMORY.md 段数 / 最新记忆时间 /
  索引落后小时数 / 已屏蔽条数 / 累计提升条数」，落后 >1 小时直接告警。
- **全局共享库**：`MEMORY_GLOBAL=1` → 记忆统一存 `~/.opencode-memory`，修掉
  「换个目录开 Agent 就像失忆」。
- `memory_transfer` 支持 `threshold`（默认 0.7）与 `force`；修正 `promoted_count`
  统计（旧版恒为 0，看起来像从未提升过）。

### 验证（全程在数据副本上，未触碰真实记忆）

- 实体加权：`duduhaibao 发布流程` → 结果全 `★实体`，v3.0.0 条 relv 1.00。
- 矛盾屏蔽：声明 `supersedes="github.com:443"` 后，v2.0.0 旧结论从默认结果消失；
  `include_superseded=true` 时回到 #2。
- 转移：`threshold=0.0` 提升 1 条；`promoted_count` 0 → 1；MEMORY.md 84 → 85 段。
- 健康自检：正确报出「索引比 MEMORY.md 落后 57.7 小时」。

## [v3.0.3] - 2026-08-11

> 修复迭代：MCP 实机全功能验证 + 三处健壮性修复 + 版本号统一

### 🐛 修复：`memory_remember` tags 参数类型兼容（实机测试发现）

**问题**：MCP 客户端（如 Claude Code）调用 `memory_remember` 时可能把 `tags` 作为**列表**传递（`["tag1","tag2"]`），而代码只处理**逗号分隔字符串**（`tags.split(",")`）→ 抛 `'list' object has no attribute 'split'`。

**修复**：`mcp_server.py` 的 `do_remember()` 现在同时兼容两种格式（`isinstance(tags_raw, list)` 分支）。

**验证**：通过 MCP JSON-RPC 实际调用，列表/字符串/无 tags/空内容/重复去重 5 种场景全部通过。

### 🔧 修复：嵌入模型缓存路径解析（Hermes 布局兼容）

**问题**：`_choose_default_model()` 只从 `get_opencode_global()/models` 找缓存，而真实 Hermes 部署中模型缓存位于其**同级** `~/.hermes/models` → 多语言模型永远检测不到、静默回退英文。

**修复**：`memory_config.py` 新增 `_model_cache_dir()`：仅当 `global_dir.name=="memory" and global_dir.parent.name==".hermes"` 时走同级 `models/`，其余布局保持 `global_dir/models`（对 `~/.config/opencode/memory` 无影响）。

> **教训沉淀**：判定 Hermes 布局必须以「父目录名 == .hermes」严格判定，不能只凭「当前目录名 == memory」——旧配置路径 `~/.config/opencode/memory` 也叫 memory。

### 🧹 重构：移除旧英文模型回退分支（评估 #2）

**问题**：`build_full_index.py` 保留 `semantic_model/` 旧英文模型回退分支，但该目录在部署环境中不存在（死代码），且静默降级英文比显式报错更危险。

**修复**：删除 legacy 回退，主模型不可用时改为显式 `raise RuntimeError`（提示重新下载）。

### 🔢 版本号统一

- `mcp_server.py` serverInfo `3.0.0` → `3.0.3`（原与 `memory_status` 标题不一致）
- `memory_status` 标题 `v3.0.1` → `v3.0.3`

### 🗂️ 涉及文件

- `scripts/mcp_server.py`
- `scripts/memory_config.py`
- `scripts/build_full_index.py`

---

## [v3.0.2] - 2026-08-07

> 三版本统一合并 + 索引自愈机制修复

### 🔀 三版本统一合并
将长期分叉的 **部署版 / 工作副本 / GitHub main** 三个版本合并对齐，保留每一侧的全部优势：

| 脚本 | 合并决策 | 亮点 |
|------|---------|------|
| `memory_config.py` | 真合并 | 吸收 GitHub 版模型智能选择 + Cross-encoder 重排 + Hermes 路径兜底；修正 Hermes 返回路径一致性 |
| `mcp_server.py` | 部署版 | 保留 v3.0.1 标题 + 同分按 ts 取最新排序修复 |
| `build_full_index.py` | 工作副本版 | 采用 `st_mtime` 时间戳 + 模型加载离线回退（先新模型后 legacy） |
| `plugin_bridge.py` | 部署版 | 复用 `hybrid_search.BM25Index` 单实现（消除双实现 IDF 漂移），移除无用 `semantic_ok` |
| `memory_git.py` | 部署版 | 完整 `.gitignore`（含向量索引）+ `rollback` returncode 校验 |
| `dual_memory_engine.py` | 部署版 | `evaluate_transfers` 优先已存重要性分数 + `user_marked` 提升 |
| 其余 9 个脚本 | 部署版精简 | 移除冗余 import（未用 `os`/`hashlib`/`faiss` 等），逻辑不变 |

> **教训沉淀**：合并前先做三版本 diff，识别"部署版独有修复"（须保留）与"GitHub 独有增强"，用语义判断而非"谁新谁旧"；`没在用 ≠ 没用`，删除前必须确认代码真实引用。

### 🔧 修复：索引自动更新机制（关键）

**问题**：FAISS 语义索引曾滞后 45 小时（会话期仅 08-05 构建，而 08-07 的新记忆全部未进索引）。虽靠 MCP 混合检索的 **STM 实时向量编码**兜底（实际使用不掉记忆），但索引自更新机制是坏的：

1. `auto_index_updater.py` 只检测 `CORE_FILES + daily/*.md`，**完全忽略 `stm/*.json`** → 新增/修改 STM 记忆时 `needs_update()` 永不返回 True
2. 即便触发重建，`semantic_search.build_index()` 也**不含 STM**（只建核心 + 日志），STM 记忆从不进 FAISS 索引
3. 无任何定时任务在运行

**修复**：
- `get_memory_files_mtime()` 纳入 `STM_DIR/*.json` 检测
- `update_index()` 改用 `build_full_index.extract_core_and_logs + build_index`（含 STM/short_term/scenarios/atoms 权威扫描）
- 新增定时维护（Windows）：
  - `Memory_Index_Rebuild`：每天 01:00 全量重建
  - `Memory_AutoUpdate`：每 30 分钟检测（有变化才重建，低负载）
  - wrapper 脚本 `run_index_rebuild.cmd` / `run_index_autoupdate.cmd` 已设 `MEMORY_PROJECT_ROOT`，避免 `cwd` 回退导致索引到空目录（返回 0 块）

**验证**：索引从 28 → **37 块**（26 核心 + 11 STM）；新增 STM 记忆后 `needs_update()` 正确返回 True；多个定时任务调度正常。

### 🗂️ 涉及文件

- `scripts/memory_config.py`
- `scripts/mcp_server.py`
- `scripts/build_full_index.py`
- `scripts/plugin_bridge.py`
- `scripts/memory_git.py`
- `scripts/dual_memory_engine.py`
- `scripts/auto_index_updater.py`
- `scripts/*.py`（统一精简导入）
- `scripts/install_memory_mcp.py` / `final_verify.py` / `hybrid_search.py` / `storage_tiers.py` 等

---

## [v3.0.1] - 2026-08-05

修复迭代：GBK/emoji 崩溃、MCP stdout 协议流污染、清理旧模型引用。

## [v3.0.0] - 更名独立

首个 v3.0 快照：更名 universal-agent-memory 后的完整实现（MCP 架构 + 混合检索 + 跨 Agent 安装）。