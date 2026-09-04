import json
import threading

from .db import now_iso

STATE_KEY = "distill:manual_state"
RUNNING, DONE, FAILED = "running", "done", "failed"

class DistillError(RuntimeError):
    pass

def _write(conn, state, remaining=None, error="", cards=None, n_cards=None, why=""):
    conn.execute(
        "INSERT INTO watermarks(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (STATE_KEY, json.dumps({
            "state": state, "remaining": remaining, "error": error, "why": why,
            "cards": cards, "n_cards": n_cards, "at": now_iso(),
        }, ensure_ascii=False), now_iso()),
    )

_人话 = [
    ("FileNotFoundError", "找不到 claude 这个命令 —— 多半是它没装好，或者不在这个服务能看到的路径里。"),
    ("No such file or directory", "找不到 claude 这个命令 —— 多半是它没装好，或者不在这个服务能看到的路径里。"),
    ("TimeoutExpired", "蒸了太久没回来，这一趟按超时停了。可以再试一次；老是这样就是那一批太大了。"),
    ("Timeout", "蒸了太久没回来，这一趟按超时停了。可以再试一次；老是这样就是那一批太大了。"),
    ("JSONDecode", "他这次回的东西读不成结果 —— 不是你的问题，重试一次多半就好。"),
    ("解析", "他这次回的东西读不成结果 —— 不是你的问题，重试一次多半就好。"),
    ("credit balance", "额度不够了 —— 这一趟没跑成，也没扣到东西。"),
    ("rate limit", "被限流了 —— 歇一会儿再按一次。"),
    ("usage limit", "额度用到上限了 —— 等窗口重置再来。"),
    ("NotPrimaryLedger", "这台机器上的库不是主库，为了不写错地方，这一趟没跑。"),
]

def 说人话(err):
    s = str(err or "")
    for 记号, 人话 in _人话:
        if 记号.lower() in s.lower():
            return 人话
    return "蒸馏这一趟没跑成。原因下面那行是给排查用的，你不用看懂它。"

def status(conn):
    row = conn.execute("SELECT value FROM watermarks WHERE key=?", (STATE_KEY,)).fetchone()
    if not row or not (row["value"] or "").strip():
        return {"state": DONE, "remaining": 0, "error": "", "why": "", "cards": None,
                "n_cards": None,
                "note": "没跑过 —— **这不是失败，是还没按过那颗按钮**"}
    try:
        return json.loads(row["value"])
    except Exception as e:
        return {"state": FAILED, "remaining": None, "n_cards": None,
                "error": f"状态读不出来：{e}",
                "why": "上一趟蒸馏的状态记坏了，读不出来 —— 按"
                       "「再试一次」重跑一遍就好。",
                "cards": None}

def can_handover(conn):
    st = status(conn)
    if st["state"] == RUNNING:
        return False, f"还在蒸，剩 {st.get('remaining')} 轮"
    if st["state"] == FAILED:
        return True, f"🔴 上一次蒸馏失败了（{st.get('error') or '原因不明'}）—— **这一窗没有新卡**"
    return True, ""

def pending_rounds(conn, conversation_id=None):
    from .nightly import _pending_messages
    _, rows = _pending_messages(conn, conversation_id=conversation_id)
    return len(rows)

def 不许蒸的窗(cfg, conversation_id):
    if conversation_id is None:
        return False
    no = ((cfg or {}).get("v3") or {}).get("no_distill_conversations") or []
    return int(conversation_id) in [int(x) for x in no]

def run(conn_factory, cfg, limit_chunks=None, conversation_id=None):
    from . import nightly

    if 不许蒸的窗(cfg, conversation_id):
        conn = conn_factory()
        try:
            why = "这扇窗设了「只入账、不蒸馏」，所以没有蒸 —— 这不是出错。"
            _write(conn, FAILED, remaining=0, n_cards=None,
                   error=f"conversation {conversation_id} in v3.no_distill_conversations", why=why)
            conn.commit()
        finally:
            conn.close()
        return {"state": FAILED, "error": why, "why": why, "n_cards": None}

    conn = conn_factory()
    try:
        _write(conn, RUNNING, remaining=pending_rounds(conn, conversation_id))
        conn.commit()
    finally:
        conn.close()

    report = []
    conn = conn_factory()
    try:
        _量, *_ = nightly.预估(conn, conversation_id=conversation_id)
        n_cards = nightly.extract(conn, cfg, report, limit_chunks=limit_chunks, 已确认量=_量,
                                  conversation_id=conversation_id)
        conn.commit()
        _write(conn, DONE, remaining=pending_rounds(conn, conversation_id),
               cards="\n".join(report), n_cards=(None if n_cards is None else int(n_cards)))
        conn.commit()
        return {"state": DONE, "report": report, "n_cards": n_cards}
    except Exception as e:
        conn.rollback()
        _write(conn, FAILED, remaining=pending_rounds(conn, conversation_id),
               error=f"{type(e).__name__}: {e}", why=说人话(e), n_cards=None)
        conn.commit()
        return {"state": FAILED, "error": str(e), "why": 说人话(e), "n_cards": None}
    finally:
        conn.close()

def run_async(conn_factory, cfg, limit_chunks=None, conversation_id=None):
    t = threading.Thread(target=run, args=(conn_factory, cfg, limit_chunks, conversation_id),
                         daemon=True)
    t.start()
    return t
