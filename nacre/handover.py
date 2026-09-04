from .config import load_config
from .db import now_iso, to_local
from .session_file import ANCHOR_KEY, SESSION_KEY

PENDING_KEY = "handover:pending"

SNIPPET_CHARS = 60

CTX_LIMIT = 279_550

TOKEN_PER_CHAR = 0.94

def est_tokens(text):
    return int(len(text or "") * TOKEN_PER_CHAR)

class HandoverError(RuntimeError):
    pass

def cut_point(rows):
    out = list(rows)
    while out and out[-1]["role"] != "assistant":
        out.pop()
    return out

def _snippet(text):
    t = (text or "").strip().replace("\n", " ")
    return t if len(t) <= SNIPPET_CHARS else t[:SNIPPET_CHARS] + "…"

def _her_date(created_at, offset):
    return to_local(created_at, offset)[:10]

def preview(conn, conversation_id=None, window_name="", cfg=None):
    offset = int((load_config() if cfg is None else cfg).get("local_utc_offset_hours", 8))
    sql = "SELECT id, role, content, created_at FROM messages"
    args = []
    if conversation_id is not None:
        sql += " WHERE conversation_id=?"
        args.append(conversation_id)
    rows = list(conn.execute(sql + " ORDER BY id", args))
    if not rows:
        return {"segments": [], "counts": {"messages": 0, "cards": 0, "marked": 0},
                "days": 0, "range": None,
                "budget": _budget([], cfg),
                "note": "这个窗口还没有对话 —— **没有可带走的东西，不是出错了**"}

    card_sql = ("SELECT id, content, src_msg_start, src_msg_end, kind FROM memories "
                "WHERE status='active' AND target_memory_id IS NULL")
    card_args = []
    if conversation_id is not None:
        card_sql += " AND src_conversation_id=?"
        card_args.append(conversation_id)
    cards = list(conn.execute(card_sql, card_args))

    covered = set()
    for c in cards:
        a, b = c["src_msg_start"], c["src_msg_end"] or c["src_msg_start"]
        if a:
            covered.update(range(int(a), int(b) + 1))

    his = set()
    for r in conn.execute(
        "SELECT src_msg_start, src_msg_end, kind FROM memories "
        "WHERE status='active' AND author='assistant'"
    ):
        a, b = r["src_msg_start"], r["src_msg_end"] or r["src_msg_start"]
        if a:
            his.update(range(int(a), int(b) + 1))

    marked = {r["message_id"] for r in conn.execute(
        "SELECT message_id FROM handover_marks WHERE window_name=?", (window_name or "",))}

    segments = []
    for r in rows:
        segments.append({
            "id": r["id"],
            "role": r["role"],
            "snippet": _snippet(r["content"]),
            "at": _her_date(r["created_at"], offset),
            "suggested": r["id"] in covered,
            "marked": r["id"] in marked,
            "his_own": r["id"] in his,
            "tokens": est_tokens(r["content"]),
        })

    起, 止 = _her_date(rows[0]["created_at"], offset), _her_date(rows[-1]["created_at"], offset)
    days = _days_between(起, 止)
    return {
        "segments": segments,
        "counts": {"messages": len(rows), "cards": len(cards), "marked": len(marked)},
        "days": days,
        "range": {"from": 起, "to": 止},
        "rule": "系统默认勾上的是：这个窗口里蒸出的卡，以及它们指向的原文区间。",
        "budget": _budget(segments, cfg),
    }

def _budget(segments, cfg):
    近况 = sum(s["tokens"] for s in segments if s["marked"] or s["suggested"])
    常驻, 常驻说明 = _resident_tokens(cfg)
    合计 = 近况 + (常驻 or 0)
    return {
        "limit": CTX_LIMIT,
        "resident": 常驻,
        "resident_note": 常驻说明,
        "recent": 近况,
        "total": 合计,
        "pct": round(合计 / CTX_LIMIT * 100, 1),
        "cards_note": "每轮现拉的卡不在这个数里 —— 它按你下一句话去检索，这会儿还不知道有多少。",
        "how": f"按实测 {TOKEN_PER_CHAR} token/字 估的，不是官方口径，可能对不上。",
    }

def _resident_tokens(cfg):
    try:
        from . import resident_index
        p = resident_index.daily_path(cfg)
    except Exception as e:
        return None, f"读不到（{type(e).__name__}）：{e}"
    if not p.exists():
        return None, f"今天这份还没生成：{p} —— 夜间维护窗口跑了吗？"
    return est_tokens(p.read_text(encoding="utf-8")), ""

def _days_between(d1, d2):
    from datetime import date
    try:
        a = date(*[int(x) for x in d1.split("-")])
        b = date(*[int(x) for x in d2.split("-")])
        return (b - a).days + 1
    except Exception:
        return 0

def mark(conn, add=None, remove=None, window_name="", by="user"):
    if by not in ("user", "system"):
        raise HandoverError(f"marked_by 只有 user / system 两种，收到 {by!r}")
    try:
        add = [int(x) for x in (add or [])]
        remove = [int(x) for x in (remove or [])]
    except (TypeError, ValueError) as e:
        raise HandoverError(f"消息 id 必须是整数：{e}")
    w = window_name or ""

    for mid in add:
        if conn.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone() is None:
            raise HandoverError(
                f"账本里没有 id={mid} 这条消息 —— 前端给的 id 对不上账本，这一批一条都没写。")
    for mid in add:
        conn.execute(
            "INSERT INTO handover_marks(message_id, window_name, marked_by, created_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(message_id, window_name) DO UPDATE SET "
            "marked_by=excluded.marked_by, created_at=excluded.created_at "
            "WHERE excluded.marked_by='user'",
            (mid, w, by, now_iso()))
    unmarked = 0
    if remove:
        q = ",".join("?" * len(remove))
        cur = conn.execute(
            f"DELETE FROM handover_marks WHERE window_name=? AND message_id IN ({q})",
            [w, *remove])
        unmarked = cur.rowcount or 0
    total = conn.execute("SELECT COUNT(*) FROM handover_marks WHERE window_name=?",
                         (w,)).fetchone()[0]
    return {"marked": len(add), "unmarked": unmarked, "total": total}

def start(conn, model, keep_message_ids=None, window_name="", effort=None):
    if not (model or "").strip():
        raise HandoverError(
            "换窗必须指定 model。\n"
            "🔴 **不许留空走 `claude` 的默认** —— 那个默认哪天变了，"
            "他就在她不知道的情况下换了个人，**而没有任何东西会提醒**。"
        )

    from . import store
    conv_id = store.ensure_conversation(conn, "frontend")
    conn.execute("UPDATE conversations SET window_name=?, model=? WHERE id=?",
                 (window_name or now_iso()[:10], model.strip(), conv_id))

    for k in (ANCHOR_KEY, SESSION_KEY):
        conn.execute("DELETE FROM watermarks WHERE key=?", (k,))

    import json as _json
    conn.execute(
        "INSERT INTO watermarks(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (PENDING_KEY, _json.dumps({
            "conversation_id": conv_id, "model": model, "effort": effort,
            "window_name": window_name, "keep": list(keep_message_ids or []),
        }, ensure_ascii=False), now_iso()),
    )
    return conv_id

def window_model(conn, conversation_id):
    row = conn.execute("SELECT model FROM conversations WHERE id=?",
                       (conversation_id,)).fetchone()
    return ((row["model"] if row else None) or "").strip()

def pending(conn):
    import json as _json
    row = conn.execute("SELECT value FROM watermarks WHERE key=?", (PENDING_KEY,)).fetchone()
    if not row or not (row["value"] or "").strip():
        return None
    try:
        return _json.loads(row["value"])
    except Exception:
        return None

def discard(conn):
    p = pending(conn)
    if not p:
        raise HandoverError(
            "现在没有「刚换完、还没开口」的窗口 —— **没有可作废的东西**。\n"
            "🔴 一旦她说了第一句话，这条路就关了。聊了几天才发现选错，那要等后续版本。"
        )
    conn.execute("DELETE FROM watermarks WHERE key=?", (PENDING_KEY,))
    for k in (ANCHOR_KEY, SESSION_KEY):
        conn.execute("DELETE FROM watermarks WHERE key=?", (k,))

    conv_id = p.get("conversation_id") if isinstance(p, dict) else None
    if conv_id is not None:
        n = conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?",
                         (conv_id,)).fetchone()[0]
        if n:
            raise HandoverError(
                f"这扇窗（{conv_id}）里已经有 {n} 条消息了 —— **拒绝撤销**。\n"
                "🔴 `discard()` 的前提是「换完了还没开口」，而这扇窗有内容 ⇒ 前提不成立。\n"
                "   继续删就是真的删掉东西，而账本里的东西删了找不回来。"
            )
        c = conn.execute("SELECT COUNT(*) FROM memories WHERE src_conversation_id=?",
                         (conv_id,)).fetchone()[0]
        if c:
            raise HandoverError(
                f"这扇窗（{conv_id}）没有消息、却挂着 {c} 张卡 —— **拒绝撤销**。\n"
                "🔴 这个组合不该出现，说明有别的东西在往这扇窗上挂卡。查清楚再动。"
            )
        conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
    return p

def settle(conn):
    p = pending(conn)
    if not p:
        return 0, False
    name = p.get("window_name") or ""
    cur = conn.execute("DELETE FROM handover_marks WHERE window_name=?", (name,))
    conn.execute("DELETE FROM watermarks WHERE key=?", (PENDING_KEY,))
    return cur.rowcount or 0, True
