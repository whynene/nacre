import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nacre.db import get_conn
from nacre.store import append_message, ensure_conversation

def _message_text(msg):
    if isinstance(msg.get("content"), list):
        parts = [b.get("text", "") for b in msg["content"] if b.get("type") == "text"]
        text = "\n".join(p for p in parts if p)
        if text:
            return text
    return msg.get("text", "") or ""

def import_file(conn, path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("conversations", [data])
    n_conv, n_msg = 0, 0
    for conv in data:
        uuid = conv.get("uuid")
        msgs = conv.get("chat_messages") or []
        if not msgs:
            continue
        conv_id = ensure_conversation(
            conn,
            "claude_ai",
            external_id=f"claude_ai:{uuid}" if uuid else None,
            title=conv.get("name") or None,
            started_at=(conv.get("created_at") or "")[:19] or None,
        )
        n_conv += 1
        for m in msgs:
            sender = m.get("sender")
            role = "user" if sender == "human" else "assistant" if sender == "assistant" else None
            text = _message_text(m).strip()
            if not role or not text:
                continue
            muuid = m.get("uuid")
            mid = append_message(
                conn,
                conv_id,
                role,
                text,
                created_at=(m.get("created_at") or "")[:19] or None,
                external_id=f"claude_ai:{muuid}" if muuid else None,
            )
            if mid:
                n_msg += 1
    return n_conv, n_msg

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    conn = get_conn()
    try:
        n_conv, n_msg = import_file(conn, sys.argv[1])
        conn.commit()
        print(f"导入完成：扫过 {n_conv} 场会话，新入账 {n_msg} 条消息（重复的已幂等跳过）。")
        print("提示：跑一次夜班（python -m nacre.nightly）让蒸馏捞漏。")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
