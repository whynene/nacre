from datetime import datetime, timedelta

from .db import get_watermark, now_iso
from .search import format_line

def assemble(conn, cfg):
    today = now_iso()[:10]
    lines = [f"# 交接简报 · {today}", ""]

    lines.append("## 核心事实")
    core_rows = conn.execute(
        "SELECT * FROM memories WHERE is_core=1 AND status='active' "
        "AND target_memory_id IS NULL ORDER BY id"
    ).fetchall()
    if core_rows:
        for r in core_rows:
            lines.append(f"- {format_line(conn, r)}")
    else:
        lines.append("（暂无。核心事实由质检台从记忆卡中授予。）")
    lines.append("")

    lines.append("## 近况")
    summary = get_watermark(conn, "core:recent_summary")
    if summary:
        lines.append(summary.strip())
    else:
        days = cfg["core_card"]["recent_days"]
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        recent = conn.execute(
            "SELECT * FROM memories WHERE status='active' AND created_at >= ? "
            "AND importance >= 3 AND target_memory_id IS NULL ORDER BY created_at DESC LIMIT 8",
            (since,),
        ).fetchall()
        if recent:
            for r in recent:
                lines.append(f"- {format_line(conn, r)}")
        else:
            lines.append(f"（最近 {days} 天暂无记录。）")
    lines.append("")

    lines.append("## 记忆目录（细节可用 recall 查阅）")
    ents = conn.execute(
        "SELECT * FROM entities ORDER BY last_mentioned_at DESC LIMIT 40"
    ).fetchall()
    if ents:
        for e in ents:
            one = (e["one_liner"] or "").strip()
            lines.append(f"- {e['name']}" + (f" · {one}" if one else ""))
    else:
        lines.append("（目录为空。）")
    lines.append("")

    return "\n".join(lines)

def save_version(conn, content_md):
    conn.execute("UPDATE core_card_versions SET active=0 WHERE active=1")
    conn.execute(
        "INSERT INTO core_card_versions(content_md, generated_at, active) VALUES(?,?,1)",
        (content_md, now_iso()),
    )

def get_active(conn):
    return conn.execute(
        "SELECT * FROM core_card_versions WHERE active=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()

def ensure_active(conn, cfg):
    row = get_active(conn)
    if row:
        return row["content_md"]
    md = assemble(conn, cfg)
    save_version(conn, md)
    conn.commit()
    return md
