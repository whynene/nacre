import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nacre.config import load_config
from nacre.db import get_conn
from nacre.store import append_message, ensure_conversation

PROJECTS_DIR = Path.home() / ".claude" / "projects"

def _text_from_message(m):
    content = m.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""

def import_sessions(conn, cfg):
    dirs = cfg["cc_import"]["project_dirs"]
    if not cfg["cc_import"]["enabled"] or not dirs:
        return 0, 0
    n_conv, n_msg = 0, 0
    for d in dirs:
        proj = PROJECTS_DIR / d
        if not proj.is_dir():
            continue
        for f in sorted(proj.glob("*.jsonl")):
            session_id = f.stem
            conv_id = None
            with f.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") not in ("user", "assistant") or rec.get("isMeta"):
                        continue
                    msg = rec.get("message") or {}
                    role = msg.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    text = _text_from_message(msg).strip()
                    if not text:
                        continue
                    if conv_id is None:
                        conv_id = ensure_conversation(
                            conn, "claude_code",
                            external_id=f"claude_code:{session_id}",
                            title=f"CC 会话 {session_id[:8]}",
                            started_at=(rec.get("timestamp") or "")[:19] or None,
                        )
                        n_conv += 1
                    uuid = rec.get("uuid")
                    mid = append_message(
                        conn, conv_id, role, text,
                        created_at=(rec.get("timestamp") or "")[:19] or None,
                        external_id=f"claude_code:{session_id}:{uuid}" if uuid else None,
                    )
                    if mid:
                        n_msg += 1
    return n_conv, n_msg

if __name__ == "__main__":
    conn = get_conn()
    try:
        n_conv, n_msg = import_sessions(conn, load_config())
        conn.commit()
        print(f"CC 会话导入：扫过 {n_conv} 场，新入账 {n_msg} 条。")
    finally:
        conn.close()
