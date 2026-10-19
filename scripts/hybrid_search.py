#!/usr/bin/env python3
"""
混合检索模块：BM25 关键词 + 向量语义 + RRF 融合排序
借鉴 mem0 / agentmemory / TencentDB-Agent-Memory 的混合检索设计。
无第三方依赖，纯标准库实现。
"""
import re
import json
import math
from pathlib import Path


# ---------------------------------------------------------------------------
# 中文感知分词：ASCII 词 + CJK 单字/双字
# ---------------------------------------------------------------------------
def tokenize(text: str) -> list:
    """中英文混合分词：英文按单词，中文按单字+双字（bigram）"""
    if not text:
        return []
    tokens = []
    lower = text.lower()
    # 英文单词 / 数字 / 代码标识符（含下划线、点、连字符）
    for w in re.findall(r"[a-z0-9_\-\.]+", lower):
        if len(w) >= 1:
            tokens.append(w)
    # 中日韩统一表意文字：单字 + 相邻双字
    cjk = re.findall(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]", text)
    if cjk:
        tokens.extend(cjk)
        for i in range(len(cjk) - 1):
            tokens.append(cjk[i] + cjk[i + 1])
    return tokens


# ---------------------------------------------------------------------------
# BM25 索引
# ---------------------------------------------------------------------------
class BM25Index:
    """增量式 BM25 索引（k1=1.5, b=0.75 标准参数）"""

    def __init__(self):
        self.docs = []          # [{id, text, source, mem_type, extra}]
        self.doc_count = 0      # N
        self.avgdl = 0.0
        self.df = {}            # term -> 文档频次
        self.postings = {}      # term -> {doc_idx: bm25_score}
        self._tokenized = []
        self._dirty = True

    def set_docs(self, docs: list):
        """docs: [{id, text, source, mem_type, extra}]"""
        self.docs = docs or []
        self._tokenized = []
        self.doc_count = len(self.docs)
        self.avgdl = 0.0
        self.df = {}
        self.postings = {}
        # 分词
        for d in self.docs:
            toks = tokenize(d.get("text", ""))
            self._tokenized.append(toks)
        if self.doc_count:
            self.avgdl = sum(len(t) for t in self._tokenized) / self.doc_count
        # 文档频次
        for toks in self._tokenized:
            for t in set(toks):
                self.df[t] = self.df.get(t, 0) + 1
        # BM25 得分
        N = self.doc_count
        for i, toks in enumerate(self._tokenized):
            dl = len(toks)
            for t in set(toks):
                tf = toks.count(t)
                idf = math.log((N - self.df[t] + 0.5) / (self.df[t] + 0.5) + 1.0)
                score = idf * (tf * (1.5 + 1)) / (tf + 1.5 * (1 - 0.75 + 0.75 * dl / max(self.avgdl, 1e-9)))
                self.postings.setdefault(t, {})[i] = score

    def search(self, query: str, top_k: int = 10) -> list:
        if not self.doc_count:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        scores = {}
        for t in q_tokens:
            for doc_idx, s in self.postings.get(t, {}).items():
                scores[doc_idx] = scores.get(doc_idx, 0) + s
        if not scores:
            return []
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        max_s = ranked[0][1] if ranked else 1.0
        results = []
        for doc_idx, s in ranked[:top_k]:
            d = self.docs[doc_idx]
            results.append({
                "text": d.get("text", "")[:500],
                "source": d.get("source", ""),
                "mem_type": d.get("mem_type", ""),
                "timestamp": d.get("timestamp", ""),
                "similarity": round(min(s / max_s, 1.0), 4),
                "extra": d.get("extra", {}),
            })
        return results


# ---------------------------------------------------------------------------
# RRF 融合（Reciprocal Rank Fusion）
# ---------------------------------------------------------------------------
def rrf_merge(result_lists: list, top_k: int, k: int = 60) -> list:
    """多路结果 RRF 融合：score(d) = Σ 1/(k + rank(d))，k 默认 60

    v3.2.2 修复：去重 key 从 text[:120] 改为完整 text。
    旧实现里 BM25 侧 build_from_stm 把 metadata 的 text 截到 800、
    search 截到 500，而向量侧返回完整 text —— 同一条记忆在两路的
    key 前缀相同但完整度不同，会被当成两条独立结果重复占位。
    用完整 text 做 key 后，只要内容一致就一定合并，不一致就保留两条（正确行为）。"""
    scores = {}
    items = {}
    for results in result_lists:
        for rank, item in enumerate(results):
            key = item.get("text", "")
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            if key not in items:
                items[key] = item
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    out = []
    for key, rrf_score in ranked[:top_k]:
        item = dict(items[key])
        item["similarity"] = round(rrf_score, 4)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 便捷构建：从 STM 目录 + 元数据构建 BM25 索引
# ---------------------------------------------------------------------------
def build_from_stm(stm_dir: Path, metadata_path: Path) -> BM25Index:
    """聚合 STM 文件和 FAISS 元数据为 BM25 语料"""
    docs = []
    # STM 短期记忆（实时）
    if stm_dir.exists():
        for f in sorted(stm_dir.glob("*.json")):
            try:
                item = json.loads(f.read_text(encoding="utf-8"))
                txt = item.get("content", "") or ""
                if txt.strip():
                    docs.append({
                        "id": f"stm/{item.get('id', f.stem)[:12]}",
                        "text": txt,
                        "source": f"stm/{item.get('id', f.stem)[:12]}",
                        "mem_type": item.get("mem_type", ""),
                        "timestamp": item.get("timestamp", ""),
                        "extra": {"stm_id": item.get("id", f.stem)},
                    })
            except Exception:
                continue
    # FAISS 元数据（核心文件索引）
    if metadata_path.exists():
        try:
            for entry in json.loads(metadata_path.read_text(encoding="utf-8")):
                txt = entry.get("text", "") or ""
                if txt.strip():
                    docs.append({
                        "id": entry.get("id", ""),
                        "text": txt[:800],
                        "source": entry.get("source", "?"),
                        "mem_type": entry.get("type", "core_memory"),
                        "timestamp": entry.get("timestamp", ""),
                        "extra": {},
                    })
        except Exception:
            pass
    index = BM25Index()
    index.set_docs(docs)
    return index
