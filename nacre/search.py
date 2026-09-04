import math
import re
from datetime import datetime

from . import embeddings, segment
from .config import load_config
from .db import now_iso, to_local
from .store import KIND_LABELS, memory_entities_names

DECAYING_KINDS = {"event", "commitment"}

DIRECT_MIN_LEN = 2
DIRECT_MAX_LEN = 60

DIRECT_LIMIT = 20

def _parse_dt(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text)[:19])
    except ValueError:
        return None

def decay_factor(row, half_life_days, now=None):
    if row["is_core"] or row["kind"] not in DECAYING_KINDS:
        return 1.0
    now = now or datetime.now()
    happened = _parse_dt(row["occurred_at"]) or _parse_dt(row["created_at"])
    anchor = max([d for d in (happened, _parse_dt(row["last_used_at"])) if d], default=None)
    if not anchor:
        return 1.0
    days = max(0.0, (now - anchor).total_seconds() / 86400)
    return 0.5 ** (days / half_life_days)

def _importance_weight(importance):
    return 0.6 + 0.08 * importance

def entity_surfaces(conn):
    out = []
    for r in conn.execute("SELECT name, aliases FROM entities").fetchall():
        name = (r["name"] or "").strip()
        if not name:
            continue
        out.append((name, name))
        for a in re.split(r"[,，]", r["aliases"] or ""):
            a = a.strip()
            if a:
                out.append((a, name))
    out.sort(key=lambda t: -len(t[0]))
    return out

def query_entities(conn, query):
    pairs = entity_surfaces(conn)
    hit = segment.mentions(query, [s for s, _ in pairs])
    canon = dict(pairs)
    out, seen = [], set()
    for s in hit:
        name = canon.get(s, s)
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out

def entity_hit_ids(conn, query, names=None):
    names = query_entities(conn, query) if names is None else names
    match = segment.entity_fts_query(names)
    if not match:
        return set()
    try:
        rows = conn.execute(
            "SELECT f.memory_id AS mid FROM memories_fts f "
            "JOIN memories m ON m.id = f.memory_id "
            "WHERE memories_fts MATCH ? "
            "AND m.status='active' AND m.target_memory_id IS NULL AND m.is_fragment=0",
            (match,),
        ).fetchall()
    except Exception:
        return set()
    return {r["mid"] for r in rows}

_CARD_ID_RE = re.compile(r"^\s*[#＃]?\s*(?:卡\s*)?(\d{1,7})\s*$")

def recall_by_card_id(conn, cfg, card_id, cut=None):
    from .store import resolve_card_id

    最终, 跳, 状态 = resolve_card_id(conn, card_id)
    if 最终 is None:
        return [], [f"库里没有 #{card_id} 这张卡 —— 是不是记岔了一位数？"]
    row = conn.execute("SELECT * FROM memories WHERE id=?", (最终,)).fetchone()
    warnings = []
    if 跳:
        warnings.append(
            f"⚠️ #{card_id} 那张已经被更新过（跟了 {跳} 跳），下面给你的是现在这一版 #{最终}。")
    if 状态 and 状态.startswith("split:"):
        枝 = 状态.split(":", 1)[1]
        warnings.append(
            f"⚠️ #{card_id} 那张后来被**拆成了好几张**（#{枝}）—— 下面给你的是原来那一张。"
            "要看拆出来的，逐个用它们的号查。")
    elif 状态 == "fact_changed":
        warnings.append(
            f"⚠️ #{card_id} 后来被改过**事实**（不是改说法）—— 那已经不是同一件事了，"
            "所以这里【停在原来那一张】，没有替你跟过去。")
    elif 状态 == "superseded":
        warnings.append(
            f"⚠️ #{最终} 这一版已经不用了，而库里没有指定接替它的那张。下面照样给你看。")
    elif 状态 == "retracted":
        warnings.append(f"⚠️ #{最终} 已作废 —— 它还在库里，但不参与检索。下面照样给你看。")
    elif 状态 == "memento":
        warnings.append(f"⚠️ #{最终} 是纪念品 —— 保留着，但机制上不再被翻出来。")
    return [{"row": row, "line": format_line(conn, row, with_quote=True, cut=cut)}], warnings

def recall(conn, cfg, query, limit=None, touch=True, cut=None):
    m = _CARD_ID_RE.match(query or "")
    if m:
        return recall_by_card_id(conn, cfg, m.group(1), cut=cut)

    warnings = []
    limit = min(int(limit or cfg["recall"]["default_limit"]), cfg["recall"]["max_limit"])
    alpha = cfg["recall"]["alpha"]

    fts_scores = {}

    直搜 = {}
    词 = query.strip()
    if DIRECT_MIN_LEN <= len(词) <= DIRECT_MAX_LEN:
        rows = conn.execute(
            "SELECT id FROM memories WHERE status='active' AND target_memory_id IS NULL "
            "  AND (content LIKE ? OR COALESCE(src_quote,'') LIKE ?) "
            "ORDER BY id DESC LIMIT ?",
            (f"%{词}%", f"%{词}%", DIRECT_LIMIT),
        ).fetchall()
        for r in rows:
            直搜[r["id"]] = 1.0

    hit_names = query_entities(conn, query)
    match = segment.entity_fts_query(hit_names)
    if match:
        try:
            rows = conn.execute(
                "SELECT memory_id, bm25(memories_fts) AS rank FROM memories_fts "
                "WHERE memories_fts MATCH ? ORDER BY rank LIMIT 60",
                (match,),
            ).fetchall()
            if rows:
                ranks = [r["rank"] for r in rows]
                lo, hi = min(ranks), max(ranks)
                for r in rows:
                    fts_scores[r["memory_id"]] = 1.0 if hi == lo else (hi - r["rank"]) / (hi - lo)
        except Exception as e:
            warnings.append(f"⚠️ 实体词通道异常：{e}")

    vec_scores = {}
    if not embeddings.is_configured(cfg):
        warnings.append("⚠️ 向量通道未启用（config.json 未配置 embedding API Key），本次仅关键词检索。")
    else:
        try:
            qvec = embeddings.embed_texts(cfg, [query])[0]
            vec_scores = embeddings.vector_scores(conn, qvec)
        except embeddings.EmbeddingUnavailable as e:
            warnings.append(f"⚠️ 向量通道调用失败，已降级为纯关键词检索：{e}")

    candidate_ids = set(fts_scores) | set(vec_scores) | set(直搜)
    if not candidate_ids:
        return [], warnings

    now = datetime.now()
    scored = []
    for mid in candidate_ids:
        row = conn.execute(
            "SELECT * FROM memories WHERE id=? AND status='active' "
            "AND target_memory_id IS NULL AND is_fragment=0",
            (mid,),
        ).fetchone()
        if not row:
            continue
        score = alpha * max(0.0, vec_scores.get(mid, 0.0)) + (1 - alpha) * fts_scores.get(mid, 0.0)
        if mid in 直搜:
            score = max(score, 直搜[mid])
        scored.append((score, row))
    scored.sort(key=lambda t: t[0], reverse=True)
    top = scored[:limit]

    results = []
    for score, row in top:
        if touch:
            conn.execute(
                "UPDATE memories SET last_used_at=?, use_count=use_count+1 WHERE id=?",
                (now_iso(), row["id"]),
            )
        results.append({"score": score, "row": row,
                        "line": format_line(conn, row, with_quote=True, cut=cut)})
    return results, warnings

from .store import STANCE_LABELS

def stance_lines(conn, memory_id, indent="    "):
    rows = conn.execute(
        "SELECT stance, content, occurred_at, created_at, src_conversation_id, src_msg_start "
        "FROM memories WHERE target_memory_id=? AND status='active' ORDER BY id",
        (memory_id,),
    ).fetchall()
    out = []
    for r in rows:
        when = happened_on(conn, r)
        out.append(f"{indent}└ 我的态度 · {STANCE_LABELS.get(r['stance'], r['stance'])} · {when}：{r['content']}")
    return out

def _bridge_neighbors(conn, memory_id):
    mid = int(memory_id)
    ids = []
    mine = conn.execute("SELECT bridge_memory_id FROM memories WHERE id=?", (mid,)).fetchone()
    if mine and mine["bridge_memory_id"]:
        ids.append(int(mine["bridge_memory_id"]))
    ids += [int(r["id"]) for r in conn.execute(
        "SELECT id FROM memories WHERE bridge_memory_id=? AND status='active'", (mid,))]
    out = []
    for i in sorted(set(ids)):
        if i != mid and conn.execute(
                "SELECT 1 FROM memories WHERE id=? AND status='active'", (i,)).fetchone():
            out.append(i)
    return out

def bridge_ids(conn, memory_id):
    mid = int(memory_id)
    seen, queue = {mid}, [mid]
    while queue:
        cur = queue.pop()
        for i in _bridge_neighbors(conn, cur):
            if i not in seen:
                seen.add(i)
                queue.append(i)
    return sorted(seen - {mid})

def bridge_lines(conn, memory_id):
    out = []
    for i in bridge_ids(conn, memory_id):
        r = conn.execute("SELECT * FROM memories WHERE id=?", (i,)).fetchone()
        if not r:
            continue
        out.append(f"    └ {shell_mark(conn, r)} · {happened_on(conn, r)}：{r['content']}")
        out += stance_lines(conn, i, indent="        ")
    return out

QUOTE_MAX = 80

COMMITMENT_LABELS = {"open": "未兑现", "fulfilled": "已兑现", "void": "已作废"}

def shell_mark(conn, row):
    if row["kind"] == "note":
        return "我想过的"
    if row["kind"] == "taboo":
        return "碰到会疼"
    if row["kind"] != "quote":
        return "整理记录"
    signed = False
    if row["src_conversation_id"] and row["src_msg_start"]:
        signed = bool(conn.execute(
            "SELECT 1 FROM messages WHERE conversation_id=? AND id BETWEEN ? AND ? "
            "AND thinking_signature IS NOT NULL AND thinking_signature != '' LIMIT 1",
            (row["src_conversation_id"], row["src_msg_start"], row["src_msg_end"] or row["src_msg_start"]),
        ).fetchone())
    return "原话 · 带签名" if signed else "原话"

def evolution_step(conn, row):
    if row["kind"] != "note" or not row["note_container"]:
        return None
    prev = conn.execute(
        "SELECT content, occurred_at, created_at, src_conversation_id, src_msg_start "
        "FROM memories WHERE kind='note' AND note_container=? AND status='active' AND id < ? "
        "ORDER BY id DESC LIMIT 1",
        (row["note_container"], row["id"]),
    ).fetchone()
    if not prev:
        return None
    when = happened_on(conn, prev)
    return f"    └ 上一版 · {when}：{prev['content']}"

def happened_on(conn, row):
    if row["occurred_at"]:
        return str(row["occurred_at"])[:10]
    try:
        conv, start = row["src_conversation_id"], row["src_msg_start"]
    except (IndexError, KeyError):
        return ""
    if not (conv and start):
        return ""
    r = conn.execute(
        "SELECT created_at FROM messages WHERE conversation_id=? AND id=?", (conv, start)
    ).fetchone()
    if not (r and r["created_at"]):
        return ""
    offset = int(load_config().get("local_utc_offset_hours", 8))
    return to_local(r["created_at"], offset)[:10]

def _title_head(row, with_quote):
    title = ""
    try:
        title = (row["title"] or "").strip()
    except (IndexError, KeyError):
        title = ""
    if title:
        return title, False
    if not with_quote:
        return "", False
    try:
        quote = (row["src_quote"] or "").strip()
    except (IndexError, KeyError):
        return "", False
    if not quote or row["kind"] == "quote":
        return "", False
    截 = quote[:QUOTE_MAX] + "…" if len(quote) > QUOTE_MAX else quote

    try:
        对上了 = row["src_msg_start"] is not None
    except (IndexError, KeyError):
        对上了 = True
    if not 对上了:
        return f"{截}〔他记的，没在账本里对上〕", True
    return 截, True

CARD_CUT_CHARS = 120

CARD_CUT_MARK = "…"

def format_line(conn, row, with_quote=False, cut=None):
    parts = [shell_mark(conn, row), happened_on(conn, row)]
    ents = memory_entities_names(conn, row["id"])
    if ents:
        parts.append("、".join(ents[:3]))
    if row["kind"] == "commitment" and row["commitment_status"]:
        parts.append(COMMITMENT_LABELS.get(row["commitment_status"], row["commitment_status"]))

    head, head_is_quote = _title_head(row, with_quote)
    if head:
        parts.append(f"「{head}」")

    content = row["content"]
    if row["kind"] == "quote" and not content.startswith(("“", '"')):
        content = f"“{content}”"
    suffix = ""
    if row["supersedes"]:
        suffix = "（此条覆盖了更早版本，链可查）"
    superseded_by = conn.execute(
        "SELECT id FROM memories WHERE supersedes=? AND status='active'", (row["id"],)
    ).fetchone()
    if superseded_by:
        suffix += f"（注意：已被 #{superseded_by['id']} 更新）"
    if cut and len(content) > cut:
        content = content[:cut] + CARD_CUT_MARK
    line = f"[{' · '.join(p for p in parts if p)}] {content}{suffix}"

    if with_quote:
        quote = (row["src_quote"] or "").strip()
        if quote and row["kind"] != "quote" and quote not in content and not head_is_quote:
            if len(quote) > QUOTE_MAX:
                quote = quote[:QUOTE_MAX] + "…"
            line += f"\n    └ 当时原话：「{quote}」"

        context = (row["write_context"] or "").strip()
        if context:
            line += f"\n    └ 当时情境：{context}"

    step = evolution_step(conn, row)
    if step:
        line += "\n" + step

    for s in stance_lines(conn, row["id"]):
        line += "\n" + s
    for b in bridge_lines(conn, row["id"]):
        line += "\n" + b
    return line
