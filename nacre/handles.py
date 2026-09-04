import re

from .db import now_iso

KINDS = {"卡": "card", "段": "segment", "记": "note"}

_SLOT_RE = re.compile(r"^\s*\[?\s*(卡|段|记)\s*(\d+)\s*\]?\s*$")

SOURCES = {"chat": "自建端", "mcp": "官方端 MCP"}

def _check_source(source):
    s = str(source or "")
    if s not in SOURCES:
        raise HandleError(
            f"位次来源只能是 {' / '.join(SOURCES)}，收到 {source!r}。\n"
            f"   🔴 **它没有默认值是有意的**：忘了传就静默挂到别人那条线上，"
            f"而下一次表态会落到他没看过的卡上。"
        )
    return s

class HandleError(RuntimeError):
    pass

def slot_of(kind_char, n):
    if kind_char not in KINDS:
        raise HandleError(f"类型字只能是 {' / '.join(KINDS)}，收到 {kind_char!r}")
    return f"{kind_char}{int(n)}"

def parse_slot(text):
    m = _SLOT_RE.match(str(text or ""))
    if not m:
        raise HandleError(
            f"看不懂这个指向：{text!r}\n"
            f"   要写成 `卡2` 这样（类型字 + 本轮位次），方括号可带可不带。\n"
            f"   🔴 光给一个数字不行 —— 跨块统一编号之后「卡2」和「段2」是两个东西。"
        )
    return f"{m.group(1)}{int(m.group(2))}"

def next_turn_id(conn):
    row = conn.execute("SELECT MAX(turn_id) AS m FROM turn_handles").fetchone()
    return int((row["m"] or 0)) + 1

def current_turn(conn, source):
    src = _check_source(source)
    row = conn.execute(
        "SELECT MAX(turn_id) AS m FROM turn_handles WHERE source=?", (src,)
    ).fetchone()
    m = row["m"] if row else None
    return int(m) if m is not None else None

def issue(conn, turn_id, items, source):
    src = _check_source(source)
    counters, out = {}, []
    for kind_char, mid in items:
        if kind_char not in KINDS:
            raise HandleError(f"类型字只能是 {' / '.join(KINDS)}，收到 {kind_char!r}")
        counters[kind_char] = counters.get(kind_char, 0) + 1
        slot = slot_of(kind_char, counters[kind_char])
        conn.execute(
            "INSERT INTO turn_handles(turn_id, slot, memory_id, created_at, source) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(turn_id, slot) DO UPDATE SET memory_id=excluded.memory_id, "
            "source=excluded.source",
            (turn_id, slot, int(mid), now_iso(), src),
        )
        out.append((slot, int(mid)))
    return out

def resolve(conn, turn_id, slot_text):
    slot = parse_slot(slot_text)
    row = conn.execute(
        "SELECT memory_id FROM turn_handles WHERE turn_id=? AND slot=?",
        (int(turn_id), slot),
    ).fetchone()
    if not row:
        有的 = [r["slot"] for r in conn.execute(
            "SELECT slot FROM turn_handles WHERE turn_id=? ORDER BY slot", (int(turn_id),)
        )]
        raise HandleError(
            f"这一轮没有 `{slot}` 这个指向。\n"
            f"   这一轮实际发下去的是：{'、'.join(有的) if 有的 else '（一个都没有）'}\n"
            f"   🔴 **没有猜一个最近的给你** —— 指错卡比指不着坏得多。"
        )
    return int(row["memory_id"])
