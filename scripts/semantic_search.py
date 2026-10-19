#!/usr/bin/env python3
"""
Semantic Memory Search with Sentence Transformers
Uses pre-trained embeddings for high-quality semantic search
"""

import os
import sys
import json
from pathlib import Path
from typing import List, Dict
import time

# Check for required packages
try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    print("Error: sentence-transformers not installed")
    print("Please install: pip install sentence-transformers")
    sys.exit(1)

# faiss 统一在 try/except 中导入，避免顶层 import 失败直接崩溃
try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False

sys.path.insert(0, str(Path(__file__).parent))
from memory_config import (
    MODEL_NAME, MEMORY_DIR, INDEX_PATH, METADATA_PATH,
    MODEL_PATH, SCRIPTS_DIR, read_index_safe
)

class SemanticMemorySearch:
    """Semantic memory search system"""
    
    def __init__(self):
        self.model = None
        self.index = None
        self.metadata = []
        self.dimension = 384  # 默认值；load_model() 后更新为实际维度
        
    def load_model(self):
        """Load sentence transformer model from local cache only"""
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        start_time = time.time()

        # 新模型缓存目录（按模型名区分）
        model_cache = MODEL_PATH
        if model_cache.exists():
            self.model = SentenceTransformer(str(model_cache))
        else:
            # 未缓存：尝试在线下载（离线时抛错，由调用方处理）
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
            self.model = SentenceTransformer(MODEL_NAME)
            model_cache.mkdir(parents=True, exist_ok=True)
            self.model.save(str(model_cache))
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

        # 动态读取真实维度（换模型后不会因硬编码 384 崩溃）
        # 新版 sentence-transformers 已将该 API 更名为 get_embedding_dimension，
        # 旧名仍可用但每次加载都会打 FutureWarning，故优先新名、旧名兜底。
        try:
            getter = getattr(self.model, "get_embedding_dimension", None)
            if getter is None:
                getter = self.model.get_sentence_embedding_dimension
            real_dim = getter()
            if real_dim:
                self.dimension = int(real_dim)
        except Exception:
            pass

        elapsed = time.time() - start_time
        print(f"✅ Model loaded ({elapsed:.1f}s) dim={self.dimension} path={MODEL_PATH}")

    def load_index(self):
        """Load existing index"""
        print("\n📂 Loading existing index...")
        
        if not INDEX_PATH.exists():
            print("❌ Index not found. Run build_index() first.")
            return False
        
        if not METADATA_PATH.exists():
            print("❌ Metadata not found.")
            return False
        
        # Load model
        if not self.model:
            self.load_model()
        
        print(f"   Loading index from {INDEX_PATH}...")
        self.index = read_index_safe(INDEX_PATH)
        
        # Load metadata
        print(f"   Loading metadata from {METADATA_PATH}...")
        # 必须显式指定 UTF-8，否则中文 Windows 会用 GBK 解码导致崩溃
        with open(METADATA_PATH, 'r', encoding='utf-8') as f:
            self.metadata = json.load(f)
        
        print(f"✅ Index loaded: {len(self.metadata)} chunks")
        return True
    
    def search(self, query: str, top_k: int = 5) -> List[Dict]:
        """Search for similar chunks"""
        
        if not self.index:
            if not self.load_index():
                return []
        
        # Clean query and ensure it's a non-empty string
        if not isinstance(query, str):
            query = str(query) if query is not None else ""
        query = query.encode("utf-8", errors="replace").decode("utf-8")
        if not query.strip():
            return []
        
        # Encode query
        query_embedding = self.model.encode([query], convert_to_numpy=True)
        faiss.normalize_L2(query_embedding)
        
        actual_k = min(top_k, len(self.metadata))
        print(f"🔍 Searching for top {actual_k} results...")
        scores, indices = self.index.search(query_embedding, actual_k)

        results = []
        for i, (idx, score) in enumerate(zip(indices[0], scores[0])):
            if idx < len(self.metadata) and score > -1e10:
                results.append({
                    **self.metadata[idx],
                    "similarity": float(score),
                    "rank": i + 1
                })

        return results

def print_results(results: List[Dict]):
    """Pretty print search results"""
    if not results:
        print("\n❌ No results found.")
        return
    
    print(f"\n{'=' * 70}")
    print(f"📊 Top {len(results)} Results")
    print(f"{'=' * 70}\n")
    
    type_icons = {
        "core_memory": "📚",
        "daily_log": "📅",
        "dream_log": "🌙"
    }
    
    for result in results:
        icon = type_icons.get(result.get("type", ""), "📄")
        similarity = result.get("similarity", 0)
        
        print(f"{icon} Rank {result['rank']} (Relevance: {similarity:.4f})")
        print(f"   📄 Source: {result['source']}")
        print(f"   🔍 ID: {result['id']}")
        
        text = result['text']
        if len(text) > 300:
            text = text[:300] + "..."
        print(f"   💬 {text}\n")

def main():
    """Main CLI interface"""
    print("🚀 Semantic Memory Search System")
    print(f"   Model: {MODEL_NAME}")
    print(f"   Memory dir: {MEMORY_DIR}\n")
    
    searcher = SemanticMemorySearch()
    
    if len(sys.argv) < 2:
        print("Usage:")
        print(f"  python {SCRIPTS_DIR.name}/semantic_search.py build        # Build search index")
        print(f"  python {SCRIPTS_DIR.name}/semantic_search.py search <q>   # Search")
        print(f"  python {SCRIPTS_DIR.name}/semantic_search.py status        # Check status")
        return
    
    command = sys.argv[1]
    
    if command == "build":
        # 权威构建流程在 build_full_index.py（扫描 stm/short_term/scenarios 等
        # 全部子目录 + 批内去重）。本类旧 build_index() 只扫 core+daily，
        # 会生成残缺索引并覆盖权威索引文件，此处转发避免误用。
        import subprocess
        target = Path(__file__).parent / "build_full_index.py"
        r = subprocess.run([sys.executable, str(target)])
        sys.exit(r.returncode)
    
    elif command == "search":
        if len(sys.argv) < 3:
            print("❌ Please provide a search query")
            return
        
        query = " ".join(sys.argv[2:])
        print(f"\n🔍 Semantic Search: '{query}'")
        
        results = searcher.search(query, top_k=5)
        print_results(results)
    
    elif command == "status":
        print("\n📋 System Status")
        print(f"   Memory dir: {MEMORY_DIR}")
        print(f"   Model: {MODEL_NAME}")
        print(f"   Model cache: {'✅' if MODEL_PATH.exists() else '❌'}")
        print(f"   Index: {'✅' if INDEX_PATH.exists() else '❌'}")
        print(f"   Metadata: {'✅' if METADATA_PATH.exists() else '❌'}")
        
        if METADATA_PATH.exists():
            with open(METADATA_PATH, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            print(f"   Indexed chunks: {len(metadata)}")
            
            types = {}
            for item in metadata:
                t = item.get("type", "unknown")
                types[t] = types.get(t, 0) + 1
            
            for t, count in types.items():
                print(f"     - {t}: {count}")
    
    else:
        print(f"❌ Unknown command: {command}")

if __name__ == "__main__":
    main()
