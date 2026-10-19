import os
from pathlib import Path

def get_opencode_global() -> Path:
    """全局记忆系统根目录（跨 Agent 通用，不依赖 opencode 专属路径）。
    优先 MEMORY_GLOBAL_DIR；未设置时，优先真实部署目录 ~/.hermes/memory（若存在）
    ，否则回退旧的 ~/.config/opencode/memory（为 cron/nohup 无 env 场景兜底）。
    """
    env = os.environ.get("MEMORY_GLOBAL_DIR")
    if env:
        return Path(env).resolve()
    # 真实部署路径探测：~/.hermes/memory 存在则优先（GitHub 修复版逻辑，兼容性保留）
    hermes = Path.home() / ".hermes" / "memory"
    if hermes.exists():
        return hermes
    return Path.home() / ".config" / "opencode" / "memory"

def get_project_root() -> Path:
    """项目根目录：优先 MEMORY_PROJECT_ROOT（跨 Agent 通用），兼容 OPENCODE_PROJECT_ROOT。
    未设置时回退 ~/.hermes（真实部署）。
    """
    env = os.environ.get("MEMORY_PROJECT_ROOT") or os.environ.get("OPENCODE_PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    if (Path.home() / ".hermes" / "memory").exists():
        return Path.home() / ".hermes"
    return Path.cwd().resolve()

def get_memory_dir() -> Path:
    """项目级记忆存储目录（兼容旧路径 .opencode/memory，支持 MEMORY_STORE 覆盖）。
    未设环境变量时（如 cron/nohup 直接跑脚本），优先真实部署路径，
    否则回退 cwd/.opencode/memory —— 避免读空目录导致索引/检索全 0。

    v3.1.0：新增 MEMORY_GLOBAL=1 —— 全局共享库（~/.opencode-memory），
    所有项目共用一份记忆，修掉「换个目录开 Agent 就看起来失忆」的问题。"""
    env = os.environ.get("MEMORY_STORE")
    if env:
        return Path(env).resolve()
    if os.environ.get("MEMORY_GLOBAL", "").strip().lower() in ("1", "true", "yes", "on"):
        return Path.home() / ".opencode-memory"
    # 真实部署路径探测：~/.hermes/memory 存在则优先（GitHub 修复版逻辑，兼容性保留）
    hermes = Path.home() / ".hermes" / "memory"
    if hermes.exists():
        return hermes
    return get_project_root() / ".opencode" / "memory"

def get_scripts_dir() -> Path:
    return get_opencode_global() / "scripts"

def get_hermes_dir() -> Path:
    """记忆核心文件目录（.opencode/）。v3.1.0：MEMORY_GLOBAL=1 时与全局库同目录，
    使 MEMORY.md 也在全局库里，避免 LTM 留在项目级造成「记忆分裂」。"""
    if os.environ.get("MEMORY_GLOBAL", "").strip().lower() in ("1", "true", "yes", "on"):
        return get_memory_dir()
    return get_project_root() / ".opencode"

MEMORY_DIR = get_memory_dir()
HERMES_DIR = get_hermes_dir()
SCRIPTS_DIR = get_scripts_dir()

INDEX_PATH = MEMORY_DIR / "semantic_index.faiss"
METADATA_PATH = MEMORY_DIR / "semantic_metadata.json"

# 嵌入模型：支持 MEMORY_MODEL_NAME 环境变量覆盖；默认多语言模型。
# （旧英文模型 all-MiniLM-L6-v2 已弃用：本机从未缓存、代码回退分支已于本次清理移除）
_ML_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"   # 中英文效果均佳，384 维

def _model_cache_dir() -> Path:
    """模型缓存根目录。

    兼容两种部署布局：
    1) Hermes 真实部署：记忆数据在 ~/.hermes/memory，模型缓存在其同级 ~/.hermes/models；
    2) 传统/环境变量布局：模型缓存在记忆根目录下的 models/（如 ~/.config/opencode/memory/models）。

    注意：必须以「父目录名为 .hermes」作为 Hermes 布局的严格判定，
    不能只凭「当前目录名为 memory」判断——旧配置路径 ~/.config/opencode/memory 也叫 memory。
    """
    global_dir = get_opencode_global()
    # 真实 Hermes 部署：全局目录是 ~/.hermes/memory，模型在其父级 ~/.hermes/models
    if global_dir.name == "memory" and global_dir.parent.name == ".hermes":
        return global_dir.parent / "models"
    # 其余布局（含旧配置）：模型缓存在 global_dir/models 下
    return global_dir / "models"


MODEL_NAME = os.environ.get("MEMORY_MODEL_NAME", _ML_MODEL)

# --- Cross-encoder 重排模型（v3.0，支持 MEMORY_RERANKER 环境变量；off 禁用） ---
_DEF_RERANKER = "cross-encoder/ms-marco-MiniLM-L-6-v2"

def get_reranker_path() -> Path:
    safe = _DEF_RERANKER.replace("/", "_").replace(":", "_")
    return get_opencode_global() / "rerankers" / safe

_RERANK_ENV = os.environ.get("MEMORY_RERANKER", "").strip()
if _RERANK_ENV.lower() == "off":
    RERANK_ENABLED = False
    RERANKER_NAME = ""
    RERANKER_PATH = None
else:
    RERANK_ENABLED = True
    RERANKER_NAME = _RERANK_ENV or _DEF_RERANKER
    RERANKER_PATH = get_reranker_path()

def get_model_path() -> Path:
    """模型本地缓存目录（按模型名区分，避免混用）"""
    safe = MODEL_NAME.replace("/", "_").replace(":", "_")
    return get_opencode_global() / "models" / safe

MODEL_PATH = get_model_path()

STM_DIR = MEMORY_DIR / "stm"
LTM_FILE = HERMES_DIR / "MEMORY.md"
COORDINATOR_FILE = MEMORY_DIR / "memory_coordinator.json"
ARCHIVE_DIR = MEMORY_DIR / "archive"
DAILY_DIR = MEMORY_DIR / "daily"
DIFF_LOG_PATH = MEMORY_DIR / "memory_diff.jsonl"   # 审计日志（阶段三-8）

MAX_CHUNK_CHARS = 1200

CORE_FILES = ["SOUL.md", "USER.md", "MEMORY.md", "AGENTS.md"]
PROJECT_ROOT = get_project_root()

def ensure_dirs():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    STM_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)


# Faiss 原生 C++ I/O 不支持中文路径，用序列化绕开
_faiss = None
_np = None

def _load_faiss():
    global _faiss, _np
    if _faiss is None:
        import faiss
        import numpy as np
        _faiss = faiss
        _np = np

def write_index_safe(index, path: Path):
    _load_faiss()
    buf = _faiss.serialize_index(index)
    path.write_bytes(buf.tobytes())

def read_index_safe(path: Path):
    _load_faiss()
    buf = _np.frombuffer(path.read_bytes(), dtype=_np.uint8)
    return _faiss.deserialize_index(buf)
