import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import runner, store, tools
from .config import load_config
from .db import now_iso, on_machine_axis

DEFAULT_NOTE_MAX = 1200
DEFAULT_QUOTE_MAX = 600

URGENCIES = ("now", "queued")
DEPTHS = ("link", "light", "research")
VERDICTS = ("breaks_circle", "echo_chamber", "unregistered")

class ForagingError(RuntimeError):
    pass

def ring_bell(conn, what, why, urgency="now", depth="light", parent_id=None):
    what = (what or "").strip()
    why = (why or "").strip()
    if not what:
        raise ValueError("门铃要说清「想读什么」")
    if not why:
        raise ValueError(
            "门铃要说清「**为什么**想看」，这一格不许空。\n"
            "🔴 它不是备注：这句话会被**逐字**当成取材指令送进隔离会话。\n"
            "   不这么接的话，`因为 Y` 就是一个建了却没有消费者的字段——"
            "这一类字段容易建了却没有消费者（commitment_status · write_context · field_visibility 都是）。"
        )
    if urgency not in URGENCIES:
        raise ValueError(
            f"urgency 只能是 {' / '.join(URGENCIES)}，收到 {urgency!r}。\n"
            "🔴 默认必须是 now。`queued` **只用于一种情况：他在她不在的时候想看的东西**——"
            "别把「立刻」也塞进那条队列。"
        )
    if depth not in DEPTHS:
        raise ValueError(
            f"depth 只能是 {' / '.join(DEPTHS)}，收到 {depth!r}。\n"
            "link=读一个指定链接（十几秒，可同步等）· light=轻查一两个源（一分钟内）·"
            " research=完整 research（**7 分钟量级，绝对不能让她坐着等**，异步 + 回来推送）。\n"
            "⚠️ 分界线不是「她给的 vs 他想的」，是「**要不要她等着**」——这跟 urgency 是两个轴，别合成一个。"
        )
    cur = conn.execute(
        "INSERT INTO reading_wishlist(created_at, what, why, urgency, depth, status, parent_id) "
        "VALUES(?,?,?,?,?, 'open', ?)",
        (now_iso(), what, why, urgency, depth, parent_id),
    )
    return cur.lastrowid

def go_again(conn, wish_id, why, depth=None):
    row = conn.execute("SELECT id, what, depth FROM reading_wishlist WHERE id=?",
                       (int(wish_id),)).fetchone()
    if not row:
        raise ValueError(f"清单里没有第 {wish_id} 条，追问挂不上去")
    return ring_bell(conn, row["what"], why, urgency="now",
                     depth=depth or row["depth"], parent_id=row["id"])

def wishlist_chain(conn, wish_id):
    out, seen = [], set()
    row = conn.execute("SELECT * FROM reading_wishlist WHERE id=?", (int(wish_id),)).fetchone()
    while row and row["id"] not in seen:
        seen.add(row["id"])
        out.append(dict(row))
        if row["parent_id"] is None:
            break
        row = conn.execute("SELECT * FROM reading_wishlist WHERE id=?", (row["parent_id"],)).fetchone()
    out.reverse()
    for r in conn.execute("SELECT * FROM reading_wishlist WHERE parent_id=? ORDER BY id",
                          (int(wish_id),)):
        out.append(dict(r))
    return out

_PROMPT = """你在一个一次性的会话里，只做一件事：去读东西，然后把材料带回来。

要读的：{what}

🔴 带回来什么，完全由下面这一句决定（这是委托你的人的原话，一字未改）：
{why}

你的活**不是"总结这个页面"**，是**回答上面那一句，并把相关的原句原样带回来**。

写成一个 JSON 对象，不要写别的：
{{
  "note": "你自己的话，回答上面那一句。上限 {note_max} 字。",
  "sources": [{{"title": "标题", "url": "链接或出处", "date": "日期或留空"}}],
  "quotes": [{{"text": "原样照抄的句子", "url": "它出自 sources 里的哪一个 url"}}]
}}

规矩：
· `sources` 不许空——**没有出处的东西一律不要带回来**。
· `quotes` 是**原样照抄**，一个字都不许改写。全部加起来上限 {quote_max} 字。
  ⭐ 遇到诗、歌词、某个人的文风、一段对话的语气——**那种东西语言本身就是内容，
    要照抄，不要复述**。（"这首诗表达了思念"，那就不是诗了。）
· 每条 quote 的 `url` 必须是 `sources` 里出现过的那个，否则引用跟出处对不上。
· 你**没有**记忆库，也**没有**任何能写东西的工具。别去找，那不是你这一趟的活。
"""

def build_prompt(what, why, note_max, quote_max):
    return _PROMPT.format(what=what, why=why, note_max=note_max, quote_max=quote_max)

def limits(cfg=None):
    cfg = load_config() if cfg is None else cfg
    blk = cfg.get("foraging") or {}
    return (int(blk.get("note_max_chars", DEFAULT_NOTE_MAX)),
            int(blk.get("quote_max_chars", DEFAULT_QUOTE_MAX)))

def _strip_fence(text):
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.S)
    return m.group(1) if m else t

def parse_result(text, note_max, quote_max):
    try:
        data = json.loads(_strip_fence(text))
    except (json.JSONDecodeError, TypeError):
        raise ForagingError(
            "隔离会话没给出能解析的 JSON —— **这一趟不算成功**。\n"
            f"   它说的前 400 字：{(text or '')[:400]}"
        ) from None
    if not isinstance(data, dict):
        raise ForagingError(f"隔离会话给的不是一个对象，是 {type(data).__name__}")

    note = (data.get("note") or "").strip()
    sources = data.get("sources") or []
    quotes = data.get("quotes") or []
    if not note:
        raise ForagingError("带回来的笔记是空的 —— 按验收状态机这一趟不算成功，不许当成「没什么可说的」")
    if not isinstance(sources, list) or not sources:
        raise ForagingError(
            "没有出处。**没有出处的想法不许进**——\n"
            "   这不是格式要求：读坏了要能一路查回去，查不回去的东西进了库就再也分不出真假。"
        )

    if len(note) > note_max:
        raise ForagingError(
            f"笔记 {len(note)} 字，超过上限 {note_max} 字。\n"
            "🔴 不截断、不自动缩写——**这道闸在的意义就是不让整篇文章当「笔记」抄回来**：\n"
            "   主会话直接读网页的话，那篇原文会永久留在这个窗口的历史里、每轮都在、赶不走"
            "（`-p` 下工具返回值就是会话文件的一部分，这里栽过一次）。"
        )

    urls = {(s.get("url") or "").strip() for s in sources if isinstance(s, dict)}
    urls.discard("")
    clean_quotes, total = [], 0
    for i, q in enumerate(quotes, 1):
        if not isinstance(q, dict):
            raise ForagingError(f"第 {i} 条引用不是对象：{q!r}")
        qt = (q.get("text") or "").strip()
        qu = (q.get("url") or "").strip()
        if not qt:
            continue
        if qu not in urls:
            raise ForagingError(
                f"第 {i} 条引用的出处 {qu!r} 不在 sources 里，**引用跟出处对不上**。\n"
                f"   sources 里有的是：{sorted(urls)}\n"
                "   ⚠️ 一条指不回出处的原样引用，读起来像证据、却查不回去 —— 那正是判语的形状。"
            )
        total += len(qt)
        clean_quotes.append({"text": qt, "url": qu})
    if total > quote_max:
        raise ForagingError(
            f"原样引用一共 {total} 字，超过额度 {quote_max} 字。\n"
            "⚠️ 这个数**刻意跟笔记上限分开**：额度小了会把诗 / 歌词 / 文风 / 语气全压死"
            "（那类内容语言本身就是内容），大了又等于整篇抄回来。\n"
            "   要调就去 config 的 `foraging.quote_max_chars`，**别在这儿截断**。"
        )
    return {"note": note, "sources": sources, "quotes": clean_quotes,
            "quote_chars": total, "note_chars": len(note)}

def run_isolated(cfg, what, why, cwd, proxy=None, model=None, effort=None,
                 claude_bin="claude", timeout=600, run=None):
    note_max, quote_max = limits(cfg)
    opts = tools.isolated_session_argv_opts(cfg)

    assert not opts["allowed_tools"] and not opts["mcp_servers"], (
        f"隔离会话被配上了 MCP 工具 {opts}——设计上要的是「没有能碰记忆库的工具」，"
        "而 MCP 工具只关得掉调用、关不掉可见性 ⇒ 只能整组不接"
    )

    run = run or runner.run_turn
    out = run(cwd, str(uuid.uuid4()), build_prompt(what, why, note_max, quote_max),
              model=model, effort=effort, claude_bin=claude_bin, timeout=timeout, proxy=proxy,
              tools=opts["tools"], allowed_tools=None, mcp_config=None,
              strict_mcp=True, new_session=True)
    parsed = parse_result(out["text"], note_max, quote_max)
    parsed["usage"] = out.get("usage") or {}
    parsed["total_cost_usd"] = out.get("total_cost_usd")
    return parsed

def source_registry(cfg=None):
    cfg = load_config() if cfg is None else cfg
    blk = cfg.get("sources") or {}
    items = blk.get("registry") or []
    if not isinstance(items, list):
        raise ValueError(f"sources.registry 必须是数组，收到 {type(items).__name__}")
    out = []
    for i, raw in enumerate(items, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"sources.registry 第 {i} 项不是对象：{raw!r}")
        match = (raw.get("match") or "").strip()
        if not match:
            raise ValueError(f"sources.registry 第 {i} 项没有 match")
        if "breaks_circle" not in raw:
            raise ValueError(
                f"来源 {match} 没写 breaks_circle。\n"
                "🔴 这一格不许留空：**默认算能打破圈 ＝ 闭环预警永不响；"
                "默认算同温层 ＝ 天天误报、最后被她关掉**。\n"
                "   判不出来就**别登记这一条**，让它落进「没登记」那一档去显形。"
            )
        out.append({"match": match, "breaks_circle": bool(raw["breaks_circle"]),
                    "note": raw.get("note") or ""})
    return out

def register_source(match, breaks_circle, note="", config_path=None):
    match = (match or "").strip()
    if not match:
        raise ValueError("登记来源要给 match（出处里出现的那一段，比如 arxiv.org）")
    if breaks_circle is None or isinstance(breaks_circle, str):
        raise ValueError(
            "breaks_circle 必须是 True/False，**不许留空、不许传字符串**。\n"
            "🔴 判不出来就**别登记这一条** —— 让它落进「没登记」那一档去显形。\n"
            "   默认算能打破圈 ＝ 闭环预警永不响；默认算同温层 ＝ 天天误报，最后被她关掉。"
        )

    from .config import ROOT
    path = Path(config_path) if config_path else ROOT / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} 解析失败，**没有动它**：{e}")

    blk = data.setdefault("sources", {})
    items = blk.setdefault("registry", [])
    if not isinstance(items, list):
        raise ValueError(f"sources.registry 必须是数组，收到 {type(items).__name__}")

    是新增 = True
    for it in items:
        if isinstance(it, dict) and (it.get("match") or "").strip().lower() == match.lower():
            it["breaks_circle"] = bool(breaks_circle)
            if note:
                it["note"] = note
            是新增 = False
            break
    if 是新增:
        items.append({"match": match, "breaks_circle": bool(breaks_circle), "note": note})

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 是新增, len(items)

def classify_source(raw_source, cfg=None):
    text = (raw_source or "").strip().lower()
    if not text:
        return "unregistered", None
    hit = None
    for item in source_registry(cfg):
        if item["match"].lower() in text:
            if hit is None or len(item["match"]) > len(hit["match"]):
                hit = item
    if hit is None:
        return "unregistered", None
    return ("breaks_circle" if hit["breaks_circle"] else "echo_chamber"), hit["match"]

def record_source(conn, memory_id, raw_source, cfg=None):
    verdict, key = classify_source(raw_source, cfg)
    conn.execute(
        "INSERT INTO note_sources(memory_id, raw_source, source_key, verdict, created_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET "
        "raw_source=excluded.raw_source, source_key=excluded.source_key, verdict=excluded.verdict",
        (int(memory_id), raw_source, key, verdict, now_iso()),
    )
    return verdict

def store_note(conn, cfg, note, src_quote, src_sentence_map, raw_source,
               occurred_at=None, author_window=None, **kw):
    mid = store.add_memory(
        conn, cfg, note, kind="note", zone=2,
        trigger_text=raw_source, trigger_type="external",
        src_quote=src_quote, src_sentence_map=src_sentence_map,
        occurred_at=occurred_at, author_window=author_window, **kw,
    )
    record_source(conn, mid, raw_source, cfg)
    return mid

def diagnose(conn, cfg=None, days=30):
    起 = on_machine_axis(datetime.now(timezone.utc) - timedelta(days=int(days)))
    rows = list(conn.execute(
        "SELECT verdict, raw_source FROM note_sources WHERE created_at >= ?", (起,)))
    checked = len(rows)
    counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in VERDICTS}
    alerts = []
    if checked and counts["breaks_circle"] == 0:
        alerts.append(
            f"🔴 闭环预警：最近 {days} 天的 {checked} 条笔记里，**能打破圈的一条都没有**。\n"
            "   （这一条同时覆盖两种情况：全是他自己以前想的 · 全是同温层。）"
        )
    if counts["unregistered"]:
        alerts.append(
            f"有 {counts['unregistered']} 条来源没登记 —— 它们**不计入任何一侧**。\n"
            "   登记走 `foraging.register_source(match, breaks_circle)`"
            "（质检台那两个按钮接的就是它）。\n"
            "   ⚠️ **判不出来就别登记** —— 让它留在这一档里显形，比猜一个填进去好。"
        )
    return {"checked": checked, "days": days, "counts": counts, "alerts": alerts,
            "line": f"本次检查了 {checked} 条笔记来源"
                    f"（能打破圈 {counts['breaks_circle']} · 同温层 {counts['echo_chamber']} ·"
                    f" 没登记 {counts['unregistered']}）"}
