# 🧠 Universal Agent Memory — 下一代 AI Agent 通用记忆系统

> **原名 `opencode-memory`，已升级为跨 Agent 通用记忆系统。** 适用于任何 AI 编程助手 / Agent：OpenCode、Claude Code、Cursor、Windsurf、Codex、Kiro、Zed、Continue、Cline、Roo Code、Gemini CLI、VS Code Copilot、Trae 等。

<div align="center">

![version](https://img.shields.io/badge/version-3.2.3-blue)
![Python](https://img.shields.io/badge/Python-3.10%2B-green)
![MCP](https://img.shields.io/badge/Protocol-MCP%20JSON--RPC-orange)
![Agents](https://img.shields.io/badge/Agents-13%2B-purple)
![License](https://img.shields.io/badge/License-MIT-brightgreen)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

**让你的 AI 编程助手真正记住你：偏好、决策、踩过的坑、项目上下文。**  
**跨会话、跨 Agent、离线优先、中英文同等流畅。**

[快速开始](#-快速开始) · [工具文档](#-12-个-mcp-工具) · [架构设计](#-架构设计) · [安装到任意-Agent](#-一键安装到任意-agent-13-个) · [Changelog](CHANGELOG.md)

</div>

---

## ✨ 为什么选择 Universal Agent Memory？

市面上的 AI 记忆系统要么需要托管云服务和 API Key，要么需要运行 Neo4j + Qdrant 双数据库，要么只支持英文。**Universal Agent Memory** 的设计原则是：**纯本地、纯 Python、零外部服务、中英文同等流畅、Agent 无关**。

| 对比项 | Universal Agent Memory | mem0 | cognee | letta |
|--------|-----------------|------|--------|-------|
| 完全离线 | ✅ 无需任何 API Key | ❌ 需 OpenAI Key | ❌ 需 LLM API | ❌ 需 LLM API |
| 单进程部署 | ✅ 纯 Python 单文件 | ✅ | ❌ 需 Neo4j/Qdrant | ❌ 需数据库 |
| Git 原生版本管理 | ✅ 每条记忆自动 commit | ❌ | ❌ | ❌ |
| 中文优化 | ✅ 多语言模型 + CJK 分词 | ⚠️ 需配置 | ⚠️ | ❌ |
| 混合检索 BM25+向量+RRF | ✅ + CrossEncoder 精排 | ✅ | ✅ | ❌ |
| 跨 Agent 一键安装 | ✅ 13 个 Agent | ✅ 32+ | ⚠️ | ❌ |
| 记忆版本回滚 | ✅ `memory_rollback` | ❌ | ❌ | ❌ |

---

## 🚀 核心特性

### 🔍 四段式混合检索（业界 SOTA 方案）

```
查询 "GBK 编码踩坑"
       │
       ├─► BM25 关键词检索   ─────────────────────┐
       │   (CJK bigram 分词，精确词汇命中)          │
       │                                          ├─► RRF 融合排序
       ├─► FAISS 向量检索    ─────────────────────┤   (Reciprocal Rank
       │   (核心文件语义索引)                       │    Fusion)
       │                                          │
       └─► STM 实时向量搜索  ─────────────────────┘
           (无需 reindex，写入即可检索)             │
                                                  ↓
                                        Cross-encoder 精排
                                        (ms-marco-MiniLM)
                                                  │
                                                  ↓
                                    #1 (relv 1.00) [pitfall] ...
                                    #2 (relv 0.87) [decision] ...
```

借鉴 **Hindsight**（LongMemEval SOTA）的四路并行 + CrossEncoder 精排方案，检索准确率显著优于纯向量 RAG。

### 🌍 多语言嵌入模型

默认使用 `paraphrase-multilingual-MiniLM-L12-v2`（384 维），中文、英文、日文、韩文同等流畅。可通过环境变量切换任意 sentence-transformers 兼容模型：

```bash
export MEMORY_MODEL_NAME=BAAI/bge-large-zh-v1.5  # 切换到中文专项模型
```

### 🏷️ 智能记忆分类

每条记忆自动推断功能类型，无需手动标注：

| 类型 | 触发关键词示例 | 用途 |
|------|--------------|------|
| `pitfall` | 踩坑、报错、exception、教训 | 避免重复犯错 |
| `decision` | 架构、决策、选型、设计 | 保持技术一致性 |
| `preference` | 偏好、习惯、喜欢 | 个性化交互 |
| `skill` | 命令、用法、how to | 技能积累 |
| `event` | 会话快照、任务进度 | 跨会话状态恢复 |
| `config` | 配置、安装、setup | 环境记录 |
| `fact` | 其他信息 | 通用存储 |

### 🔐 Git 原生版本管理

记忆不只是存储——每次写入、删除、演化操作都自动生成 Git commit，完整审计、随时回滚：

```bash
# 查看记忆变更历史
memory_history()
# → 📜 记忆变更历史 (共 138 次提交)
#     be365be  v3.0: memory_prime + Cross-encoder 重排 + ...
#     07caece  BUG修复: Cross-encoder logits 改用 min-max 归一化
#     966c571  v2.0: 索引用多语言模型重建 (11 chunks, 384d)

# 回滚到任意版本
memory_rollback(hash="07caece")
```

### 🧬 Reflect 离线演化

借鉴 **EverOS Reflection** 机制，记忆越用越精炼：

```
memory_reflect(apply=True, threshold=0.82)
# → 贪心聚类：相似度 ≥ 0.82 的记忆归为一簇
# → 每簇保留信息量最大的记忆（内容最长）
# → 合并标签，记录 merged_from
# → 高价值记忆自动提升到 MEMORY.md（LTM）
```

---

## 📦 12 个 MCP 工具

| 工具 | 功能 | 亮点 |
|------|------|------|
| `memory_prime` | **会话启动上下文注入** | 一次调用返回：偏好 + 踩坑 + 决策 + 任务相关记忆，替代多次单独检索 |
| `memory_recall` | **混合语义检索** | BM25 + 向量 + RRF + CrossEncoder，支持 `type_filter` 精准过滤 |
| `memory_remember` | **智能记忆保存** | 自动类型推断、语义去重（>0.9 拦截）、diff 审计 |
| `memory_reflect` | **离线记忆演化** | 聚类合并冗余，提炼高价值记忆到 LTM，preview/apply 双模式 |
| `memory_session_save` | **会话快照** | 保存任务进度 + 文件列表 + 决策，下次会话一键恢复 |
| `memory_transfer` | **STM→LTM 转移** | 手动触发高重要性短期记忆提升到长期记忆 |
| `memory_status` | **系统状态** | 显示索引条目、STM 类型分布、审计日志统计 |
| `memory_forget` | **关键词删除** | BM25 缓存联动失效，删除即时生效 |
| `memory_reindex` | **重建向量索引** | 进程内同步执行（4~16s），自动去重，校验索引文件是否真的落盘 |
| `memory_history` | **变更历史** | 查看 Git 提交记录 |
| `memory_rollback` | **版本回滚** | 回滚到任意 commit |
| `memory_sync` | **Git 初始化/同步** | 一键初始化记忆仓库 |

---

## ⚡ 快速开始

### 方式一：一键安装到任意 Agent（推荐）

```bash
git clone https://github.com/Shine8592/universal-agent-memory
cd universal-agent-memory

pip install -r requirements.txt

# 安装到所有已检测到的 Agent
python scripts/install_memory_mcp.py --all

# 或安装到指定 Agent
python scripts/install_memory_mcp.py claude-code cursor kiro
```

重启对应 Agent 即可使用所有 `memory_*` 工具。

### 方式二：手动配置

```bash
pip install sentence-transformers faiss-cpu numpy
```

然后根据你的 Agent 选择对应配置：

<details>
<summary><b>OpenCode</b>（<code>~/.config/opencode/opencode.jsonc</code>）</summary>

```jsonc
{
  "mcp": {
    "memory": {
      "type": "local",
      "command": ["python", "-u", "/path/to/scripts/mcp_server.py"],
      "enabled": true
    }
  }
}
```
</details>

<details>
<summary><b>Claude Code</b>（<code>~/.claude/settings.json</code>）</summary>

```json
{
  "mcpServers": {
    "memory": {
      "command": "python",
      "args": ["-u", "/path/to/scripts/mcp_server.py"]
    }
  }
}
```
</details>

<details>
<summary><b>Cursor</b>（<code>~/.cursor/mcp.json</code>）</summary>

```json
{
  "mcpServers": {
    "memory": {
      "command": "python",
      "args": ["-u", "/path/to/scripts/mcp_server.py"]
    }
  }
}
```
</details>

<details>
<summary><b>Zed</b>（<code>~/.config/zed/settings.json</code>）</summary>

```json
{
  "context_servers": {
    "memory": {
      "command": { "path": "python", "args": ["-u", "/path/to/scripts/mcp_server.py"] }
    }
  }
}
```
</details>

---

## 🔧 一键安装到任意 Agent（13 个）

```bash
python scripts/install_memory_mcp.py --list  # 检测已安装的 Agent
```

| Agent | 支持 | 配置格式 |
|-------|------|----------|
| opencode | ✅ | `mcp.<name>` |
| Claude Code | ✅ | `mcpServers` |
| Cursor | ✅ | `mcpServers` |
| Windsurf | ✅ | `mcpServers` |
| Codex CLI | ✅ | `mcpServers` |
| Kiro | ✅ | `mcpServers` |
| Zed | ✅ | `context_servers` |
| Continue | ✅ | `modelContextProtocolServers` |
| Cline (VSCode) | ✅ | `mcpServers` |
| Roo Code (VSCode) | ✅ | `mcpServers` |
| Gemini CLI | ✅ | `mcpServers` |
| VS Code Copilot | ✅ | `mcpServers` |
| Trae | ✅ | `mcpServers` |

> 安装器自动备份原配置（`*.bak`），安装/卸载幂等，重复执行无副作用。

---

## 🏗️ 架构设计

### 存储结构

```
项目根目录/
└── .opencode/
    ├── SOUL.md          ─── Agent 核心人格
    ├── USER.md          ─── 用户画像
    ├── MEMORY.md        ─── 长期记忆（LTM）
    ├── AGENTS.md        ─── Agent 配置
    └── memory/
        ├── stm/             ─── 短期记忆（JSON，24h 窗口）
        ├── semantic_index.faiss   ─── 向量索引
        ├── semantic_metadata.json ─── 索引元数据
        ├── memory_diff.jsonl      ─── 操作审计日志
        ├── archive/         ─── 冷存储归档
        └── daily/           ─── 每日日志
```

全局安装目录：`~/.config/opencode/memory/`，多项目共享同一套脚本，按项目根目录隔离记忆存储。

### 记忆生命周期

```
写入 (memory_remember)
  → 精确去重检查
  → 语义去重 (相似度 > 0.9)
  → 自动 type 推断
  → 重要性评分
  → 写入 STM (JSON)
  → Git auto-commit
  → diff 审计日志

检索 (memory_recall)
  → BM25 关键词 + FAISS 向量 + STM 实时向量
  → RRF 融合排序
  → Cross-encoder 精排
  → type_filter 精准过滤

演化 (memory_reflect)
  → 贪心聚类（余弦相似度 ≥ 阈值）
  → 冗余合并（保留信息量最大项）
  → 高价值记忆提升 LTM

过期 (24h 后)
  → 重要性评估
  → 高分记忆语义蒸馏写入 MEMORY.md
  → 低分记忆清理
```

### 编码防御（Windows 中文兼容）

专为中文 Windows 环境（GBK 系统编码）设计的三级防御：

```
stdin 字节流
  ① 先试 UTF-8（MCP 协议标准）
  ② 失败 → 系统编码（cp936/cp1252）
  ③ 再失败 → UTF-8 + replace 兜底

+ _clean_surrogates() 递归清洗 UTF-16 代理字符
+ 所有文件 IO 显式指定 encoding="utf-8"
```

---

## 🛠️ 环境变量配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MEMORY_PROJECT_ROOT` | `cwd` | 项目根目录（跨 Agent 通用） |
| `MEMORY_MODEL_NAME` | `paraphrase-multilingual-MiniLM-L12-v2` | 嵌入模型名称 |
| `MEMORY_RERANKER` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | 精排模型（设为 `off` 禁用） |
| `MEMORY_GLOBAL_DIR` | `~/.config/opencode/memory` | 全局脚本目录 |
| `MEMORY_STORE` | `<project>/.opencode/memory` | 记忆存储目录 |

---

## 📋 依赖

```
sentence-transformers  # 语义嵌入 + Cross-encoder 精排
faiss-cpu             # 向量索引
numpy                 # 数值计算
```

Python 3.10+，无需外部数据库、无需 Docker、无需 API Key。

---

## 🗂️ 项目结构

```
scripts/
├── mcp_server.py          # MCP Server 主入口（12 个工具，JSON-RPC over stdio）
├── hybrid_search.py       # BM25 + RRF 混合检索（纯标准库，中文 bigram）
├── semantic_search.py     # FAISS 向量检索
├── dual_memory_engine.py  # 双记忆引擎（STM/LTM 协同，自动分类，语义蒸馏）
├── memory_config.py       # 路径 + 模型配置（全环境变量可覆盖）
├── memory_git.py          # Git 原生版本管理
├── build_full_index.py    # 全量索引构建
├── install_memory_mcp.py  # 跨 Agent 一键安装器（13 个 Agent）
├── storage_tiers.py       # 热/温/冷三层存储
├── auto_index_updater.py  # 自动索引更新器（文件变更检测 → 触发重建）
├── plugin_bridge.py       # JSON CLI 桥接（Daemon 模式）
# 注：旧版的 memory_dedup.py / memory_maintain.py / init_dual_memory.py /
#     final_verify.py 已废弃删除 —— 语义去重并入 dual_memory_engine.find_semantic_duplicate，
#     定时维护改为 cron 直接调 build_full_index.py / memory_transfer。
```

---

## 🧬 演进历史

本项目由作者多年迭代而来，当前为**唯一维护的主项目**（v3.0+）。历史过渡仓库均已归档：

```
skill-memory-logic (2026-04)  → 最早的规则日志版
      ↓
SuperMemo-Du (2026-04)        → 双记忆引擎 + 三层存储
      ↓
super-memory-hermes-v1 (2026-05) → 语义向量记忆版（含已归档的 hermes-memory-system）
      ↓
universal-agent-memory (2026-07) → 当前主项目：MCP 架构 + 混合检索 + 跨 Agent 安装
```

| 历史仓库 | 状态 | 说明 |
|----------|------|------|
| [skill-memory-logic](https://github.com/Shine8592/skill-memory-logic) | 🗄️ 已归档 | 记忆逻辑管理技能（5 条铁律） |
| [SuperMemo-Du](https://github.com/Shine8592/SuperMemo-Du) | 🗄️ 已归档 | 双记忆协同引擎 |
| [super-memory-hermes-v1](https://github.com/Shine8592/super-memory-hermes-v1) | 🗄️ 已归档 | Hermes 语义向量记忆 V1 |
| [hermes-memory-system](https://github.com/Shine8592/hermes-memory-system) | 🗄️ 已归档 | Hermes 记忆系统原型 |

### 📌 版本归档（Releases）

本项目遵循 **本地迭代 → 验证 → 发布 GitHub 最新 → 旧版本归档** 的开发原则。`main` 分支始终为最新可安装版本，历史版本通过 [Releases](https://github.com/Shine8592/universal-agent-memory/releases) + Tag 归档沉淀，方便后续者查看完整演化过程。

版本命名规则：主版本按代际递增（v1 → v2 → v3），小版本迭代用 `vX.Y` / `vX.Y.Z` 格式归档（如 v1.1、v2.2）。

| 版本 | 说明 |
|------|------|
| [v3.2.3](https://github.com/Shine8592/universal-agent-memory/releases/tag/v3.2.3) | **当前最新**：补上会话采集层 —— 新增 `memory_session_ingest.py`（从 opencode.db 增量采集会话到 daily 日志 + STM，防 compaction 删消息）与 `memory_daily_upgrade.py`（每日 STM→LTM 提升）；`build_full_index.py` 的 daily 切分改为按「问答轮次」；文案去 opencode 绑定，改为「AI Agent 记忆系统」 |
| [v3.2.2](https://github.com/Shine8592/universal-agent-memory/releases/tag/v3.2.2) | 体检修复 —— 打通 `promoted_count` / `total_transfers` 双计数器（`memory_reflect` 链路此前不累计，"已提升到长期"恒为 0）；RRF 融合去重 key 改为完整 text（修掉同一条记忆在 BM25/向量两路被当成两条重复占位）；清理文档中 4 个已废弃脚本的引用 |
| [v3.2.1](https://github.com/Shine8592/universal-agent-memory/releases/tag/v3.2.1) | 修复 `--target` 部署形态下的向量依赖断裂 —— `build_full_index.py` 改惰性导入、`_bootstrap_sys_path()` 真正读取 `PIP_TARGET`、`memory_reindex` 缺依赖时给降级提示而非抛栈、`memory_status` 增加依赖状态行、重建后校验 index mtime；删除 v3.1.1 遗留的 `background` 参数 |
| [v3.2.0](https://github.com/Shine8592/universal-agent-memory/releases/tag/v3.2.0) | **破坏性**：两个记忆 Skill 合并为一个 —— `semantic-memory-auto` 的触发条件/不触发条件/结果评分并入 `memory-system` 后**删除该 Skill**；修正存储树（`warm/` `cold/` 实为内存 LRU，非磁盘目录）；MCP 工具清单补全至 12 个 |
| [v3.1.1](https://github.com/Shine8592/universal-agent-memory/releases/tag/v3.1.1) | 修复 `memory_reindex` 在 MCP 环境下 spawn 子进程会卡死（0.1s CPU / 20MB 永不结束 → 静默失败）；改为进程内同步重建（实测 5.2s），完成后就地失效缓存 |
| [v3.1.0](https://github.com/Shine8592/universal-agent-memory/releases/tag/v3.1.0) | 检索质量三件套（时间感知排序 + 实体加权 + 矛盾记忆屏蔽）+ 索引健康度自检 + 双记忆引擎修复（`promoted_count` / `threshold` / `force`）+ `MEMORY_GLOBAL` 全局共享库 |
| [v3.0.3](https://github.com/Shine8592/universal-agent-memory/releases/tag/v3.0.3) | 修复迭代（MCP 实机全功能验证 + `memory_remember` tags 列表格式兼容 + 模型缓存路径 Hermes 布局兼容 + 移除旧英文回退） |
| [v3.0.2](https://github.com/Shine8592/universal-agent-memory/releases/tag/v3.0.2) | 三版本统一合并（部署版/工作副本/GitHub 对齐，保留全部优势）+ 修复索引自动更新机制（STM 纳入检测、定时重建） |
| [v3.0.1](https://github.com/Shine8592/universal-agent-memory/releases/tag/v3.0.1) | 修复迭代（GBK/emoji 崩溃、MCP stdout 协议流污染、清理旧模型引用） |
| [v3.0.0](https://github.com/Shine8592/universal-agent-memory/releases/tag/v3.0.0) | 首个 v3.0 快照：更名 universal-agent-memory 后的完整实现 |
| [v2.0](https://github.com/Shine8592/universal-agent-memory/releases/tag/v2.0) | 语义向量记忆版（Hermes V1 演进）：混合检索 BM25+RRF+CrossEncoder，对应 [super-memory-hermes-v1](https://github.com/Shine8592/super-memory-hermes-v1) |
| [v1.0](https://github.com/Shine8592/universal-agent-memory/releases/tag/v1.0) | 规则逻辑记忆 + 双记忆引擎阶段，对应 [skill-memory-logic](https://github.com/Shine8592/skill-memory-logic) / [SuperMemo-Du](https://github.com/Shine8592/SuperMemo-Du) |

---

## 📄 License

MIT License — 自由使用、修改、分发。

---

<div align="center">

如果这个项目对你有帮助，请给个 ⭐ Star — 这是对持续维护的最大鼓励。

</div>