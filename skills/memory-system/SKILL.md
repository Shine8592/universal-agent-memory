---
name: memory-system
description: |
  记忆系统（AI Agent 持久记忆，跨平台通用）。当用户问到历史决策、配置偏好、项目进度、已解决的坑、已装技能或技术方案时，
  先用语义检索召回再回答；也用于保存重要信息、重建索引、查看/回滚记忆版本。
  检索链路 = BM25 关键词 + Faiss 向量 + RRF 融合 + CrossEncoder 重排，并叠加实体加权与时间感知。
  纯本地运行（sentence-transformers 小模型），不依赖任何 LLM API。
tags: [memory, semantic-search, vector-db, faiss, dual-memory, storage-tiers, persistence]
---

# Memory System for opencode

> 本文件同时承担两件事：**说明系统架构**，以及**规定什么时候必须检索**。
> （v3.2.0：原 `semantic-memory-auto` 已并入本 skill，不再单独存在 —— 两个 skill 描述重叠会
> 让同一个问题同时命中两次，纯属浪费上下文。）

## 什么时候用（触发条件）

用户的问题涉及以下内容时，**先检索再回答**：

1. **历史决策** —— "为什么选 XX""我们之前讨论过…"
2. **用户配置** —— "我的邮箱是""API 配置"
3. **项目进度** —— "项目进展""之前改了什么"
4. **用户偏好** —— "我喜欢什么"
5. **已安装技能** —— "我们有什么技能"
6. **技术方案 / 踩过的坑** —— "之前怎么解决的"

**不触发**：简单问候（"你好"）、即时命令（"发邮件"）、用户明确说不用查。

## 检索（优先 MCP，CLI 兜底）

优先用 MCP 工具（已配置在 `opencode.jsonc`）：

| 工具 | 用途 |
|---|---|
| `memory_recall` | 语义检索（BM25 + 向量 + RRF + 重排 + 实体/时间）|
| `memory_prime` | 会话启动一次性注入（偏好 + 踩坑 + 决策 + 高分记忆）|
| `memory_remember` | 保存记忆（支持 `type` / `tags` / `supersedes`）|
| `memory_transfer` | STM→LTM 提升（`threshold` / `force`）|
| `memory_forget` | 按关键词删除 |
| `memory_status` | 状态 + 索引健康度自检（索引 vs MEMORY.md 是否落后）|
| `memory_reindex` | 重建索引（进程内同步，约 4s）|
| `memory_history` | 版本历史 |
| `memory_rollback` | 回滚到指定版本 |
| `memory_sync` | 初始化 / 同步 Git 仓库 |
| `memory_reflect` | 离线演化（聚类合并冗余记忆）|
| `memory_session_save` | 会话快照（下次 `memory_prime` 恢复）|

**CLI 兜底**（脚本全局安装，任意目录可用）：

```bash
python ~/.config/opencode/memory/scripts/semantic_search.py search "查询"
python ~/.config/opencode/memory/scripts/semantic_search.py status
python ~/.config/opencode/memory/scripts/build_full_index.py   # 全量重建
```

中国大陆可设镜像：`$env:HF_ENDPOINT="https://hf-mirror.com"`

## 结果怎么用

取 Top 3–5 条，引用时标注来源前缀（如 `[MEMORY.md]`）。相关度参考：

| 评分 | 含义 | 应对 |
|---|---|---|
| 0.5+ | 强相关 | 直接引用 |
| 0.3–0.5 | 中等 | 选择性引用 |
| < 0.3 | 弱相关 | 可选 |

> v3.1.0 起评分 = **0.70 × 相关性 + 0.18 × 实体命中 + 0.12 × 新近度**，
> 所以"旧结论压新结论"和"多项目混排靠运气"这两种情况会明显减少。

## 架构

| 组件 | 作用 | 技术 |
|---|---|---|
| 语义向量检索 | 语义召回（非关键词）| sentence-transformers + Faiss |
| 双记忆引擎 | STM ↔ LTM 自动提升 | JSON + 重要性评分 |
| 三层存储 | 热 / 温 / 冷 | **内存 LRU 缓存** + 磁盘 + 归档 |

> ⚠️ 三层存储是**内存缓存策略**（`storage_tiers.py` 的 `get_cold_items` / `auto_demote`），
> **不是** `memory/` 下的 `warm/`、`cold/` 目录 —— 早期文档的存储树图这样画过，已更正。

## 存储结构

```
project_root/
└── .opencode/
    ├── MEMORY.md        # 长期记忆（LTM）—— 真源：按 ### 分节，切成 chunk 入索引
    ├── SOUL.md          # 人格
    ├── USER.md          # 用户画像
    ├── AGENTS.md        # Agent 配置
    └── memory/
        ├── semantic_index.faiss    # Faiss 向量索引
        ├── semantic_metadata.json  # 索引元数据（含 timestamp）
        ├── stm/                    # 短期记忆（24h 窗口，JSON）
        ├── daily/                  # 每日日志
        └── archive/                # 归档
```

> 代码全局安装在 `~/.config/opencode/memory/scripts/`。
> **存储跟着 cwd 走**（项目级）；想跨项目共用一份记忆，设 `MEMORY_GLOBAL=1` → `~/.opencode-memory`。

## 长期记忆与提升

- **STM**：24h 窗口、原始会话记录、最多 1000 条
- **LTM**：`MEMORY.md`，蒸馏后的关键信息
- **提升条件**：`importance_score >= 0.7`，或 tags 含 `pitfall`，或用户标记 important
- **矛盾结论**：保存时用 `supersedes="旧结论特征串"`；检索默认屏蔽被推翻的旧结论
  （要看旧的传 `include_superseded=true`）

## 依赖

```bash
pip install sentence-transformers faiss-cpu numpy
```

模型约 80MB，首次运行自动下载（`$env:HF_ENDPOINT` 可走镜像）；**纯 CPU 即可**，不需要 GPU。
