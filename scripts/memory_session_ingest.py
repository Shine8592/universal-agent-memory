#!/usr/bin/env python3
"""会话采集器：把 opencode 的会话增量导入记忆系统。

为什么需要它：
    memory_remember 只能靠模型主动调用，实际结果是"大部分会话根本没进记忆"——
    记忆系统看起来开着，实际是空的。本脚本补上采集层。

为什么必须高频 + 增量 + 追加：
    opencode 会在上下文超限时做 compaction，**直接删除旧 message**。
    实测：2026-10-22 上午的几十轮对话在下午就被压缩没了，事后无法补采。
    所以必须每 1~2 小时跑一次，把新消息立刻落盘到 daily 文件。

设计：
    - daily/YYYY-MM-DD.md 为**追加式**日志，每条消息带 `<!-- msg:ID -->` 标记
    - 重复运行按 msg_id 去重，不会写重
    - 内容里的 markdown 标题会被转义，避免破坏 daily 结构
    - 同时把「用户提问 + 助手结论」成对写入 STM（供检索与后续提升）

用法：
    python memory_session_ingest.py                 # 采集今天（增量）
    python memory_session_ingest.py --days 2        # 采集最近 2 天
    python memory_session_ingest.py --date 2026-10-22
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

MAX_USER_CHARS = 1500
MAX_ASSIST_CHARS = 1200
MAX_STM_PER_RUN = 60
MSG_MARK = "<!-- msg:"


def _db_path() -> Path:
    env = os.environ.get("OPENCODE_DB")
    return Path(env) if env else Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _project_root() -> str:
    from memory_config import PROJECT_ROOT
    return str(PROJECT_ROOT)


def _sanitize(text: str) -> str:
    """转义行首的 markdown 标题，避免破坏 daily 文件的层级结构。"""
    return re.sub(r"(?m)^(\s*)(#{1,6})(\s)", r"\1\\\2\3", text)


def _fetch_day(db: Path, day_start: datetime, day_end: datetime, project_root: str):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    lo = int(day_start.timestamp() * 1000)
    hi = int(day_end.timestamp() * 1000)

    # user 消息没有 path 字段，必须用 session.directory 兜底
    cur.execute("SELECT id, directory FROM session")
    sess_dir = {r["id"]: (r["directory"] or "") for r in cur.fetchall()}

    cur.execute("""
        SELECT id, session_id, time_created, data FROM message
        WHERE time_created >= ? AND time_created < ?
        ORDER BY time_created ASC
    """, (lo, hi))

    rows = []
    root_norm = project_root.replace("/", "\\").lower()
    for m in cur.fetchall():
        try:
            md = json.loads(m["data"])
        except Exception:
            continue
        role = md.get("role")
        if role not in ("user", "assistant"):
            continue
        cwd = ((md.get("path") or {}).get("cwd") or "").replace("/", "\\")
        if not cwd:
            cwd = (sess_dir.get(m["session_id"], "") or "").replace("/", "\\")
        if root_norm not in cwd.lower():
            continue

        cur.execute("SELECT data FROM part WHERE message_id = ? ORDER BY time_created ASC", (m["id"],))
        texts = []
        for pr in cur.fetchall():
            try:
                pd = json.loads(pr["data"])
            except Exception:
                continue
            if pd.get("type") == "text":
                txt = (pd.get("text") or "").strip()
                if txt:
                    texts.append(txt)
        if not texts:
            continue
        rows.append({
            "msg_id": m["id"],
            "session_id": m["session_id"],
            "ts": datetime.fromtimestamp(m["time_created"] / 1000),
            "role": role,
            "text": "\n".join(texts).strip(),
        })
    con.close()
    return rows


def _existing_stm_sources(stm_dir: Path) -> set:
    seen = set()
    for f in stm_dir.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            src = (d.get("metadata") or {}).get("source_msg")
            if src:
                seen.add(src)
        except Exception:
            continue
    return seen


def ingest_one_day(day: datetime) -> dict:
    from memory_config import DAILY_DIR, STM_DIR
    from dual_memory_engine import ShortTermMemory

    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    rows = _fetch_day(_db_path(), day_start, day_end, _project_root())

    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    daily_file = DAILY_DIR / f"{day_start.date().isoformat()}.md"

    # 已有内容（追加模式，按 msg 标记去重）
    old = daily_file.read_text(encoding="utf-8") if daily_file.exists() else ""
    done = set(re.findall(re.escape(MSG_MARK) + r"([^\s>]+)", old))

    if not old.strip():
        old = (f"# {day_start.date().isoformat()} 会话记录\n"
               f"> 项目：{_project_root()}\n")

    new_lines = []
    stm = ShortTermMemory()
    stm_seen = _existing_stm_sources(STM_DIR)
    added_stm = 0
    appended = 0

    # 按会话遍历，user 消息与其后第一条 assistant 文本配对写 STM
    by_session = {}
    for r in rows:
        by_session.setdefault(r["session_id"], []).append(r)

    for sid, items in by_session.items():
        i = 0
        while i < len(items):
            it = items[i]
            if it["msg_id"] not in done:
                who = "用户" if it["role"] == "user" else "助手"
                limit = MAX_USER_CHARS if it["role"] == "user" else MAX_ASSIST_CHARS
                body = it["text"]
                if len(body) > limit:
                    body = body[:limit] + "…"
                new_lines.append(
                    f"\n### {who} {it['ts'].strftime('%H:%M')} · {sid[:12]}\n"
                    f"{MSG_MARK}{it['msg_id']} -->\n{_sanitize(body)}\n"
                )
                done.add(it["msg_id"])
                appended += 1

            # STM：user 消息 + 紧随其后的 assistant 结论
            if it["role"] == "user" and added_stm < MAX_STM_PER_RUN:
                if it["msg_id"] not in stm_seen:
                    answer = ""
                    for j in range(i + 1, len(items)):
                        if items[j]["role"] == "assistant":
                            answer = items[j]["text"][:MAX_ASSIST_CHARS]
                            break
                        if items[j]["role"] == "user":
                            break
                    content = f"[会话提问] {it['text'][:MAX_USER_CHARS]}"
                    if answer:
                        content += f"\n[当时结论] {answer}"
                    try:
                        stm.add(content, metadata={
                            "tags": ["session", "daily"],
                            "source_msg": it["msg_id"],
                            "session_id": sid,
                            "source": "opencode_session",
                        })
                        stm_seen.add(it["msg_id"])
                        added_stm += 1
                    except Exception as e:
                        print(f"  [warn] STM 写入失败: {e}")
            i += 1

    if new_lines:
        daily_file.write_text(old.rstrip() + "\n" + "".join(new_lines), encoding="utf-8")

    return {
        "date": day_start.date().isoformat(),
        "messages": len(rows),
        "appended": appended,
        "stm_added": added_stm,
        "daily_chars": len(daily_file.read_text(encoding="utf-8")) if daily_file.exists() else 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="指定日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--days", type=int, default=1, help="采集最近 N 天（含今天）")
    args = ap.parse_args()

    if args.date:
        days = [datetime.strptime(args.date, "%Y-%m-%d")]
    else:
        base = datetime.now()
        days = [base - timedelta(days=i) for i in range(args.days)][::-1]

    total = 0
    for d in days:
        r = ingest_one_day(d)
        total += r["stm_added"]
        print(f"[{r['date']}] DB消息 {r['messages']} 条 / 新增落盘 {r['appended']} 条 "
              f"/ STM +{r['stm_added']} / daily {r['daily_chars']} 字符")
    print(f"[done] 本次新增 STM {total} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
