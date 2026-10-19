#!/usr/bin/env python3
"""
MCP Server for opencode memory system.
Raw JSON-RPC over stdio — no mcp library dependency.

v2.0 升级：
- 混合检索：BM25 关键词 + FAISS 向量 + RRF 融合（阶段一-1）
- 多语言中文嵌入模型（阶段一-2）
- 语义去重（相似度>0.9）（阶段一-3）
- 记忆功能型分类 type（阶段三-6）
- memory_reflect 离线演化（阶段三-7）
- memory_diff.jsonl 审计日志（阶段三-8）
"""
import sys, json, os, contextlib
from pathlib import Path
from datetime import datetime

def _bootstrap_sys_path():
    """在部分 MCP 宿主以 stdio 拉起本服务、且不继承 PYTHONPATH / 不设
    VIRTUAL_ENV 时，主动把常见的依赖目录补回 sys.path。

    典型场景：QwenPaw 等框架 spawn 子进程后，venv 的 site-packages 与
    PIP_TARGET 指定的 user site（如 torch/scipy 所在目录）都不在 sys.path 中，
    导致 sentence-transformers 在首次向量检索时才 ImportError，而宿主只报
    一个难以定位的 "Connection closed"。这里提前补回这些路径即可正常加载。

    也可用环境变量 UAM_EXTRA_SYS_PATH（os.pathsep 分隔）显式注入路径，
    任何宿主无需改代码即可传入自定义依赖目录。
    """
    import site
    added = []
    # 1) 显式注入（宿主提供，环境无关）
    for p in os.environ.get("UAM_EXTRA_SYS_PATH", "").split(os.pathsep):
        p = p.strip()
        if p and p not in sys.path and os.path.isdir(p):
            sys.path.insert(0, p)
            added.append(p)
    # 2) 自动识别当前 virtualenv
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        vsp = os.path.join(venv, "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages")
        if os.path.isdir(vsp) and vsp not in sys.path:
            sys.path.insert(0, vsp)
            added.append(vsp)
    # 3) user site / global site（PIP_TARGET 场景）
    try:
        for usp in (site.getusersitepackages(),) + (tuple(site.getsitepackages()) if hasattr(site, "getsitepackages") else ()):
            if usp and os.path.isdir(usp) and usp not in sys.path:
                sys.path.append(usp)
                added.append(usp)
    except Exception:
        pass
    # 4) 兼容常规 shell 下的 PYTHONPATH
    for p in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        p = p.strip()
        if p and p not in sys.path and os.path.isdir(p):
            sys.path.insert(0, p)
            added.append(p)
    return added


def _check_embedding_deps():
    """启动期自检：缺依赖时给出可执行的清晰提示，而不是在首次检索时才崩。

    用 importlib.util.find_spec 轻量探测（只检查模块是否可导入，不真正执行
    sentence_transformers 等重包的顶层代码），避免启动即加载 embedding 后端、
    拖慢 MCP 握手导致宿主把本服务判为未连接（显示灰色）。能否真正 import 仍由
    get_searcher() 在首次检索时验证。
    """
    import importlib.util
    missing = []
    for mod in ("numpy", "faiss", "sentence_transformers"):
        try:
            if importlib.util.find_spec(mod) is None:
                missing.append(mod)
        except Exception:
            missing.append(mod)
    if missing:
        print("[universal-agent-memory] WARNING: 缺失 embedding 依赖: "
              + ", ".join(missing) + "。memory_recall 将无法工作。修复: `pip install "
              + " ".join(missing) + "` 或设置 UAM_EXTRA_SYS_PATH 指向其所在目录。",
              file=sys.stderr)
    return missing


_bootstrap_sys_path()
_missing_deps = _check_embedding_deps()

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True, write_through=True)

sys.path.insert(0, str(Path(__file__).parent))
from memory_config import (
    MODEL_NAME, MEMORY_DIR, INDEX_PATH, METADATA_PATH,
    STM_DIR, SCRIPTS_DIR, PROJECT_ROOT, DIFF_LOG_PATH, ensure_dirs,
    RERANK_ENABLED, HERMES_DIR
)
from hybrid_search import build_from_stm, rrf_merge

# ═══════════════════════════════════════════════════════════════════
#  v3.1.0 检索增强：实体加权 + 时间感知 + 矛盾记忆屏蔽
#
#  借鉴 mem0 的 entity linking / temporal reasoning，但**不引入任何 LLM**：
#  实体用词典匹配，时间用时间戳衰减，矛盾用显式 supersedes 声明。
# ═══════════════════════════════════════════════════════════════════
SUPERSEDED_PATH = MEMORY_DIR / "superseded.json"

# 实体词典：项目名 / 工具名 / 平台名（命中是最强的领域信号）
ENTITY_TERMS = [
    "duduhaibao", "duduppt", "duduexcel", "dudutt", "universal-agent-memory",
    "opencode", "hermes", "claude", "codex", "cursor", "playwright", "tavily",
    "typst", "python-pptx", "pptxgenjs", "openpyxl", "faiss", "bm25", "sqlite",
    "github", "pypi", "vps", "ssh", "nginx", "docker", "windows", "powershell",
    "快团团", "拼多多", "海报", "记忆系统", "节气", "小红书", "公众号", "邀请函",
]


def _extract_entities(text: str) -> set:
    t = (text or "").lower()
    return {e for e in ENTITY_TERMS if e in t}


def _parse_ts(value):
    """解析多种时间格式 → datetime；解析不了返回 None。"""
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except Exception:
            return None
    s = str(value)[:19].replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def _recency_score(value) -> float:
    """新近程度 0~1（半衰期 180 天）。无时间戳给中性 0.5，不奖不罚。"""
    ts = _parse_ts(value)
    if ts is None:
        return 0.5
    days = max(0.0, (datetime.now() - ts).total_seconds() / 86400.0)
    return 0.5 ** (days / 180.0)


def _load_superseded() -> list:
    try:
        if SUPERSEDED_PATH.exists():
            data = json.loads(SUPERSEDED_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _save_superseded(items: list):
    try:
        SUPERSEDED_PATH.parent.mkdir(parents=True, exist_ok=True)
        SUPERSEDED_PATH.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[superseded] 写入失败: {e}", file=sys.stderr)


def _is_superseded(text: str, rules: list) -> bool:
    t = (text or "").lower()
    for r in rules or []:
        pat = str(r.get("pattern", "") or "").strip().lower()
        if pat and pat in t:
            return True
    return False


# 全文缓存：检索结果里的 text 会被截断（200/500 字），直接拿它比对 supersedes
# 会漏掉位于截断之后的关键词。这里按「前 120 字」回查元数据里的完整 chunk。
_full_cache = {"mtime": None, "map": {}}


def _full_text_map() -> dict:
    try:
        if not METADATA_PATH.exists():
            return {}
        mt = METADATA_PATH.stat().st_mtime
        if _full_cache["mtime"] == mt:
            return _full_cache["map"]
        meta = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
        m = {}
        for e in meta:
            t = e.get("text", "") or ""
            if t:
                m[t[:120]] = t
        _full_cache["mtime"] = mt
        _full_cache["map"] = m
        return m
    except Exception:
        return {}

# Lazy-load model on first use (cold start ~1s instead of ~10s)
searcher = None

def _clean_surrogates(text: str) -> str:
    """Remove lone UTF-16 surrogate characters that can't be encoded to UTF-8."""
    return text.encode("utf-8", errors="replace").decode("utf-8")

def _clean_obj(obj):
    """Recursively clean surrogates from all strings in a nested dict/list."""
    if isinstance(obj, str):
        return _clean_surrogates(obj)
    if isinstance(obj, dict):
        return {k: _clean_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_obj(v) for v in obj]
    return obj

def get_searcher():
    global searcher
    if searcher is None:
        try:
            from semantic_search import SemanticMemorySearch
            with contextlib.redirect_stdout(sys.stderr):
                searcher = SemanticMemorySearch()
                searcher.load_model()
        except Exception as _e:
            print(f"[universal-agent-memory] 加载 embedding 后端失败: {_e}", file=sys.stderr)
            if _missing_deps:
                print(f"[universal-agent-memory] 缺失依赖: {_missing_deps}（请设置 UAM_EXTRA_SYS_PATH 或 pip install 它们）", file=sys.stderr)
            return None
    return searcher

def auto_commit(msg: str):
    try:
        from memory_git import commit as gc
        gc(msg)
    except:
        pass

# --- 审计日志（阶段三-8）---
def append_diff(op: str, item_id: str = "", content: str = "", tags: list = None, **extra):
    """追加一条操作到 memory_diff.jsonl 审计日志"""
    try:
        entry = {
            "ts": datetime.now().isoformat(),
            "op": op,
            "id": item_id[:12] if item_id else "",
            "content": content[:200] if content else "",
            "tags": tags or [],
        }
        entry.update(extra)
        DIFF_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DIFF_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

# --- BM25 缓存（阶段一-1）---
_bm25_index = None

def get_bm25_index():
    """构建（或复用）BM25 索引：聚合 STM + FAISS 元数据
    修复：缓存复用，仅在 invalidate_bm25() 后才重建（原先每次 recall 全量重建）"""
    global _bm25_index
    if _bm25_index is None:
        _bm25_index = build_from_stm(STM_DIR, METADATA_PATH)
    return _bm25_index

def invalidate_bm25():
    """STM 变化后使 BM25 索引失效"""
    global _bm25_index
    _bm25_index = None

# --- Cross-encoder 重排（v3.0 P0-2，借鉴 Hindsight SOTA 配方）---
_reranker = None
_reranker_failed = False   # 加载失败后不再重试，避免每次 recall 卡顿

def get_reranker():
    """惰性加载 Cross-encoder 精排模型；不可用时返回 None（优雅降级为纯 RRF）"""
    global _reranker, _reranker_failed
    if _reranker_failed:
        return None
    if _reranker is None:
        try:
            from sentence_transformers import CrossEncoder
            from memory_config import RERANKER_NAME, RERANKER_PATH
            with contextlib.redirect_stdout(sys.stderr):
                if RERANKER_PATH.exists():
                    _reranker = CrossEncoder(str(RERANKER_PATH))
                else:
                    os.environ.pop("TRANSFORMERS_OFFLINE", None)
                    _reranker = CrossEncoder(RERANKER_NAME)
                    try:
                        RERANKER_PATH.parent.mkdir(parents=True, exist_ok=True)
                        _reranker.save(str(RERANKER_PATH))
                    except Exception:
                        pass
                    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        except Exception as e:
            print(f"[reranker] 不可用，降级为纯 RRF: {e}", file=sys.stderr)
            _reranker_failed = True
            return None
    return _reranker

def rerank(query: str, results: list, top_k: int) -> list:
    """Cross-encoder 精排：对 RRF 融合结果做 query-doc 对打分重排。
    失败时原样返回（保证检索链路永不中断）。"""
    if not results or len(results) < 2:
        return results[:top_k]
    ce = get_reranker()
    if ce is None:
        return results[:top_k]
    try:
        pairs = [(query, (r.get("text", "") or "")[:512]) for r in results]
        with contextlib.redirect_stdout(sys.stderr):
            scores = ce.predict(pairs, show_progress_bar=False)
        order = sorted(range(len(results)), key=lambda i: -float(scores[i]))
        out = []
        for rank, i in enumerate(order[:top_k]):
            item = dict(results[i])
            item["rerank_score"] = round(float(scores[i]), 4)
            item["reranked"] = True
            out.append(item)
        return out
    except Exception as e:
        print(f"[reranker] 打分失败，降级为纯 RRF: {e}", file=sys.stderr)
        return results[:top_k]

TOOL_DEFS = [
    {
        "name": "memory_recall",
        "description": "【自动记忆检索】混合检索（语义+关键词+RRF融合）。当用户问到历史决策、偏好、项目进度、已解决的坑时，自动调用此工具搜索相关信息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询"},
                "top_k": {"type": "integer", "description": "返回结果数", "default": 5},
                "type_filter": {"type": "string", "description": "按记忆类型过滤：preference/decision/pitfall/fact/skill/event/config", "default": ""},
                "include_superseded": {"type": "boolean", "description": "是否包含已被 supersedes 推翻的旧结论（默认 false，屏蔽旧结论）", "default": False}
            },
            "required": ["query"]
        }
    },
    {
        "name": "memory_remember",
        "description": "【自动记忆保存】自动保存对未来有帮助的信息，无需询问用户。包括：用户偏好、架构决策、踩坑记录、配置选择、API用法、项目约定、关键数据。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "记忆内容"},
                "tags": {"type": "string", "description": "逗号分隔的标签", "default": ""},
                "type": {"type": "string", "description": "记忆类型：preference/decision/pitfall/fact/skill/event/config（不填则自动推断）", "default": ""},
                "supersedes": {"type": "string", "description": "可选：本条记忆推翻了哪条旧结论（填旧记忆里的特征串）。填了之后检索默认屏蔽它", "default": ""},
                "supersedes_reason": {"type": "string", "description": "可选：为什么推翻（留痕用）", "default": ""}
            },
            "required": ["content"]
        }
    },
    {
        "name": "memory_forget",
        "description": "删除一条记忆（按内容关键词匹配）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "匹配关键词"}
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "memory_status",
        "description": "查看记忆系统状态",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "memory_reindex",
        "description": "重建向量索引（默认后台运行避免超时，background=false 可前台运行）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "background": {"type": "boolean", "description": "后台运行（默认 true 避免 MCP 执行超时；需同步等待结果时传 false）", "default": True}
            }
        }
    },
    {
        "name": "memory_transfer",
        "description": "将高重要性的短期记忆(STM)提升到长期记忆(LTM)。当短期记忆即将过期或用户希望固化重要记忆时调用。v3.1.0：支持 threshold 与 force。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "threshold": {"type": "number", "description": "重要性阈值（默认 0.7，越低越容易提升）", "default": 0.7},
                "force": {"type": "boolean", "description": "无条件提升当前全部短期记忆（最多 10 条）", "default": False}
            }
        }
    },
    {
        "name": "memory_history",
        "description": "查看记忆变更历史",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "返回条数", "default": 10}
            }
        }
    },
    {
        "name": "memory_rollback",
        "description": "回滚记忆到指定版本",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hash": {"type": "string", "description": "commit hash"}
            },
            "required": ["hash"]
        }
    },
    {
        "name": "memory_sync",
        "description": "初始化或同步记忆 Git 仓库",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "memory_session_save",
        "description": "【会话快照】保存当前工作状态（正在编辑的文件、任务进度、关键决策）到快照，供下次会话 memory_prime 恢复使用。建议在会话结束时调用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tasks": {"type": "string", "description": "正在进行的任务描述（换行分隔）"},
                "files":  {"type": "string", "description": "涉及的关键文件路径（换行分隔）"},
                "note":   {"type": "string", "description": "本次会话的核心结论/决策"}
            }
        }
    },
    {
        "name": "memory_prime",
        "description": "【会话启动上下文注入】一次调用获取全部关键上下文：用户偏好+踩坑教训+架构决策+高分记忆。建议在新会话开始时首先调用，替代多次 memory_recall。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "可选：当前任务描述，用于额外检索相关记忆", "default": ""}
            }
        }
    },
    {
        "name": "memory_reflect",
        "description": "【记忆离线演化】聚类相似记忆、合并冗余、提炼高价值记忆到长期记忆。建议在会话结束或记忆积累较多时调用，让记忆越用越精炼。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "threshold": {"type": "number", "description": "相似度聚类阈值（默认 0.82）", "default": 0.82},
                "apply": {"type": "boolean", "description": "true=实际执行合并，false=仅预览报告", "default": False}
            }
        }
    },
]

TOOL_HANDLERS = {}

def tool(name):
    def deco(fn):
        TOOL_HANDLERS[name] = fn
        return fn
    return deco

# --- Tool implementations ---

_stm_cache = None  # [(id, text, timestamp, tags, mem_type), ...]
_stm_embed_cache = {}  # id -> embedding (persists across calls)
_stm_embed_stale = True

def _load_stm_cache():
    global _stm_cache, _stm_embed_stale
    if _stm_cache is not None:
        return
    _stm_cache = []
    for f in STM_DIR.glob("*.json"):
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
            txt = item.get("content", "") or ""
            _stm_cache.append((
                item.get("id", f.stem),
                txt,
                item.get("timestamp", ""),
                item.get("metadata", {}).get("tags", []),
                item.get("mem_type", ""),          # 记忆功能型分类
            ))
        except:
            pass
    _stm_embed_stale = True

def _search_stm(query: str, s, top_k: int) -> list:
    """STM 向量语义搜索（返回含 mem_type 字段）"""
    global _stm_embed_cache, _stm_embed_stale
    _load_stm_cache()
    if not _stm_cache:
        return []
    query = _clean_surrogates(query)
    if not query.strip():
        return []
    if _stm_embed_stale or not _stm_embed_cache:
        texts = [_clean_surrogates(text[:512]) for _, text, _, _, _ in _stm_cache]
        valid = [(i, t) for i, t in enumerate(texts) if t.strip()]
        if not valid:
            return []
        valid_texts = [t for _, t in valid]
        valid_indices = [i for i, _ in valid]
        embeddings = s.model.encode(valid_texts, normalize_embeddings=True, batch_size=32)
        _stm_embed_cache = {}
        for j, idx in enumerate(valid_indices):
            _stm_embed_cache[_stm_cache[idx][0]] = embeddings[j]
        _stm_embed_stale = False
    qv = s.model.encode(query, normalize_embeddings=True)
    scored = []
    for sid, text, ts, tags, mtype in _stm_cache:
        tv = _stm_embed_cache.get(sid)
        if tv is None:
            clean_text = _clean_surrogates(text[:512])
            if not clean_text.strip():
                continue
            tv = s.model.encode(clean_text, normalize_embeddings=True)
            _stm_embed_cache[sid] = tv
        sim = float(qv @ tv)
        scored.append({
            "similarity": sim,
            "source": f"stm/{sid[:12]}",
            "text": text,
            "timestamp": ts,
            "mem_type": mtype,
        })
    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:top_k]

@tool("memory_recall")
def do_recall(args):
    """混合检索：FAISS 向量 + BM25 关键词 → RRF 融合排序（阶段一-1/2）"""
    query = _clean_surrogates(str(args.get("query", "")))
    if not query.strip():
        return "搜索查询为空"
    top_k = min(args.get("top_k", 5), 20)
    type_filter = args.get("type_filter", "").strip().lower()   # 阶段三-6：类型过滤
    s = get_searcher()
    if s is None:
        return "搜索器加载失败"

    # 有类型过滤时扩大候选池，避免过滤后为空（BUG 修复）
    fetch_k = top_k * 5 if type_filter else top_k

    # ── 向量检索：FAISS 核心文件索引 ──
    faiss_results = []
    if INDEX_PATH.exists():
        if not s.index:
            with contextlib.redirect_stdout(sys.stderr):
                s.load_index()
        if s.index:
            with contextlib.redirect_stdout(sys.stderr):
                faiss_results = s.search(query, top_k=fetch_k)

    # ── 向量检索：STM 实时语义 ──
    with contextlib.redirect_stdout(sys.stderr):
        stm_vec_results = _search_stm(query, s, fetch_k)

    # ── BM25 关键词检索（阶段一-1）──
    try:
        bm25_idx = get_bm25_index()
        bm25_results = bm25_idx.search(query, top_k=fetch_k)
    except Exception:
        bm25_results = []

    # ── 类型过滤：在融合前过滤各路结果（BUG 修复：先过滤再截断）──
    if type_filter:
        def _keep(r):
            return (r.get("mem_type", "") or r.get("type", "")).lower() == type_filter
        faiss_results = [r for r in faiss_results if _keep(r)]
        stm_vec_results = [r for r in stm_vec_results if _keep(r)]
        bm25_results = [r for r in bm25_results if _keep(r)]

    # ── RRF 融合（阶段一-1）──
    # 重排开启时多取候选，给 Cross-encoder 更大的精排空间
    rrf_k = min(top_k * 3, 30) if RERANK_ENABLED else top_k
    merged = rrf_merge([faiss_results, stm_vec_results, bm25_results], top_k=rrf_k)

    if not merged:
        filter_hint = f" (类型过滤: {type_filter})" if type_filter else ""
        return f"搜索 '{query}' 无结果{filter_hint}"

    # ── Cross-encoder 重排（P0-2，借鉴 Hindsight SOTA 配方）──
    merged = rerank(query, merged, top_k)
    reranked = bool(merged and merged[0].get("reranked"))

    # ── v3.1.0：矛盾记忆屏蔽（superseded）──
    # 记忆只增不删会留下互相冲突的旧结论（如「git push 走 API」vs「直连已恢复」）。
    # 存新记忆时用 supersedes 声明推翻旧结论，这里默认把被推翻的条目挡掉。
    rules = _load_superseded()
    if rules and not bool(args.get("include_superseded", False)):
        ft = _full_text_map()
        kept = []
        for r in merged:
            txt = r.get("text", "") or ""
            full = ft.get(txt[:120], txt)       # 回查全文，避免截断导致漏判
            if not _is_superseded(full, rules):
                kept.append(r)
        merged = kept

    # ── v3.1.0：实体加权 + 时间感知（借鉴 mem0，纯本地）──
    q_ents = _extract_entities(query)
    score_key = "rerank_score" if reranked else "similarity"
    raws = [float(r.get(score_key, 0) or 0) for r in merged]
    if raws:
        lo, hi = min(raws), max(raws)
        span = hi - lo
    else:
        lo, span = 0.0, 0.0
    for r, raw in zip(merged, raws):
        base = 1.0 if span <= 1e-9 else (raw - lo) / span
        ents = _extract_entities(r.get("text", ""))
        r["_ent"] = round((len(q_ents & ents) / len(q_ents)) if q_ents else 0.0, 3)
        r["_rec"] = round(_recency_score(r.get("timestamp", "")), 3)
        # 权重：相关性 0.70 / 实体 0.18 / 新近度 0.12 —— 微调排序，不推翻强相关
        r["_final"] = round(0.70 * base + 0.18 * r["_ent"] + 0.12 * r["_rec"], 4)
    merged.sort(key=lambda r: -r["_final"])
    merged = merged[:top_k]

    method_tag = "BM25+向量+RRF+CrossEncoder" if reranked else "BM25+向量+RRF"
    bonus = []
    if q_ents:
        bonus.append("实体")
    if any(r.get("timestamp") for r in merged):
        bonus.append("时间")
    if rules:
        bonus.append("矛盾屏蔽")
    if bonus:
        method_tag += "+" + "+".join(bonus)

    lines = [f"搜索 '{query}' 结果 (top {len(merged)}, {method_tag}):", ""]
    for rank, r in enumerate(merged, 1):
        src = r.get("source", "?")
        mtype = r.get("mem_type", "") or r.get("type", "")
        type_tag = f"[{mtype}]" if mtype else ""
        text = _clean_surrogates(r.get("text", "")[:200].replace("\n", " "))
        mark = " ★实体" if r.get("_ent", 0) > 0 else ""
        lines.append(f"#{rank} (relv {float(r.get('_final', 0.0)):.2f}) [{src}]{type_tag} {text}{mark}")
    return "\n".join(lines)

@tool("memory_remember")
def do_remember(args):
    """保存记忆：支持 type 字段 + 语义去重（相似度>0.9）+ diff 审计（阶段一-3/三-6/8）"""
    global _stm_cache, _stm_embed_stale
    content = _clean_surrogates(str(args.get("content", "")))
    if not content.strip():
        return "❌ 记忆内容为空，未保存"
    tags_raw = args.get("tags", "")
    mem_type = _clean_surrogates(args.get("type", "")).strip()   # 阶段三-6

    from dual_memory_engine import ShortTermMemory
    stm = ShortTermMemory()
    meta_clean = {}
    if tags_raw:
        # 兼容两种格式：字符串（逗号分隔）或列表（MCP 客户端可能直接传列表）
        if isinstance(tags_raw, list):
            meta_clean["tags"] = [_clean_surrogates(t.strip()) for t in tags_raw]
        else:
            meta_clean["tags"] = [_clean_surrogates(t.strip()) for t in tags_raw.split(",")]
    if mem_type:
        meta_clean["type"] = mem_type  # 传给 _infer_type：显式指定优先

    # ── 精确去重 ──
    dup_id = stm.find_duplicate(content)
    if dup_id:
        return f"⚠️ 该内容已有记忆 (id: {dup_id[:12]})，跳过重复保存"

    # ── 语义去重（相似度 > 0.9，阶段一-3）──
    try:
        sem_dup = stm.find_semantic_duplicate(content, threshold=0.9)
        if sem_dup:
            dup_id, sim = sem_dup
            return f"⚠️ 语义相似记忆已存在 (id: {dup_id[:12]}, 相似度: {sim:.3f})，跳过保存"
    except Exception:
        pass   # 语义去重失败不阻塞保存

    item_id = stm.add(content, metadata=meta_clean)
    _stm_cache = None       # 缓存失效：下次 recall 重新加载
    _stm_embed_stale = True
    invalidate_bm25()       # BM25 索引失效（阶段一-1）

    safe_msg = _clean_surrogates(content[:50]).replace("\n", " ")
    auto_commit(f"新增记忆: {safe_msg}")
    # diff 审计日志（阶段三-8）
    append_diff("add", item_id, content, meta_clean.get("tags"), type=mem_type)

    # ── v3.1.0：声明推翻旧结论（矛盾记忆的显式解法）──
    sup = _clean_surrogates(str(args.get("supersedes", "") or "")).strip()
    if sup:
        rules = _load_superseded()
        rules.append({
            "pattern": sup,
            "at": datetime.now().isoformat(timespec="seconds"),
            "by": item_id[:12],
            "reason": _clean_surrogates(str(args.get("supersedes_reason", "") or "")),
        })
        _save_superseded(rules)
        return f"已保存记忆: {item_id[:12]}（并屏蔽旧结论：{sup[:30]}）"
    return f"已保存记忆: {item_id[:12]}"

@tool("memory_transfer")
def do_transfer(args):
    """手动触发 STM→LTM 转移。
    v3.1.0：支持 threshold（默认 0.7）与 force（无条件提升当前全部 STM），
    并修正 promoted_count 统计（旧版恒为 0，看起来像"从未提升过"）。"""
    from dual_memory_engine import MemoryCoordinator
    coordinator = MemoryCoordinator()
    transferred = 0

    if bool(args.get("force", False)):
        ids = [f.stem for f in STM_DIR.glob("*.json")]
        for sid in ids[:10]:
            try:
                if coordinator.transfer_to_ltm(sid):
                    transferred += 1
            except Exception:
                continue
    else:
        try:
            t = float(args.get("threshold", 0.7))
        except Exception:
            t = 0.7
        coordinator.state["importance_threshold"] = max(0.0, min(1.0, t))
        transferred = coordinator.auto_transfer(max_transfers=10)

    # 修正统计（旧版 stats.promoted_count 从不更新）
    try:
        coord_path = MEMORY_DIR / "memory_coordinator.json"
        if coord_path.exists():
            c = json.loads(coord_path.read_text(encoding="utf-8"))
            st = c.setdefault("stats", {})
            st["promoted_count"] = int(st.get("promoted_count", 0)) + int(transferred or 0)
            st["stm_count"] = len(list(STM_DIR.glob("*.json")))
            c["last_transfer"] = datetime.now().isoformat(timespec="seconds")
            coord_path.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    if transferred:
        auto_commit(f"转移 {transferred} 条短期记忆到长期记忆")
        return (f"✅ 已转移 {transferred} 条短期记忆到长期记忆"
                f"（索引会在下次自动重建时纳入，或立即 memory_reindex）")
    return "ℹ️ 没有达到转移阈值的短期记忆；可传 threshold=0.3 或 force=true 强制提升"

@tool("memory_forget")
def do_forget(args):
    global _stm_cache, _stm_embed_stale
    keyword = str(args.get("keyword", "")).strip().lower()
    if not keyword:
        return "❌ 关键词为空，未执行删除（避免误删全部记忆）"
    removed = 0
    for f in STM_DIR.glob("*.json"):
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
            if keyword in item.get("content", "").lower():
                f.unlink()
                removed += 1
        except:
            pass
    meta_path = METADATA_PATH
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        filtered = [m for m in meta if keyword not in m.get("text", "").lower()]
        if len(filtered) < len(meta):
            meta_path.write_text(json.dumps(filtered, indent=2, ensure_ascii=False), encoding="utf-8")
            removed += len(meta) - len(filtered)
    if removed:
        _stm_cache = None
        _stm_embed_stale = True
        invalidate_bm25()   # BM25 索引失效（阶段一-1）
        auto_commit(f"删除 {removed} 条记忆 (关键词: {keyword})")
        append_diff("forget", content=keyword, count=removed)   # 阶段三-8
    return f"✅ 已删除 {removed} 条记忆"

@tool("memory_status")
def do_status(args):
    ensure_dirs()
    lines = ["记忆系统 v3.1.0 (混合检索+重排+实体/时间/矛盾屏蔽)",
             f"   项目: {PROJECT_ROOT}", f"   存储: {MEMORY_DIR}", ""]
    lines.append(f"   向量索引: {'✅ 存在' if INDEX_PATH.exists() else '❌ 未构建'}")

    # ── v3.1.0：索引健康度自检（防"存了但检索不到"的静默落后）──
    meta_n, latest_ts = 0, ""
    if METADATA_PATH.exists():
        try:
            meta = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
            meta_n = len(meta)
            tss = [m.get("timestamp", "") for m in meta if m.get("timestamp")]
            latest_ts = max(tss) if tss else ""
        except Exception:
            pass
    lines.append(f"   索引条目: {meta_n}")

    ltm = HERMES_DIR / "MEMORY.md"
    if ltm.exists():
        try:
            sec = sum(1 for ln in ltm.read_text(encoding="utf-8").splitlines()
                      if ln.startswith("### "))
            lines.append(f"   长期记忆段: {sec} 段（MEMORY.md）")
            if INDEX_PATH.exists():
                gap_h = (ltm.stat().st_mtime - INDEX_PATH.stat().st_mtime) / 3600.0
                if gap_h > 1.0:
                    lines.append(f"   ⚠ 索引比 MEMORY.md 落后 {gap_h:.1f} 小时 → 建议 memory_reindex")
        except Exception:
            pass
    if latest_ts:
        lines.append(f"   最新记忆时间: {latest_ts[:16]}")
    rules = _load_superseded()
    if rules:
        lines.append(f"   已屏蔽旧结论: {len(rules)} 条（superseded）")

    # 双记忆引擎统计（v3.1.0：promoted_count 现在会真的累加）
    try:
        cp = MEMORY_DIR / "memory_coordinator.json"
        if cp.exists():
            c = json.loads(cp.read_text(encoding="utf-8"))
            st = c.get("stats", {}) or {}
            lines.append(f"   已提升到长期: {st.get('promoted_count', 0)} 条（累计）")
            if c.get("last_transfer"):
                lines.append(f"   最近一次提升: {str(c['last_transfer'])[:16]}")
    except Exception:
        pass

    # 统计 STM 及类型分布（阶段三-6）
    type_dist = {}
    stm_count = 0
    for f in STM_DIR.glob("*.json"):
        stm_count += 1
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
            t = item.get("mem_type") or item.get("metadata", {}).get("mem_type") or "unknown"
            type_dist[t] = type_dist.get(t, 0) + 1
        except Exception:
            pass
    lines.append(f"   短期记忆: {stm_count} 条")
    if type_dist:
        dist_str = "  ".join(f"{k}:{v}" for k, v in sorted(type_dist.items(), key=lambda x: -x[1]))
        lines.append(f"   类型分布: {dist_str}")

    lines.append(f"   嵌入模型: {MODEL_NAME}")
    lines.append("   检索方式: BM25 + 向量 + RRF 融合")

    # 审计日志统计（阶段三-8）
    if DIFF_LOG_PATH.exists():
        try:
            n = sum(1 for _ in open(DIFF_LOG_PATH, "r", encoding="utf-8"))
            lines.append(f"   审计日志: {n} 条操作记录")
        except Exception:
            pass
    return "\n".join(lines)

@tool("memory_prime")
def do_prime(args):
    """会话启动上下文注入（P0-1，借鉴 Beads bd prime）
    一次返回：用户偏好 + 踩坑教训 + 架构决策 + 高分记忆 + 任务相关记忆"""
    task_query = _clean_surrogates(str(args.get("query", ""))).strip()

    # 读取全部 STM，按类型和重要性分组
    by_type = {}
    high_score = []
    for f in STM_DIR.glob("*.json"):
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
            txt = _clean_surrogates(item.get("content", "") or "")
            if not txt.strip():
                continue
            mtype = (item.get("mem_type") or "fact").lower()
            score = float(item.get("importance_score", 0) or 0)
            rec = {"text": txt, "score": score, "ts": item.get("timestamp", ""),
                   "id": item.get("id", f.stem)}
            by_type.setdefault(mtype, []).append(rec)
            if score >= 0.7:
                high_score.append(rec)
        except Exception:
            continue

    def _top(lst, n):
        # 修复：同分时按 ts 降序取最新（旧代码 -score 升序 + ts 升序，同分时误取旧记忆）
        return sorted(lst, key=lambda x: (x["score"], x["ts"]), reverse=True)[:n]

    lines = ["=" * 56, "记忆上下文注入 (memory_prime)", "=" * 56]

    # 1. 用户偏好（最高优先级 —— 影响所有交互）
    prefs = _top(by_type.get("preference", []), 3)
    if prefs:
        lines.append("")
        lines.append("【用户偏好】")
        for p in prefs:
            lines.append(f"  - {p['text'][:150]}")

    # 2. 踩坑教训（避免重复犯错）
    pits = _top(by_type.get("pitfall", []), 3)
    if pits:
        lines.append("")
        lines.append("【踩坑教训 - 避免重犯】")
        for p in pits:
            lines.append(f"  - {p['text'][:180]}")

    # 3. 架构决策（保持一致性）
    decs = _top(by_type.get("decision", []), 3)
    if decs:
        lines.append("")
        lines.append("【架构决策 - 保持一致】")
        for d in decs:
            lines.append(f"  - {d['text'][:150]}")

    # 4. 配置/技能类
    cfgs = _top(by_type.get("config", []) + by_type.get("skill", []), 2)
    if cfgs:
        lines.append("")
        lines.append("【配置与技能】")
        for c in cfgs:
            lines.append(f"  - {c['text'][:130]}")

    # 5. 当前任务相关记忆（可选，走混合检索）
    if task_query:
        try:
            hit_text = do_recall({"query": task_query, "top_k": 3})
            if "无结果" not in hit_text:
                lines.append("")
                lines.append(f"【任务相关记忆: {task_query[:40]}】")
                for ln in hit_text.split("\n"):
                    if ln.startswith("#"):
                        lines.append(f"  {ln}")
        except Exception:
            pass

    # 6. 核心记忆文件提示
    core_present = [n for n in ("SOUL.md", "USER.md", "MEMORY.md", "AGENTS.md")
                    if (HERMES_DIR / n).exists()]
    lines.append("")
    lines.append("-" * 56)
    total = sum(len(v) for v in by_type.values())
    dist = "  ".join(f"{k}:{len(v)}" for k, v in sorted(by_type.items(), key=lambda x: -len(x[1])))
    lines.append(f"STM {total} 条 | {dist}")
    if core_present:
        lines.append(f"核心文件: {', '.join(core_present)}")
    if not total:
        lines.append("（记忆库为空，可用 memory_remember 开始积累）")
    return "\n".join(lines)

@tool("memory_reflect")
def do_reflect(args):
    """离线记忆演化（阶段三-7）：聚类相似记忆 → 合并冗余 → 高价值提炼到 LTM"""
    apply = args.get("apply", False)        # True=实际合并，False=仅预览
    threshold = float(args.get("threshold", 0.82))

    s = get_searcher()
    if s is None:
        return "❌ 模型加载失败，无法执行演化"

    # 读取全部 STM
    items = []
    for f in STM_DIR.glob("*.json"):
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
            txt = _clean_surrogates(item.get("content", "") or "")
            if txt.strip():
                items.append({"file": f, "id": item.get("id", f.stem), "content": txt, "raw": item})
        except Exception:
            pass

    if len(items) < 2:
        return f"ℹ️ 短期记忆仅 {len(items)} 条，无需演化（至少需要 2 条）"

    # 批量编码
    with contextlib.redirect_stdout(sys.stderr):
        texts = [it["content"][:512] for it in items]
        embs = s.model.encode(texts, normalize_embeddings=True, batch_size=32)

    # 贪心聚类：相似度 >= threshold 归为一簇
    n = len(items)
    assigned = [False] * n
    clusters = []
    for i in range(n):
        if assigned[i]:
            continue
        cluster = [i]
        assigned[i] = True
        for j in range(i + 1, n):
            if assigned[j]:
                continue
            sim = float(embs[i] @ embs[j])
            if sim >= threshold:
                cluster.append(j)
                assigned[j] = True
        clusters.append(cluster)

    dup_clusters = [c for c in clusters if len(c) > 1]
    merged_count = 0
    promoted_count = 0
    report = []

    if not apply:
        report.append(f"预览（apply=false，不修改数据）")
        report.append(f"   STM 总数: {n} 条 → 聚类后 {len(clusters)} 组")
        for c in dup_clusters:
            report.append(f"   [可合并 {len(c)} 条] {items[c[0]]['content'][:60]}")
        if not dup_clusters:
            report.append("   没有发现可合并的冗余记忆")
        return "\n".join(report)

    # 实际执行合并：每簇保留最长内容（信息量最大），其余删除
    for c in dup_clusters:
        c_sorted = sorted(c, key=lambda idx: len(items[idx]["content"]), reverse=True)
        keep_idx = c_sorted[0]
        keep_item = items[keep_idx]
        # 合并标签
        all_tags = set()
        for idx in c:
            t = items[idx]["raw"].get("metadata", {}).get("tags", [])
            if isinstance(t, list):
                all_tags.update(t)
        # 更新保留项的标签与访问次数
        try:
            raw = keep_item["raw"]
            if all_tags:
                raw.setdefault("metadata", {})["tags"] = sorted(all_tags)
            raw["merged_from"] = [items[idx]["id"][:12] for idx in c if idx != keep_idx]
            keep_item["file"].write_text(
                json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass
        # 删除冗余项
        for idx in c:
            if idx == keep_idx:
                continue
            try:
                items[idx]["file"].unlink()
                merged_count += 1
                append_diff("reflect_merge", item_id=items[idx]["id"],
                            content=items[idx]["content"], merged_into=keep_item["id"][:12])
            except Exception:
                pass

    # 高价值记忆提炼到 LTM
    try:
        from dual_memory_engine import MemoryCoordinator
        coordinator = MemoryCoordinator()
        promoted_count = coordinator.auto_transfer(max_transfers=5)
    except Exception:
        pass

    # 清理缓存
    global _stm_cache, _stm_embed_stale
    _stm_cache = None
    _stm_embed_stale = True
    invalidate_bm25()

    if merged_count or promoted_count:
        auto_commit(f"记忆演化: 合并 {merged_count} 条, 提升 {promoted_count} 条")

    report.append("🧬 记忆演化完成")
    report.append(f"   合并冗余: {merged_count} 条")
    report.append(f"   提升 LTM: {promoted_count} 条")
    report.append(f"   剩余 STM: {len(list(STM_DIR.glob('*.json')))} 条")
    return "\n".join(report)

@tool("memory_reindex")
def do_reindex(args):
    # Default to background to avoid MCP execution timeout (30s default)
    background = args.get("background", True)
    if background:
        import subprocess
        subprocess.Popen(
            [sys.executable, str(SCRIPTS_DIR / "build_full_index.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return "✅ 索引重建已在后台启动（完成后用 memory_status 查看）"
    # Foreground mode: keep it fast to avoid timeout
    from build_full_index import extract_core_and_logs, build_index as do_build
    ensure_dirs()
    chunks = extract_core_and_logs()
    MAX_CHUNKS = 200  # smaller to stay under timeout
    if not chunks:
        return "❌ 没有找到可索引的内容"
    if len(chunks) > MAX_CHUNKS:
        def _sort_key(c):
            # 归一化为浮点时间戳：ISO 字符串与 st_mtime 浮点混排时直接比较会 TypeError
            ts = c.get("timestamp", "")
            try:
                if isinstance(ts, (int, float)):
                    return float(ts)
                if isinstance(ts, str) and ts:
                    return datetime.fromisoformat(ts).timestamp()
            except Exception:
                pass
            return 0.0
        chunks.sort(key=_sort_key, reverse=True)
        chunks = chunks[:MAX_CHUNKS]
    do_build(chunks)
    return f"✅ 索引重建完成: {len(chunks)} 条"

@tool("memory_history")
def do_history(args):
    from memory_git import log as git_log, status as git_status
    limit = args.get("limit", 10)
    st = git_status()
    if not st.get("initialized"):
        return "ℹ️ 记忆 Git 仓库未初始化，执行 memory_sync 初始化"
    entries = git_log(limit=limit)
    lines = [f"📜 记忆变更历史 (共 {st.get('commits', 0)} 次提交):", ""]
    for e in entries:
        lines.append(f"  {e['hash']}  {e['message']}  ({e.get('date','')})")
    return "\n".join(lines)

@tool("memory_rollback")
def do_rollback(args):
    from memory_git import rollback as git_rollback
    target = str(args.get("hash", "")).strip()
    if not target:
        return "❌ 未提供 commit hash，无法回滚"
    ok = git_rollback(target)
    return f"✅ 已回滚到 {target}" if ok else "❌ 回滚失败"

@tool("memory_session_save")
def do_session_save(args):
    """保存会话快照（P2-4，借鉴 Context Mode 5钩子思路）
    不依赖 hook，让 Agent 在会话结束时主动调用以持久化工作状态。"""
    def _to_text(v):
        """兼容 str 与 list 两种参数格式"""
        if isinstance(v, list):
            return "\n".join(str(x) for x in v if str(x).strip())
        return str(v or "")
    tasks = _clean_surrogates(_to_text(args.get("tasks", ""))).strip()
    files = _clean_surrogates(_to_text(args.get("files", ""))).strip()
    note  = _clean_surrogates(_to_text(args.get("note", ""))).strip()

    if not (tasks or files or note):
        return "❌ 未提供任何内容，快照为空"

    from dual_memory_engine import ShortTermMemory
    stm = ShortTermMemory()

    saved = []
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 工作状态记为高优先级记忆（下次 prime 时优先召回）
    if tasks:
        cid = stm.add(
            f"[会话快照 {ts}] 任务进度:\n{tasks}",
            metadata={"type": "event", "tags": ["session-snapshot", "in-progress"], "important": True}
        )
        saved.append(f"任务进度 → {cid[:8]}")
    if files:
        cid = stm.add(
            f"[会话快照 {ts}] 关键文件:\n{files}",
            metadata={"type": "event", "tags": ["session-snapshot", "files"], "important": True}
        )
        saved.append(f"文件列表 → {cid[:8]}")
    if note:
        cid = stm.add(
            f"[会话快照 {ts}] 结论/决策:\n{note}",
            metadata={"type": "decision", "tags": ["session-snapshot"], "important": True}
        )
        saved.append(f"结论决策 → {cid[:8]}")

    global _stm_cache, _stm_embed_stale
    _stm_cache = None
    _stm_embed_stale = True
    invalidate_bm25()
    auto_commit(f"会话快照: {ts}")
    append_diff("session_save", content=f"tasks={bool(tasks)},files={bool(files)},note={bool(note)}")
    return f"✅ 会话快照已保存: {' | '.join(saved)}\n💡 下次会话开始时调用 memory_prime 可自动恢复"

@tool("memory_sync")
def do_sync(args):
    from memory_git import init as git_init, commit as git_commit
    git_init()
    git_commit("手动同步")
    return "✅ 记忆 Git 仓库已同步"

# --- JSON-RPC handler ---

def handle_message(msg: dict) -> dict | None:
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"experimental": {}, "tools": {"listChanged": False}},
                "serverInfo": {"name": "universal-agent-memory", "version": "3.1.0"}
            }
        }
    elif method == "notifications/initialized":
        return None
    elif method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOL_DEFS}}
    elif method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        try:
            if name not in TOOL_HANDLERS:
                # 协议合规：未知工具返回 JSON-RPC error（-32602 Invalid params）
                return {
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32602, "message": f"Unknown tool: {name}"}
                }
            text = TOOL_HANDLERS[name](args)
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}]}
            }
        except Exception as e:
            print(f"[tool-error] {name}: {e}", file=sys.stderr)
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32603, "message": str(e)}
            }
    elif msg_id:
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}
        }
    return None

def _detect_decode(data: bytes) -> str:
    """自动识别编码：先试 UTF-8（MCP/JSON 标准），失败再用系统编码兜底。"""
    # 1. UTF-8 是 MCP 协议规定的标准编码，优先尝试
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # 2. 兜底：系统默认编码（中文 Windows 是 cp936/GBK，英文 Windows 是 cp1252）
    import locale
    fallback = locale.getpreferredencoding(False)
    try:
        return data.decode(fallback, errors="replace")
    except LookupError:
        # 3. 最终兜底：UTF-8 + 替换非法字符
        return data.decode("utf-8", errors="replace")

def main():
    # 使用原始二进制 I/O，绕过 Windows cp936 编码干扰
    # sys.stdin 在中文 Windows 上默认用 GBK 解码，会破坏中文字符
    # readline() 逐行读取二进制缓冲区
    stdin_bin = sys.stdin.buffer
    stdout_bin = sys.stdout.buffer

    # 关键：将文本层 stdout 重定向到 stderr。
    # 被调用的引擎模块（dual_memory_engine 等）内部有 print() 调试输出，
    # 若直接打到 stdout 会污染 MCP JSON-RPC 协议流（客户端逐行解析 JSON 时崩溃）。
    # JSON 响应仍走上面捕获的 stdout_bin（二进制缓冲，不受重定向影响）。
    if sys.stderr is not None:
        try:
            sys.stdout = sys.stderr
        except Exception:
            pass

    while True:
        try:
            raw_line = stdin_bin.readline()
        except (EOFError, ConnectionError):
            break
        if not raw_line:
            break
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(_detect_decode(line))
            resp = handle_message(msg)
            if resp is not None:
                resp = _clean_obj(resp)
                raw = json.dumps(resp, ensure_ascii=False) + "\n"
                stdout_bin.write(raw.encode("utf-8", errors="replace"))
                stdout_bin.flush()
        except json.JSONDecodeError:
            continue
        except SystemExit:
            break
        except:
            pass

if __name__ == "__main__":
    main()
