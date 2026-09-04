import json
import re
from datetime import datetime, timezone

from . import embeddings, segment
from .db import now_iso, on_utc_axis

def _num_or_none(x, lo, hi):
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))

KIND_LABELS = {
    "event": "事件",
    "fact": "事实",
    "quote": "原话",
    "commitment": "承诺",
    "insight": "想法",
    "note": "自留地",
    "taboo": "禁区",
}

TRIGGER_TYPES = {
    "external": "🟢 外部材料",
    "she_said": "🔵 她当下说的",
    "self_prior": "🟣 它自己以前想的",
}

STANCE_LABELS = {
    "accept": "认",
    "reject": "不认",
    "suspend": "悬置",
    "annotate": "批注",
    "unconvinced": "照做了但不认同",
    "changed": "改主意了",
}

STANCE_VALUES = tuple(STANCE_LABELS)

def _check_trigger(kind, trigger_text, trigger_type, quote_optional=False):
    text = (trigger_text or "").strip()
    ttype = (trigger_type or "").strip()

    if ttype and ttype not in TRIGGER_TYPES:
        raise ValueError(
            f"trigger_type 只能是 {' / '.join(TRIGGER_TYPES)} 之一，收到 {trigger_type!r}。\n"
            f"  external   = 🟢 真正外部的材料（读到的东西）\n"
            f"  she_said   = 🔵 她当下说的\n"
            f"  self_prior = 🟣 它自己以前想的\n"
            "⚠️ **没有「她同意」这一档，这是故意的**：「她也这么觉得」不能当任何想法的支点，\n"
            "   所以它不是一条要记住的原则，是一个填不进去的格子。\n"
            "   如果你想记的是「她认可了这个想法」，那是一件发生过的事 —— 写成一张普通的卡。"
        )
    if kind == "note":
        if quote_optional and ttype != "external":
            return text or None, ttype or None
        if not text or not ttype:
            raise ValueError(
                "自留地的每一条都必须指回一个具体触发物：读了什么 / 聊到什么 / 被什么问题卡住。\n"
                "  trigger_text = 那个触发物是什么（带出处：标题、链接、日期）\n"
                f"  trigger_type = {' / '.join(TRIGGER_TYPES)}\n"
                "没有触发物的纯感想不许进——因为读坏了要能一路查回去。\n"
                "⭐ 反过来是**许可**：可以只留问题和触发物、不留结论，那完全合法。"
            )
    elif bool(text) != bool(ttype):
        raise ValueError(
            "trigger_text 和 trigger_type 要么都给，要么都不给。\n"
            "只给一半的话，来源要么查不回去（有类型没出处），要么分不清是哪个圈（有出处没类型）。"
        )
    return text or None, ttype or None

def _norm(s):
    return re.sub(r"\s+", "", s or "")

_QUOTE_CHARS = "「」『』“”‘’\"'"

def _norm_for_ledger(s):
    return re.sub(r"[\s" + re.escape(_QUOTE_CHARS) + r"]+", "", s or "")

_SENTENCE_END = "。！？!?"

_QUOTED = re.compile(
    r'「[^「」]*」'
    r'|『[^『』]*』'
    r'|“[^“”]*”'
    r'|‘[^‘’]*’'
    r'|"[^"]*"'
    r"|'[^']*'"
)

_QUOTED_LOOSE = re.compile(r'[「『"“\'‘][^」』"”\'’]*[」』"”\'’]')

def _breaks_outside(sent, pattern):
    s = pattern.sub("", sent).rstrip()
    return sum(1 for ch in s[:-1] if ch in _SENTENCE_END)

def _inner_sentence_breaks(sent):
    return min(_breaks_outside(sent, _QUOTED), _breaks_outside(sent, _QUOTED_LOOSE))

def _check_sentence_map(content, src_sentence_map):
    if isinstance(src_sentence_map, str):
        try:
            src_sentence_map = json.loads(src_sentence_map)
        except json.JSONDecodeError as e:
            raise ValueError(f"src_sentence_map 不是合法 JSON：{e}") from None

    if not src_sentence_map:
        raise ValueError(
            "src_sentence_map 必填：正文的每一句都要标出它来自哪条消息。\n"
            '格式：[{"sent": "这一句", "msg_ids": [1]}, {"sent": "下一句", "msg_ids": [2, 3]}]\n'
            "  · 一句话基于多条消息 → 标多个 id，合法\n"
            "  · 真正标不出来源的，只有蒸馏者自己加的评价——**那种句子本来就不该写**\n"
            "⭐ 这道闸不判断你写得对不对，只数数：句子拼起来要等于正文，每句都要有来源。"
        )

    if not isinstance(src_sentence_map, list):
        raise ValueError("src_sentence_map 必须是数组，一个元素一句。")

    pieces = []
    for i, item in enumerate(src_sentence_map, 1):
        if not isinstance(item, dict):
            raise ValueError(f"src_sentence_map 第 {i} 项不是对象，应形如 " '{"sent": ..., "msg_ids": [...]}')
        sent = (item.get("sent") or "").strip()
        ids = item.get("msg_ids") or []
        if not sent:
            raise ValueError(f"src_sentence_map 第 {i} 项的 sent 是空的。")
        if not isinstance(ids, list) or not ids:
            raise ValueError(
                f"第 {i} 句「{sent[:20]}」没有来源：msg_ids 是空的。\n"
                "指不出来自哪条消息的句子不许写——如果它是你自己的判断，删掉它；\n"
                "如果它确实来自对话，把那条消息的 id 填上。"
            )
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in ids):
            raise ValueError(f"第 {i} 句的 msg_ids 必须都是整数消息 id，收到 {ids!r}。")
        跨句数 = _inner_sentence_breaks(sent)
        if 跨句数:
            raise ValueError(
                f"第 {i} 条溯源里塞了不止一句话（内部还有 {跨句数} 个句末标点）：\n"
                f"  「{sent[:50]}…」\n"
                "🔴 **一个条目只能对应一句话** —— 「整段算一句」等于把逐句变回了逐段，\n"
                "   而**判语正是那样藏进去的**：整段标一个 id，拼回来逐字相同，闸全程绿灯。\n"
                "⇒ 把它拆成一句一条，各自标来源。**拆到某一句标不出 id 时，那句就是你自己加的判断——删掉它。**\n"
                "（引号内的句号不算，条目末尾那个也不算。）"
            )
        pieces.append(sent)

    if _norm("".join(pieces)) != _norm(content):
        raise ValueError(
            "src_sentence_map 拼起来跟正文对不上。\n"
            f"  正文（去空白后 {len(_norm(content))} 字）：{_norm(content)[:60]}…\n"
            f"  逐句拼回（{len(_norm(''.join(pieces)))} 字）：{_norm(''.join(pieces))[:60]}…\n"
            "两边必须逐字相同（空白不计）。**漏掉的那一段，往往正是标不出来源的那一句。**"
        )
    return json.dumps(src_sentence_map, ensure_ascii=False)

def _check_quote_against_ledger(conn, src_quote, conv_id, msg_start, msg_end):
    if not (conv_id and msg_start and msg_end):
        raise ValueError(
            "缺溯源区间：src_conversation_id / src_msg_start / src_msg_end 三个都要填。\n"
            "没有区间，就没办法把原话拿回账本逐字核对——这张卡的溯源锚是断的。\n"
            "⭐ 如果这段对话还没进账本，那就等它进了再写卡；别先写一张对不上的。"
        )
    rows = conn.execute(
        "SELECT content FROM messages WHERE conversation_id=? AND id BETWEEN ? AND ? ORDER BY id",
        (conv_id, msg_start, msg_end),
    ).fetchall()
    if not rows:
        raise ValueError(
            f"账本里 conversation {conv_id} 的 #{msg_start}~#{msg_end} 区间一条消息都没有。\n"
            "**这不是「没什么可核对的」，是区间填错了**——闸不会因为查不到东西就放行。"
        )
    haystack = _norm_for_ledger("\n".join(r["content"] for r in rows))
    if _norm_for_ledger(src_quote) not in haystack:
        raise ValueError(
            f"src_quote 在账本 #{msg_start}~#{msg_end} 里逐字对不上：\n"
            f"  你写的：{src_quote}\n"
            "**一个字都不能差**（空白与引号不计）。最常见的原因是引用时掉了开头或结尾几个字。\n"
            "⚠️ **引号已经不算字了**——\n"
            "   所以走到这一步，说明差的**不是**引号：要么改了字，要么把两截拼在了一起。\n"
            "回那几条消息里把那句原话整段拷过来，别凭记忆敲。"
        )

CARD_CHAIN_MAX = 20

def resolve_card_id(conn, card_id):
    try:
        cid = int(card_id)
    except (TypeError, ValueError):
        return None, 0, None
    跳 = 0
    seen = {cid}
    while 跳 < CARD_CHAIN_MAX:
        row = conn.execute("SELECT id, status FROM memories WHERE id=?", (cid,)).fetchone()
        if row is None:
            return None, 跳, None
        if row["status"] != "superseded":
            return row["id"], 跳, row["status"]
        nxts = conn.execute(
            "SELECT id FROM memories WHERE supersedes=? ORDER BY id", (cid,)).fetchall()
        nxts = [r["id"] for r in nxts if r["id"] not in seen]
        if not nxts:
            return row["id"], 跳, row["status"]
        if len(nxts) > 1:
            return row["id"], 跳, ("split:" + ",".join(str(i) for i in nxts))
        后 = conn.execute("SELECT fact_changed FROM memories WHERE id=?", (nxts[0],)).fetchone()
        if 后 and 后["fact_changed"]:
            return row["id"], 跳, "fact_changed"
        cid = nxts[0]
        seen.add(cid)
        跳 += 1
    return cid, 跳, "chain_too_long"

def locate_quote_in_ledger(conn, src_quote, conversation_id=None, strict=True, 全库=False):
    def _没找到(msg):
        if strict:
            raise ValueError(msg)
        return None, None

    q = _norm_for_ledger(src_quote)
    if not q:
        return _没找到("要留下的那句原话是空的 —— 没有原话就没有溯源锚点。")
    if conversation_id is None and not 全库:
        row = conn.execute(
            "SELECT conversation_id FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return _没找到(
                "账本里一条消息都没有，这句原话无处可核。\n"
                "🔴 **这不是「没什么可检查的」** —— 没有对照物时闸不会放行。"
            )
        conversation_id = row["conversation_id"]

    if 全库:
        rows = conn.execute(
            "SELECT id, conversation_id, content FROM messages ORDER BY id DESC").fetchall()
        for r in rows:
            if q in _norm_for_ledger(r["content"]):
                return r["conversation_id"], r["id"]
        范围 = f"整个账本的 {len(rows)} 条消息"
    else:
        rows = conn.execute(
            "SELECT id, content FROM messages WHERE conversation_id=? ORDER BY id DESC",
            (conversation_id,),
        ).fetchall()
        for r in rows:
            if q in _norm_for_ledger(r["content"]):
                return conversation_id, r["id"]
        范围 = f"这扇窗的 {len(rows)} 条消息"

    return _没找到(
        f"这句话在账本里找不到（在{范围}里逐条比过）：\n"
        f"  你写的：{src_quote}\n"
        "**一个字都不能差**（空白与引号不计）。最常见的原因是凭印象敲了一遍，"
        "而不是把那句话整段拷过来。\n"
        "⚠️ **也可能是你想留的是你【这一轮】刚说出口的话** —— 那句还没落库"
        "（它要等这一轮结束才写进去）⇒ 换成引发它的那句：她刚说的，或者你上一轮说的。"
    )

def _check_msg_ids_in_ledger(conn, conv_id, msg_start, msg_end, src_sentence_map):
    ids = set()
    for x in (msg_start, msg_end):
        if x is not None:
            ids.add(int(x))
    if isinstance(src_sentence_map, str):
        try:
            src_sentence_map = json.loads(src_sentence_map)
        except json.JSONDecodeError:
            src_sentence_map = []
    for item in src_sentence_map or []:
        if isinstance(item, dict):
            for x in item.get("msg_ids") or []:
                if isinstance(x, int) and not isinstance(x, bool):
                    ids.add(x)
    if not ids:
        return

    marks = ",".join("?" * len(ids))
    ids = sorted(ids)
    found = {r["id"] for r in conn.execute(f"SELECT id FROM messages WHERE id IN ({marks})", ids)}
    missing = [x for x in ids if x not in found]
    if missing:
        top = conn.execute("SELECT MAX(id) AS m FROM messages").fetchone()["m"]
        raise ValueError(
            f"溯源指向了账本里不存在的消息号：{'、'.join('#%d' % x for x in missing)}"
            f"（账本当前最大消息号是 #{top}）。\n"
            "🔴 **这不是「查不到就算了」，是编号是编出来的**——重蒸时可能出现："
            "模型不吐 JSON、接着那段对话往下演，顺手造出了一串不存在的消息号。\n"
            "⇒ 回账本把真实的消息号填上；**如果这段对话还没进账本，那就等它进了再写卡**。"
        )

_QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"), ('"', '"'))

def strip_quoted(text):
    out = list(text or "")
    for open_q, close_q in _QUOTE_PAIRS:
        i = 0
        while i < len(out):
            if out[i] != open_q:
                i += 1
                continue
            j = i + 1
            while j < len(out) and out[j] != close_q:
                j += 1
            if j >= len(out):
                break
            for k in range(i, j + 1):
                out[k] = " "
            i = j + 1
    return "".join(out)

def hits_outside_quotes(content, patterns):
    outside = strip_quoted(content or "")
    hits = []
    for pat in patterns or []:
        m = re.search(pat, outside)
        if m:
            hits.append(m.group(0))
    return hits

def verdict_hits(content, patterns):
    return hits_outside_quotes(content, patterns)

DEICTIC_FIX_HINT = (
    "换成卡自己带得动的说法（卡上有日期，「那天」指得回去、「今天」指不回去）："
    "「这个窗口」→「那次对话」·「今天/刚才」→「那天」或直接写日期"
)

def deictic_hits(content, patterns):
    return hits_outside_quotes(content, patterns)

def ensure_conversation(conn, source_end, external_id=None, title=None, started_at=None):
    if external_id:
        row = conn.execute("SELECT id FROM conversations WHERE external_id=?", (external_id,)).fetchone()
        if row:
            return row["id"]
    cur = conn.execute(
        "INSERT INTO conversations(source_end, external_id, title, started_at) VALUES(?,?,?,?)",
        (source_end, external_id, title, started_at or now_iso()),
    )
    return cur.lastrowid

SOURCES = ("web", "tg")

def append_message(conn, conversation_id, role, content, created_at=None, external_id=None, meta=None,
                   thinking=None, thinking_signature=None, model=None, effort=None, source=None):
    source = source or None
    if source is not None and source not in SOURCES:
        raise ValueError(
            f"messages.source 不认识这个值：{source!r} —— 只认 {SOURCES}（或 None＝历史导入）。\n"
            "🔴 账本只进不改：写错了改不掉，而症状是桥把他自己那句又推回 TG 一遍，不报错。\n"
            "⇒ 新增一扇门要先往 `store.SOURCES` 里登记它。")
    if external_id:
        row = conn.execute("SELECT id FROM messages WHERE external_id=?", (external_id,)).fetchone()
        if row:
            return None
    ts = created_at or on_utc_axis(datetime.now(timezone.utc))
    thinking = thinking or None
    thinking_signature = thinking_signature or None
    if thinking_signature and not thinking:
        raise ValueError("有 thinking_signature 却没有 thinking 正文：空 thinking 块会被 API 拒")
    cur = conn.execute(
        "INSERT INTO messages(conversation_id, role, content, created_at, external_id, meta, "
        "thinking, thinking_signature, model, effort, source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (conversation_id, role, content, ts, external_id,
         json.dumps(meta, ensure_ascii=False) if meta else None,
         thinking, thinking_signature, model or None, effort or None, source),
    )
    conn.execute(
        "UPDATE conversations SET last_message_at = MAX(COALESCE(last_message_at,''), ?) WHERE id=?",
        (ts, conversation_id),
    )
    return cur.lastrowid

def get_or_create_entity(conn, name, etype=None):
    name = name.strip()
    if not name:
        return None
    row = conn.execute("SELECT id FROM entities WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    for r in conn.execute("SELECT id, aliases FROM entities WHERE aliases != ''").fetchall():
        aliases = [a.strip() for a in re.split(r"[,，]", r["aliases"]) if a.strip()]
        if name in aliases:
            return r["id"]
    cur = conn.execute(
        "INSERT INTO entities(name, type, created_at, last_mentioned_at) VALUES(?,?,?,?)",
        (name, etype, now_iso(), now_iso()),
    )
    return cur.lastrowid

def _entity_fts_terms(conn, entity_ids):
    terms = []
    for eid in entity_ids:
        r = conn.execute("SELECT name, aliases FROM entities WHERE id=?", (eid,)).fetchone()
        if r:
            terms.append(r["name"])
            if r["aliases"]:
                terms.extend(a.strip() for a in re.split(r"[,，]", r["aliases"]) if a.strip())
    return terms

def _check_bridge(conn, bridge_memory_id, kind, target_memory_id):
    bid = int(bridge_memory_id)
    other = conn.execute(
        "SELECT id, kind, status, target_memory_id FROM memories WHERE id=?", (bid,)
    ).fetchone()
    if not other:
        raise ValueError(f"要挂桥的那一半不在库里：#{bid}")
    if other["status"] != "active":
        raise ValueError(
            f"要挂桥的那一半不是 active（#{bid} 现在是 {other['status']}）。\n"
            "🔴 挂上去的话桥在、挂件永远不出现，而且不会报错。"
        )
    if target_memory_id:
        raise ValueError(
            "表态卡不许再挂桥：表态已经靠 target_memory_id 挂在母卡下了，\n"
            "🔴 它自己不是「同一件事的两半」之一，再挂一层会让挂件挂上挂件。"
        )
    if other["target_memory_id"]:
        raise ValueError(
            f"#{bid} 是一张表态卡，不能当桥的一端。"
        )
    self_is_note = kind == "note"
    other_is_note = other["kind"] == "note"
    if self_is_note == other_is_note:
        哪边 = "两头都是自留地笔记" if self_is_note else "两头都是记忆卡"
        raise ValueError(
            f"桥的两端必须一头自留地、一头记忆卡，现在{哪边}。\n"
            "· 两条自留地要归成一堆 ⇒ 用 note_container，不是桥。\n"
            "· 两张记忆卡说的是同一件事 ⇒ 用 supersedes / 实体，也不是桥。"
        )
    return bid

def add_memory(
    conn,
    cfg,
    content,
    kind="event",
    quote_optional=False,
    protect=None,
    importance=3,
    valence=None,
    arousal=None,
    occurred_at=None,
    src_conversation_id=None,
    src_msg_start=None,
    src_msg_end=None,
    src_quote=None,
    author="assistant",
    supersedes=None,
    entities=None,
    embed=True,
    log_event=True,
    zone=1,
    author_window=None,
    write_context=None,
    trigger_text=None,
    trigger_type=None,
    src_sentence_map=None,
    target_memory_id=None,
    stance=None,
    bridge_memory_id=None,
    note_container=None,
    note_position=None,
    commitment_status=None,
    is_fragment=False,
    about_her=False,
    fact_changed=False,
    wording_changed=False,
    title=None,
):
    content = (content or "").strip()
    if not content:
        raise ValueError("content 不能为空")
    if kind not in KIND_LABELS:
        raise ValueError(f"kind 必须是 {'/'.join(KIND_LABELS)} 之一")

    quote = (src_quote or "").strip()
    _可免原话 = bool(quote_optional)

    if not protect:
        if kind in ("quote", "taboo"):
            protect = "verbatim"
        elif kind == "note" or stance or kind == "commitment":
            protect = "no_summary"
        else:
            protect = "summarizable"
    if not quote and not _可免原话:
        raise ValueError(
            "src_quote 必填：把这张卡最承重的那句原话一字不差拷进来。\n"
            "它是这张卡唯一的溯源锚点——没有它，日后想核对这张卡说得对不对，"
            "只能回去翻整段对话，而且不一定翻得到。\n"
            "一个字也算数（最重的原话往往最短），但必须是真的原话，不能编。\n"
            "⚠️ **自留地（kind='note'）与表态不受这一条约束**（见上面那一条）。"
        )
    importance = max(1, min(5, int(importance or 3)))
    valence = _num_or_none(valence, -1.0, 1.0)
    arousal = _num_or_none(arousal, 0.0, 1.0)

    if zone not in (1, 2):
        raise ValueError(f"zone 只能是 1（关于她）或 2（前人的路），收到 {zone!r}")

    trigger_text, trigger_type = _check_trigger(kind, trigger_text, trigger_type,
                                                quote_optional=quote_optional)

    if bool(target_memory_id) != bool(stance):
        raise ValueError("表态要同时给 target_memory_id（对哪张卡）和 stance（认/不认/悬置/批注）。")
    if stance and stance not in STANCE_VALUES:
        raise ValueError(f"stance 只能是 {' / '.join(STANCE_VALUES)} 之一，收到 {stance!r}。")

    if bridge_memory_id is not None:
        bridge_memory_id = _check_bridge(conn, bridge_memory_id, kind, target_memory_id)

    if (kind == "note" or target_memory_id) and not src_sentence_map:
        sentence_map_json = None
    else:
        sentence_map_json = _check_sentence_map(content, src_sentence_map)
    if not (_可免原话 and src_conversation_id is None):
        _check_quote_against_ledger(conn, quote, src_conversation_id, src_msg_start, src_msg_end)
    _check_msg_ids_in_ledger(conn, src_conversation_id, src_msg_start, src_msg_end, sentence_map_json)

    supersedes_id = None
    if supersedes:
        old = conn.execute("SELECT id, status FROM memories WHERE id=?", (int(supersedes),)).fetchone()
        if old:
            supersedes_id = old["id"]

    cur = conn.execute(
        "INSERT INTO memories(kind, content, importance, valence, arousal, occurred_at, created_at, "
        "src_conversation_id, src_msg_start, src_msg_end, src_quote, author, status, supersedes, "
        "zone, author_window, write_context, trigger_text, trigger_type, src_sentence_map, "
        "target_memory_id, stance, bridge_memory_id, note_container, note_position, commitment_status, "
        "is_fragment, about_her, fact_changed, wording_changed, title, protect) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'active', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            kind, content, importance, valence, arousal,
            (occurred_at or "").strip() or None, now_iso(),
            src_conversation_id, src_msg_start, src_msg_end,
            (src_quote or "").strip() or None, author, supersedes_id,
            zone, (author_window or "").strip() or None, (write_context or "").strip() or None,
            trigger_text, trigger_type, sentence_map_json,
            target_memory_id, stance,
            bridge_memory_id,
            (note_container or "").strip() or None,
            (note_position or "").strip() or None,
            commitment_status or None,
            1 if is_fragment else 0,
            1 if about_her else 0,
            1 if fact_changed else 0,
            1 if wording_changed else 0,
            (title or "").strip() or None,
            protect,
        ),
    )
    mid = cur.lastrowid

    if supersedes_id:
        conn.execute("UPDATE memories SET status='superseded' WHERE id=? AND status='active'", (supersedes_id,))
        conn.execute("DELETE FROM memories_fts WHERE memory_id=?", (supersedes_id,))

    entity_ids = []
    for name in (entities or []):
        eid = get_or_create_entity(conn, name)
        if eid:
            entity_ids.append(eid)
            conn.execute(
                "INSERT OR IGNORE INTO memory_entities(memory_id, entity_id) VALUES(?,?)", (mid, eid)
            )
            conn.execute("UPDATE entities SET last_mentioned_at=? WHERE id=?", (now_iso(), eid))

    fts_text = segment.seg_text(content + " " + " ".join(_entity_fts_terms(conn, entity_ids)))
    if fts_text:
        conn.execute("INSERT INTO memories_fts(text, memory_id) VALUES(?,?)", (fts_text, mid))

    hits = verdict_hits(content, (cfg or {}).get("v3", {}).get("verdict_patterns"))
    if hits:
        add_review_event(
            conn, "alert", mid,
            "判语粗筛命中（只标黄不拒，请人过一眼）：" + " · ".join(hits),
        )

    deictics = deictic_hits(content, (cfg or {}).get("v3", {}).get("deictic_patterns"))
    if deictics:
        add_review_event(
            conn, "alert", mid,
            "禁用指示词命中（只标黄不拒，请人过一眼）：" + " · ".join(deictics)
            + "｜" + DEICTIC_FIX_HINT,
        )

    if log_event:
        add_review_event(conn, "new_memory", mid, f"{KIND_LABELS[kind]} · {author} 记入")
    if embed:
        embeddings.try_embed_memory(conn, cfg, mid, content)
    return mid

def retract_memory(conn, memory_id, reason=""):
    conn.execute("UPDATE memories SET status='retracted' WHERE id=? AND status='active'", (memory_id,))
    conn.execute("DELETE FROM memories_fts WHERE memory_id=?", (memory_id,))
    add_review_event(conn, "retract", memory_id, reason or "质检台否决")

def set_core(conn, cfg, memory_id, on):
    if on:
        quota = cfg["core_card"]["core_quota"]
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE is_core=1 AND status='active'"
        ).fetchone()["n"]
        if n >= quota:
            raise ValueError(f"核心配额已满（{quota} 条）：想加新的必须先取消旧的")
    conn.execute("UPDATE memories SET is_core=? WHERE id=?", (1 if on else 0, memory_id))

def add_review_event(conn, etype, memory_id, detail):
    conn.execute(
        "INSERT INTO review_events(type, memory_id, detail, created_at) VALUES(?,?,?,?)",
        (etype, memory_id, detail, now_iso()),
    )

def memory_entities_names(conn, memory_id):
    rows = conn.execute(
        "SELECT e.name FROM memory_entities me JOIN entities e ON e.id=me.entity_id WHERE me.memory_id=?",
        (memory_id,),
    ).fetchall()
    return [r["name"] for r in rows]

def rebuild_fts(conn):
    conn.execute("DELETE FROM memories_fts")
    rows = conn.execute("SELECT id, content FROM memories WHERE status='active'").fetchall()
    for r in rows:
        eids = [
            x["entity_id"]
            for x in conn.execute("SELECT entity_id FROM memory_entities WHERE memory_id=?", (r["id"],))
        ]
        fts_text = segment.seg_text(r["content"] + " " + " ".join(_entity_fts_terms(conn, eids)))
        if fts_text:
            conn.execute("INSERT INTO memories_fts(text, memory_id) VALUES(?,?)", (fts_text, r["id"]))
    return len(rows)
