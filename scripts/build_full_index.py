#!/usr/bin/env python3
"""
嘟嘟记忆系统增强脚本 - 将所有核心记忆与日志编入向量索引
"""
import os, sys, json, re, time
from pathlib import Path
from datetime import datetime

# 注意：sentence-transformers / faiss / numpy 一律不在模块顶层导入（见 v3.2.1）。
# extract_core_and_logs() 是纯文本抽取，MCP 的 memory_reindex 会直接 import 本模块；
# 若顶层硬导入，在 `pip install --target` 这类部署形态下（依赖目录不在 sys.path）
# 整个模块 import 就会失败，连纯文本抽取都用不了，且报错栈毫无指向性。

# Safe UTF-8 stdout for Windows GBK consoles
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8", "utf_8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))
from memory_config import (
    MODEL_NAME, MODEL_PATH, MEMORY_DIR, HERMES_DIR,
    INDEX_PATH, METADATA_PATH, CORE_FILES, DAILY_DIR,
    MAX_CHUNK_CHARS, ensure_dirs, PROJECT_ROOT,
    write_index_safe
)

def extract_core_and_logs():
    """从核心记忆文件和日志中提取文本块"""
    chunks = []

    for name in CORE_FILES:
        fpath = HERMES_DIR / name
        if not fpath.exists():
            fpath = PROJECT_ROOT / name
        if fpath.exists():
            text = fpath.read_text(encoding='utf-8')
            sections = re.split(r'\n(?=#)', text)
            for i, sec in enumerate(sections):
                if len(sec.strip()) > 50:
                    chunks.append({
                        "id": f"core/{name}:{i}",
                        "text": sec.strip(),
                        "source": name,
                        "type": "core_memory",
                        "timestamp": datetime.now().isoformat()
                    })

    if DAILY_DIR.exists():
        seen = set()
        for fpath in sorted(DAILY_DIR.glob("*.md")):
            try:
                text = fpath.read_text(encoding='utf-8').strip()
                if len(text) <= 30:
                    continue
                # v3.2.3：按「问答轮次」切分（一条用户提问 + 其后的助手回复 = 1 个 chunk）。
                # 旧版整文件 1 个 chunk 且截断到 MAX_CHUNK_CHARS，一天里大部分会话检索不到；
                # 中间版本按单条消息切又会把问答拆散、块数翻倍。
                # 轮次切分兼顾语义完整与索引体积。
                sections = re.split(r'\n(?=### 用户 )', text)
                for si, sec in enumerate(sections):
                    sec = sec.strip()
                    if len(sec) < 40:
                        continue
                    # 跳过纯文件头（不含任何消息）
                    if "### " not in sec:
                        continue
                    key = sec[:50]
                    if key in seen:
                        continue
                    seen.add(key)
                    # 轮次超长时保留「提问 + 结论」两段，丢弃中间过程
                    if len(sec) > MAX_CHUNK_CHARS:
                        half = MAX_CHUNK_CHARS // 2
                        sec = sec[:half].rstrip() + "\n…\n" + sec[-half:].lstrip()
                    chunks.append({
                        "id": f"daily/{fpath.name}:{si}",
                        "text": sec,
                        "source": f"daily/{fpath.name}",
                        "type": "daily_log",
                        "timestamp": datetime.now().isoformat()
                    })
            except Exception as e:
                print(f"  ⚠ {fpath.name}: {e}")

    # --- 【补充】读取 MCP 记忆系统各目录（STM/short_term/scenarios/atoms/personas） ---
    # 修复：此前只扫核心文件 + daily 日志，STM 真实记忆从不进索引，语义检索长期不完整
    for sub in ["stm", "short_term", "scenarios", "archived", "personas", "atoms"]:
        d = MEMORY_DIR / sub
        if not d.exists():
            continue
        for fpath in sorted(d.glob("*")):
            if not fpath.is_file():
                continue
            try:
                if fpath.suffix == ".json":
                    data = json.loads(fpath.read_text(encoding='utf-8'))
                    if isinstance(data, dict) and data.get("content"):
                        text = str(data["content"]).strip()
                    elif isinstance(data, list):
                        text = json.dumps(data, ensure_ascii=False)[:MAX_CHUNK_CHARS]
                    else:
                        continue
                else:
                    text = fpath.read_text(encoding='utf-8').strip()
                if len(text) > 20:
                    chunks.append({
                        "id": f"{sub}/{fpath.name}",
                        "text": text[:MAX_CHUNK_CHARS],
                        "source": f"{sub}/{fpath.name}",
                        "type": "memory_" + sub,
                        "timestamp": fpath.stat().st_mtime
                    })
            except Exception as e:
                print(f"  ⚠ {sub}/{fpath.name}: {e}")

    print(f"  核心记忆: {sum(1 for c in chunks if c['type']=='core_memory')} 块")
    print(f"  日志: {sum(1 for c in chunks if c['type']=='daily_log')} 条")
    print(f"  MCP记忆: {sum(1 for c in chunks if c['type'].startswith('memory_'))} 条")
    return chunks

def _require_embed_deps():
    """按需导入向量依赖，缺谁报谁（v3.2.1）。

    返回 (np, faiss, SentenceTransformer)。缺依赖时抛 ImportError，
    消息里直接给可执行的修复命令 —— 调用方（MCP memory_reindex）会把这句话
    原样透给用户，而不是丢一个找不到源头的 ModuleNotFoundError。
    """
    missing = []
    try:
        import numpy as np
    except ImportError:
        np = None
        missing.append("numpy")
    try:
        import faiss
    except ImportError:
        faiss = None
        missing.append("faiss")
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        SentenceTransformer = None
        missing.append("sentence-transformers")
    if missing:
        raise ImportError(
            "缺少向量依赖: " + ", ".join(missing)
            + "。修复: pip install " + " ".join(missing)
            + "；若依赖装在 pip --target 目录，请设置 PIP_TARGET 或 "
            "UAM_EXTRA_SYS_PATH 指向该目录后重启 MCP"
        )
    return np, faiss, SentenceTransformer


def build_index(chunks):
    """构建Faiss向量索引（重依赖在此处才导入）"""
    np, faiss, SentenceTransformer = _require_embed_deps()
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    print(f"\n🧠 加载模型: {MODEL_NAME}")
    if MODEL_PATH.exists():
        model = SentenceTransformer(str(MODEL_PATH))
    else:
        # 未缓存：在线下载；失败则显式报错（不再静默回退旧英文模型）
        try:
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
            model = SentenceTransformer(MODEL_NAME)
            MODEL_PATH.mkdir(parents=True, exist_ok=True)
            model.save(str(MODEL_PATH))
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        except Exception as e:
            raise RuntimeError(f"嵌入模型不可用且在线下载失败: {e}") from e

    texts = [c["text"] for c in chunks]
    if not texts:
        print("❌ 没有可索引的文本")
        return
    print(f"📊 生成 {len(texts)} 个嵌入向量...")

    embeddings = model.encode(texts, show_progress_bar=False, batch_size=32)
    embeddings = np.asarray(embeddings, dtype=np.float32)
    if embeddings.ndim == 1:
        embeddings = embeddings.reshape(1, -1)
    embeddings = embeddings / np.linalg.norm(embeddings, axis=1, keepdims=True)

    dimension = embeddings.shape[1]
    index = faiss.IndexFlatIP(dimension)
    index.add(embeddings.astype(np.float32))

    write_index_safe(index, INDEX_PATH)
    with open(METADATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(chunks, f, indent=2, ensure_ascii=False)

    print(f"\n✅ 索引构建完成!")
    print(f"   总块数: {len(chunks)}")
    print(f"   维度: {dimension}")
    print(f"   索引: {INDEX_PATH}")
    print(f"   元数据: {METADATA_PATH}")

if __name__ == '__main__':
    print("=" * 50)
    print("🚀 AI Agent 记忆系统 - 全量索引构建")
    print("=" * 50)
    start = time.time()

    ensure_dirs()
    chunks = extract_core_and_logs()

    # 全量重建：只在本批 chunks 内部去重，不与旧元数据比对
    # （旧逻辑会把所有 chunk 判为已存在，导致索引重建为空 —— 已修复）
    seen_keys = set()
    uniq = []
    for c in chunks:
        key = (c.get("text", "") or "")[:80]
        if not key.strip() or key in seen_keys:
            continue
        seen_keys.add(key)
        uniq.append(c)
    if len(uniq) < len(chunks):
        print(f"  去重过滤: {len(chunks) - len(uniq)} 条（批内重复）")
    chunks = uniq

    MAX_CHUNKS = 500
    if len(chunks) > MAX_CHUNKS:
        def sort_key(c):
            # 统一归一化为浮点时间戳再比较：ISO 字符串与 st_mtime 浮点混合时，
            # 若按字符串排序会出现 "9..." 排在 "17..." 之后的字典序错误
            ts = c.get("timestamp", "")
            try:
                if isinstance(ts, (int, float)):
                    return float(ts)
                if isinstance(ts, str) and ts:
                    return datetime.fromisoformat(ts).timestamp()
            except Exception:
                pass
            return 0.0
        chunks.sort(key=sort_key, reverse=True)
        chunks = chunks[:MAX_CHUNKS]
        print(f"  📐 压缩至 {MAX_CHUNKS} 条（保留最新）")

    build_index(chunks)
    elapsed = time.time() - start
    print(f"\n⏱️ 耗时: {elapsed:.1f}s")
