#!/usr/bin/env python3
"""每日记忆维护：STM → LTM 提升 + 过期清理。

供系统计划任务（Windows Task Scheduler / cron）调用。
只做两件事：
  1. 按重要性阈值（默认 0.7）把达标的短期记忆提升到长期记忆 MEMORY.md
  2. 清理超过 24h 的短期记忆（其中 importance_score >= 0.6 的先提升再删除）

索引重建不在此处：由 auto_index_updater.py（增量）与 build_full_index.py（全量）负责，
避免重复构建。

用法：
    python memory_daily_upgrade.py
"""
import sys
import json
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def _ltm_sections() -> int:
    """统计 MEMORY.md 的 ### 段落数（用于计算本次实际提升量）。"""
    try:
        from memory_config import LTM_FILE
        return sum(1 for ln in LTM_FILE.read_text(encoding="utf-8").splitlines()
                   if ln.startswith("### "))
    except Exception:
        return 0


def main() -> int:
    from memory_config import STM_DIR, COORDINATOR_FILE
    from dual_memory_engine import MemoryCoordinator

    before = _ltm_sections()

    coordinator = MemoryCoordinator()
    # 1) 按阈值提升（evaluate_transfers 用 importance_threshold，默认 0.7）
    promoted = coordinator.auto_transfer(max_transfers=10)
    # 2) 过期清理（>=24h；其中 >=0.6 分的先提升到 LTM 再删）
    try:
        coordinator.stm._cleanup()
    except Exception as e:
        print(f"[warn] cleanup 失败: {e}")

    after = _ltm_sections()
    delta = max(0, after - before)

    # 3) 同步统计（promoted_count / stm_count / last_transfer）
    try:
        c = json.loads(COORDINATOR_FILE.read_text(encoding="utf-8")) if COORDINATOR_FILE.exists() else {}
        st = c.setdefault("stats", {})
        st["promoted_count"] = int(st.get("promoted_count", 0)) + delta
        st["stm_count"] = len(list(STM_DIR.glob("*.json")))
        c["last_transfer"] = datetime.now().isoformat(timespec="seconds")
        COORDINATOR_FILE.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[warn] 统计写入失败: {e}")

    remaining = len(list(STM_DIR.glob("*.json")))
    print(f"[done] auto_transfer={promoted} ltm_sections {before}->{after} "
          f"(+{delta}) stm_remaining={remaining}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
