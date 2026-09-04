import re

from . import search
from .config import load_config
from .db import now_iso

LOW_INFO_PATTERNS = (
    r"^嗯+$", r"^嗯嗯+$", r"^哦+$", r"^噢+$", r"^啊+$",
    r"^好的?$", r"^行$", r"^可以$", r"^收到$", r"^知道了?$", r"^懂了?$",
    r"^晚安$", r"^早安$", r"^午安$", r"^早$", r"^在吗?$",
    r"^哈+$", r"^呵+$", r"^嘿+$", r"^笑死$",
    r"^谢谢?$", r"^谢啦$", r"^辛苦了?$",
    r"^是的?$", r"^对$", r"^对的?$", r"^没错$", r"^不是$", r"^没有$",
    r"^ok$", r"^okay$", r"^yes$", r"^no$", r"^\W+$",
)

LOW_INFO_MAX_CHARS = 6

TENDER_PATTERNS = (
    "吵", "冲突", "争执", "闹", "生气", "愤怒", "难过", "伤心", "哭",
    "对不起", "抱歉", "道歉", "原谅", "错了", "怪我",
    "失败", "搞砸", "做错", "后悔", "遗憾",
    "拒绝", "不想", "不愿", "算了", "分手", "结束",
    "难堪", "尴尬", "丢脸", "羞",
)

DEFAULT_TURN_BUDGET_CHARS = 3800

class PullError(RuntimeError):
    pass

def _截断说明(cards):
    from . import search
    if not any(search.CARD_CUT_MARK in (c.get("line") or "") for c in cards):
        return ""
    return "，每条只给了开头（想看完整的卡自己 recall；实在要当时的原话再 read_original）"

def is_low_info(text):
    t = (text or "").strip()
    if not t:
        return True
    if len(t) > LOW_INFO_MAX_CHARS:
        return False
    low = t.lower().rstrip("。.!！~～、,，")
    return any(re.match(p, low) for p in LOW_INFO_PATTERNS)

def is_tender_topic(text):
    t = text or ""
    return any(p in t for p in TENDER_PATTERNS)

def pin_card(conn, memory_id):
    row = conn.execute("SELECT id FROM memories WHERE id=? AND status='active'",
                       (int(memory_id),)).fetchone()
    if not row:
        raise PullError(f"要推的这张卡不在（或已撤回）：#{memory_id}")
    dup = conn.execute(
        "SELECT id FROM pinned_cards WHERE memory_id=? AND used_at IS NULL",
        (int(memory_id),)).fetchone()
    if dup:
        return int(dup["id"]), False
    cur = conn.execute(
        "INSERT INTO pinned_cards(memory_id, created_at) VALUES(?,?)",
        (int(memory_id), now_iso()))
    return int(cur.lastrowid), True

def take_pinned(conn):
    rows = list(conn.execute(
        "SELECT p.id AS pin_id, m.* FROM pinned_cards p "
        "JOIN memories m ON m.id = p.memory_id "
        "WHERE p.used_at IS NULL AND m.status='active' ORDER BY p.id"))
    if rows:
        conn.execute(
            "UPDATE pinned_cards SET used_at=? WHERE used_at IS NULL", (now_iso(),))
    return rows

def fit_budget(conn, cards, cfg=None):
    budget = int(((cfg or {}).get("recall") or {}).get(
        "turn_budget_chars", DEFAULT_TURN_BUDGET_CHARS))
    kept, used, folded = [], 0, 0
    已在某串里 = set()
    for i, c in enumerate(cards):
        mid = c["row"]["id"]
        if mid in 已在某串里:
            folded += 1
            continue
        cost = len(c["line"]) + 1
        if kept and used + cost > budget and not c.get("pinned"):
            装不下 = sum(1 for later in cards[i:] if later["row"]["id"] not in 已在某串里)
            return kept, 装不下, folded
        kept.append(c)
        used += cost
        已在某串里.update(search.bridge_ids(conn, mid))
    return kept, 0, folded

def pull(conn, cfg, text, limit=None):
    cfg = load_config() if cfg is None else cfg

    pinned = [{"score": 1.0, "row": r, "line": search.format_line(conn, r, with_quote=True, cut=search.CARD_CUT_CHARS),
               "pinned": True} for r in take_pinned(conn)]

    if is_low_info(text):
        return {"skipped": not pinned, "cards": pinned, "total": len(pinned),
                "hit_total": len(pinned), "hit_names": [],
                "failed": False, "error": "", "tender": False,
                "over": 0, "folded": 0}

    tender = is_tender_topic(text)
    try:
        results, warnings = search.recall(conn, cfg, text, limit=limit,
                                          cut=search.CARD_CUT_CHARS)
    except Exception as e:
        return {"skipped": False, "cards": pinned, "total": len(pinned),
                "hit_total": len(pinned), "hit_names": [],
                "failed": True, "error": f"{type(e).__name__}: {e}", "tender": tender,
                "over": 0, "folded": 0}

    seen = {c["row"]["id"] for c in pinned}
    merged = pinned + [r for r in results if r["row"]["id"] not in seen]
    merged, over, folded = fit_budget(conn, merged, cfg)
    given = {c["row"]["id"] for c in merged}
    hit_names = search.query_entities(conn, text)
    hit_total = len(search.entity_hit_ids(conn, text, names=hit_names) | given)
    return {"skipped": False, "cards": merged, "total": len(merged),
            "hit_total": hit_total, "hit_names": hit_names,
            "failed": False, "error": "", "tender": tender,
            "over": over, "folded": folded,
            "warnings": warnings}

def mark_lines(ids, lines):
    lines = list(lines)
    ids = list(ids)
    if len(ids) != len(lines):
        raise PullError(
            f"卡号和材料对不齐（卡号 {len(ids)} 个、材料 {len(lines)} 条）。\n"
            "🔴 **不许「能挂几个算几个」** —— 错位的后果是他表态写到了别人身上，"
            "而那不会报错。"
        )
    return [f"[#{int(i)}] {line}" for i, line in zip(ids, lines)]
def render(pulled, 卡号们=None):
    if pulled.get("skipped"):
        return ""

    if pulled.get("failed"):
        return ("〔查记忆的时候出错了：%s〕\n"
                "这一轮没能查成，**不是查过了没有** —— 这两件事不一样。"
                % pulled.get("error", "原因不明"))

    cards = pulled.get("cards") or []
    if not cards:
        line = "〔查过了，没有相关的记忆卡。〕"
        if pulled.get("tender"):
            line += "\n可能是没被记下来，**不代表没发生**。"
        return line

    n = len(cards)
    hit = pulled.get("hit_total")
    names = pulled.get("hit_names") or []
    what = "提到" + "、".join(f"「{x}」" for x in names) + "的" if names else "相关的"
    if isinstance(hit, int) and hit > n:
        head = f"〔查到 {hit} 条{what}。下面是这一轮最相关的 {n} 条{_截断说明(cards)}：〕"
    else:
        head = f"〔查到 {n} 条{what}{_截断说明(cards)}：〕"
    if 卡号们 is None:
        body = "\n".join(c["line"] for c in cards)
    else:
        body = "\n".join(mark_lines(卡号们, [c["line"] for c in cards]))
    out = f"{head}\n{body}"

    over = pulled.get("over") or 0
    if over:
        out += f"\n〔另有 {over} 条这一轮的量放不下，没给你 —— 是装不下，不是没有。〕"
    return out
