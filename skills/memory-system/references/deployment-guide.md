# Universal Agent Memory — 部署与维护参考

## 一键部署（公开仓库）
```bash
git clone https://github.com/Shine8592/universal-agent-memory.git
cd universal-agent-memory
chmod +x install.sh && ./install.sh
```

## 手动部署步骤
```bash
# 1. 安装依赖
pip install sentence-transformers faiss-cpu numpy

# 2. 部署脚本（全局脚本目录，遵循 universal-agent-memory 官方约定）
mkdir -p ~/.config/opencode/memory/scripts ~/.config/opencode/memory/{stm,archive}
cp scripts/*.py ~/.config/opencode/memory/scripts/
chmod +x ~/.config/opencode/memory/scripts/*.py

# 3. 部署技能
cp -r skills/* ~/.config/opencode/memory/skills/

# 4. 放入核心记忆文件（用户自行提供）
# 需要: SOUL.md, IDENTITY.md, USER.md, MEMORY.md, TOOLS.md

# 5. 初始化双记忆引擎
python3 ~/.config/opencode/memory/scripts/init_dual_memory.py

# 6. 构建向量索引
cd ~/.config/opencode/memory && python3 ~/.config/opencode/memory/scripts/build_full_index.py
```

## Ubuntu 部署（venv 推荐 / --target 兼容）

### 方案 A：venv（推荐）

```bash
python3 -m venv /opt/uam-venv
/opt/uam-venv/bin/pip install -U pip
/opt/uam-venv/bin/pip install numpy faiss-cpu sentence-transformers

# MCP 配置里的 command 指向 venv 解释器，而不是系统的 python3
#   "command": "/opt/uam-venv/bin/python"
#   "args": ["/root/.config/opencode/memory/scripts/mcp_server.py"]
```

venv 的 site-packages 由 `_bootstrap_sys_path()` 的 `VIRTUAL_ENV` 分支自动补回，
无需额外配置。**能用 venv 就不要用 `--target`**。

### 方案 B：pip --target（容器 / 只读镜像）

```bash
pip install --target /app/user-packages/python numpy faiss-cpu sentence-transformers
export PIP_TARGET=/app/user-packages/python
```

- v3.2.1 起 `_bootstrap_sys_path()` 会读取 `PIP_TARGET` 并注入 `sys.path`；多个目录用 `:` 分隔。
- **必须在启动 MCP 的那个进程环境里设置**（写进 systemd unit / docker-compose 的 `environment`），
  只在安装用的 shell 里 `export` 是无效的。
- 也可用 `UAM_EXTRA_SYS_PATH` 显式补充路径（优先级最高，适合多目录）。

### 部署验收（两步，缺一步都不算成功）

1. 调 `memory_status` → 必须看到 `向量依赖: ✅ OK`
   - 显示 `❌ 缺失 numpy/faiss/sentence_transformers` = 依赖没进 `sys.path`
2. 调 `memory_reindex` → 必须返回 `索引文件已更新（HH:MM:SS）`
   - 返回 `mtime 未前进` = 构建提前退出或写入失败，去查 MCP 的 stderr

> 改完环境变量要**重启 MCP 进程**；`_bootstrap_sys_path()` 只在进程启动时执行一次。

### 离线部署（目标机无外网）

先在联网机器跑一次，让模型下载到缓存，再把这两个目录整体复制到目标机的**同路径**：

| 目录 | 内容 | 体积 |
|------|------|------|
| `~/.config/opencode/models/` | 嵌入模型 `paraphrase-multilingual-MiniLM-L12-v2` | ≈470 MB |
| `~/.config/opencode/rerankers/` | 精排模型 `cross-encoder/ms-marco-MiniLM-L-6-v2` | ≈90 MB |

代码已设 `TRANSFORMERS_OFFLINE=1`，不会再尝试联网；模型缺失时会明确报
「嵌入模型不可用且在线下载失败」，而不是静默降级。

### 关闭精排（低配机器）

```bash
export MEMORY_RERANKER=off
```

检索降级为纯 `BM25 + 向量 + RRF`（不加载 CrossEncoder，省约 90MB 与一次模型加载）。

> ⚠️ 注意：精排用的是 `sentence_transformers.CrossEncoder`，**仍然依赖 torch**。
> 当前版本没有提供远程 embedding/rerank 通道，因此 Ubuntu 部署**本机仍需安装 torch**
> （`sentence-transformers` 会自动带上）。若目标是"完全无 torch"，本版本不支持。

## Cron 部署（使用 Hermes cronjob 工具）

```python
# 1. 每日备份
cronjob action=create name="嘟嘟每日备份" \
  schedule="0 2 * * *" \
  prompt="执行嘟嘟每日自动备份到GitHub。进入目录 /root/dudu-backup，git add -A，git commit，git push"

# 2. 向量索引重建
cronjob action=create name="向量索引每日重建" \
  schedule="0 3 * * *" \
  prompt="执行嘟嘟向量记忆索引自动重建。运行命令: python3 ~/.config/opencode/memory/scripts/build_full_index.py"

# 3. 记忆维护
cronjob action=create name="记忆维护自动转移" \
  schedule="0 */6 * * *" \
  prompt="执行嘟嘟记忆维护。运行命令: python3 ~/.config/opencode/memory/scripts/memory_maintain.py"
```

## 日常维护命令速查

| 操作 | 命令 |
|------|------|
| 查看索引状态 | `python3 ~/.config/opencode/memory/scripts/semantic_search.py status` |
| 语义搜索 | `python3 ~/.config/opencode/memory/scripts/semantic_search.py search "你的问题"` |
| 重建索引（含会话） | `python3 ~/.config/opencode/memory/scripts/build_full_index.py` |
| 记忆维护（清理+转移） | `python3 ~/.config/opencode/memory/scripts/memory_maintain.py` |
| 查看Cron任务列表 | `cronjob action=list` |

## 故障排查

| 问题 | 原因 | 解决 |
|------|------|------|
| `ModuleNotFoundError: No module named 'sentence_transformers'` | 依赖未安装 | `pip install sentence-transformers` |
| `memory_status` 显示 `向量依赖: ❌ 缺失 ...` | 依赖装了但不在 `sys.path`（`--target` / 容器常见） | 设 `PIP_TARGET` 或 `UAM_EXTRA_SYS_PATH`，然后**重启 MCP** |
| `memory_reindex` 返回「mtime 未前进」 | 构建提前退出或索引写入失败 | 确认 chunks 非 0，查 MCP stderr |
| 容器内 `memory_reindex` 抛 `ModuleNotFoundError` | 旧版 `build_full_index.py` 顶层硬导入 | 升级到 v3.2.1+ |
| Faiss索引为空（0 chunks） | 核心记忆文件未放入 `~/.config/opencode/memory/` | 放入 SOUL.md, MEMORY.md 等文件 |
| 搜索结果不相关 | 索引太旧或会话数据未包含 | 运行 `build_full_index.py` 重建 |
| STM为空 | 刚部署或已过期清理 | 正常，新会话会自动产生STM条目 |
