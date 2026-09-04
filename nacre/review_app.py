import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from urllib.parse import urlparse

from flask import Flask, redirect, render_template, request, url_for

from nacre import (core_card, diagnose, embeddings, foraging, handles,
                           nightly, resident_index, search, store)
from nacre.config import load_config
from nacre.db import get_conn, now_iso, to_local, write_session

app = Flask(__name__)

TABS = [
    ("today", "今日"),
    ("log", "日志"),
    ("inject", "注入"),
    ("him", "关于他"),
    ("all", "全部"),
    ("dash", "看板"),
]

FACT_CHANGED_MARK = "[改事实]"
WORDING_CHANGED_MARK = "[改说法]"

@app.template_filter("md_bold")
def _md_bold(text):
    from markupsafe import Markup, escape

    parts = escape(text or "").split("**")
    if len(parts) % 2 == 0:
        return Markup("**".join(parts))
    return Markup("".join(p if i % 2 == 0 else f"<b>{p}</b>" for i, p in enumerate(parts)))

@app.before_request
def _block_cross_site_writes():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if source and urlparse(source).netloc != request.host:
        return "跨站写入已拒绝：请直接在质检台页面上操作。", 403
    return None

def _cfg():
    return load_config()

ZONE_LABELS = {1: "关于她", 2: "前人的路"}

from nacre.store import STANCE_LABELS

FIELD_LABELS = {
    "content": "正文", "occurred_at": "事发日期", "created_at": "入库日期",
    "src_quote": "原话锚点", "stance": "表态", "commitment_status": "承诺状态",
    "trigger_text": "触发物", "trigger_type": "来源档", "write_context": "温度标注",
    "src_sentence_map": "逐句溯源", "author_window": "窗口", "kind": "类型",
    "target_memory_id": "表态对象", "note_container": "容器", "importance": "重要度",
    "src_conversation_id": "对话 id", "src_msg_start": "起始消息", "src_msg_end": "结束消息",
    "supersedes": "覆盖了", "id": "卡号", "author": "作者",
    "valence": "情绪 v", "arousal": "情绪 a",
}

def _field_visibility(conn, table="memories"):
    try:
        rows = conn.execute(
            "SELECT column_name, model_visible, note FROM field_visibility WHERE table_name=?",
            (table,),
        ).fetchall()
    except Exception:
        return {}
    return {r["column_name"]: {"visible": bool(r["model_visible"]), "note": r["note"] or ""}
            for r in rows}

def _visible_fields(row, fv):
    keys = row.keys()
    out = []
    for col in keys:
        if col not in fv and row[col] in (None, "", 0):
            continue
        if col in fv and row[col] in (None, ""):
            continue
        state = "unregistered" if col not in fv else ("visible" if fv[col]["visible"] else "hidden")
        out.append({
            "col": col,
            "label": FIELD_LABELS.get(col, col),
            "value": row[col],
            "state": state,
            "note": fv.get(col, {}).get("note", ""),
        })
    order = {"visible": 0, "unregistered": 1, "hidden": 2}
    out.sort(key=lambda f: (order[f["state"]], f["col"]))
    return out

def _stances_for(conn, memory_id):
    ids = [memory_id] + _wording_chain(conn, memory_id)
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM memories WHERE target_memory_id IN ({marks}) AND stance IS NOT NULL "
        "AND status='active' ORDER BY id",
        ids,
    ).fetchall()
    return rows

def _wording_chain(conn, memory_id):
    out, cur = [], memory_id
    seen = {memory_id}
    while True:
        row = conn.execute("SELECT supersedes FROM memories WHERE id=?", (cur,)).fetchone()
        prev = row["supersedes"] if row else None
        if not prev or prev in seen:
            return out
        if _fact_changed(conn, cur):
            return out
        out.append(prev)
        seen.add(prev)
        cur = prev

def _fact_changed(conn, memory_id):
    row = conn.execute("SELECT fact_changed FROM memories WHERE id=?", (memory_id,)).fetchone()
    return bool(row and row["fact_changed"])

def _memory_view(conn, row, fv=None, with_stances=True):
    src_window = None
    if row["src_conversation_id"]:
        r = conn.execute(
            "SELECT window_name FROM conversations WHERE id=?", (row["src_conversation_id"],)
        ).fetchone()
        src_window = r["window_name"] if r else None
    fv = _field_visibility(conn) if fv is None else fv
    return {
        "row": row,
        "entities": store.memory_entities_names(conn, row["id"]),
        "kind_label": store.KIND_LABELS.get(row["kind"], row["kind"]),
        "src": _src_text(conn, row),
        "zone_label": ZONE_LABELS.get(row["zone"]) if row["zone"] != 1 else None,
        "src_window": src_window,
        "happened_on": search.happened_on(conn, row),
        "filed_on": (row["created_at"] or "")[:10],
        "fields": _visible_fields(row, fv),
        "stances": [
            {"row": s, "label": STANCE_LABELS.get(s["stance"], s["stance"])}
            for s in (_stances_for(conn, row["id"]) if with_stances else [])
        ],
    }

def _with_pct(items):
    mx = max([it["n"] for it in items], default=0) or 1
    for it in items:
        it["pct"] = round(it["n"] / mx * 100, 1)
    return items

def _dashboard(conn):
    def counts(sql):
        return {r["k"]: r["n"] for r in conn.execute(sql).fetchall()}

    kind = counts("SELECT kind k, COUNT(*) n FROM memories WHERE status='active' GROUP BY kind")
    status = counts("SELECT status k, COUNT(*) n FROM memories GROUP BY status")
    author = counts("SELECT author k, COUNT(*) n FROM memories WHERE status='active' GROUP BY author")
    trig = counts("SELECT trigger_type k, COUNT(*) n FROM memories "
                  "WHERE status='active' AND kind='note' GROUP BY trigger_type")
    stance = counts("SELECT stance k, COUNT(*) n FROM memories "
                    "WHERE status='active' AND stance IS NOT NULL GROUP BY stance")

    active_n = conn.execute(
        "SELECT COUNT(*) n FROM memories WHERE status='active'").fetchone()["n"]
    vec_n = conn.execute(
        "SELECT COUNT(*) n FROM memories m JOIN embeddings e ON e.memory_id=m.id WHERE m.status='active'"
    ).fetchone()["n"]
    src_n = conn.execute(
        "SELECT COUNT(*) n FROM memories WHERE status='active' "
        "AND (src_conversation_id IS NOT NULL OR (src_quote IS NOT NULL AND src_quote != ''))"
    ).fetchone()["n"]
    trace_n = conn.execute(
        "SELECT COUNT(*) n FROM memories WHERE status='active' "
        "AND src_sentence_map IS NOT NULL AND src_sentence_map != ''"
    ).fetchone()["n"]

    frag_n = conn.execute(
        "SELECT COUNT(*) n FROM memories WHERE status='active' AND is_fragment=1").fetchone()["n"]
    her_n = conn.execute(
        "SELECT COUNT(*) n FROM memories WHERE status='active' AND about_her=1").fetchone()["n"]
    found_n = conn.execute(
        "SELECT COUNT(*) n FROM memories WHERE status='active' AND is_foundation=1").fetchone()["n"]

    top_used = conn.execute(
        "SELECT * FROM memories WHERE status='active' AND use_count > 0 "
        "ORDER BY use_count DESC, last_used_at DESC LIMIT 10"
    ).fetchall()
    never_used = conn.execute(
        "SELECT COUNT(*) n FROM memories WHERE status='active' AND use_count=0"
    ).fetchone()["n"]

    return {
        "kind": _with_pct([{"label": label, "n": kind.get(k, 0)} for k, label in store.KIND_LABELS.items()]),
        "status": _with_pct([
            {"label": label, "n": status.get(k, 0)}
            for k, label in [("active", "在册"), ("superseded", "已被覆盖"),
                             ("retracted", "已否决"), ("memento", "纪念品"), ("disputed", "存疑")]
        ]),
        "author": _with_pct([
            {"label": label, "n": author.get(k, 0)}
            for k, label in [("nightly", "夜班蒸馏"), ("assistant", "他自己写的"), ("user", "她手动加的")]
        ]),
        "trigger": _with_pct([
            {"label": label, "n": trig.get(k, 0)} for k, label in store.TRIGGER_TYPES.items()
        ]),
        "stance": _with_pct([
            {"label": label, "n": stance.get(k, 0)} for k, label in STANCE_LABELS.items()
        ]),
        "active_n": active_n,
        "vec_n": vec_n, "vec_pct": round(vec_n / active_n * 100) if active_n else 0,
        "src_n": src_n, "src_pct": round(src_n / active_n * 100) if active_n else 0,
        "trace_n": trace_n, "trace_pct": round(trace_n / active_n * 100) if active_n else 0,
        "frag_n": frag_n, "her_n": her_n, "found_n": found_n,
        "top_used": top_used,
        "never_used": never_used,
        "cost": _cost_trend(conn),
    }

def _cost_trend(conn, days=30):
    from datetime import datetime, timedelta

    day_list = [(datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(days - 1, -1, -1)]
    try:
        rows = conn.execute(
            "SELECT substr(occurred_at,1,10) d, COALESCE(SUM(cost_usd),0) usd, "
            "SUM(input_tokens+output_tokens+cache_read+cache_write) tok, COUNT(*) c "
            "FROM turn_usage WHERE substr(occurred_at,1,10) >= ? GROUP BY d",
            (day_list[0],),
        ).fetchall()
    except Exception:
        return None
    by_day = {r["d"]: r for r in rows}
    series = [{"d": d,
               "usd": round(by_day[d]["usd"], 4) if d in by_day else 0.0,
               "tok": int(by_day[d]["tok"] or 0) if d in by_day else 0,
               "n": by_day[d]["c"] if d in by_day else 0} for d in day_list]
    mx = max([s["tok"] for s in series], default=0) or 1
    for s in series:
        s["pct"] = round(s["tok"] / mx * 100, 1)
    return {
        "series": series,
        "days": days,
        "total_usd": round(sum(s["usd"] for s in series), 4),
        "total_turns": sum(s["n"] for s in series),
        "has_data": any(s["n"] for s in series),
    }

def _src_text(conn, row):
    if row["src_conversation_id"]:
        conv = conn.execute(
            "SELECT title, source_end FROM conversations WHERE id=?", (row["src_conversation_id"],)
        ).fetchone()
        rng = ""
        if row["src_msg_start"]:
            rng = f" 消息 #{row['src_msg_start']}~#{row['src_msg_end'] or row['src_msg_start']}"
        return f"出处：{conv['source_end']} · {conv['title'] or '未命名会话'}{rng}"
    if row["src_quote"]:
        return f"原话锚点：“{row['src_quote']}”"
    return "（无溯源指针）"

SLOT_BUDGET = {
    resident_index.SLOT_HER_WORDS: resident_index.HER_WORDS_BUDGET,
    resident_index.SLOT_PENDING: resident_index.PENDING_BUDGET,
}

SLOT_BLOCK = {
    resident_index.SLOT_HER_WORDS: resident_index.her_words,
    resident_index.SLOT_PENDING: resident_index.pending_things,
}

EVIDENCE_HEAD = 70

def _decided(conn, *tag_prefixes):
    out = []
    for p in tag_prefixes:
        out += [r["detail"] for r in conn.execute(
            "SELECT detail FROM review_events WHERE detail LIKE ?", (p + "%",)).fetchall()]
    return out

def _one_liner_evidence(conn, name):
    row = conn.execute("SELECT id, COALESCE(one_liner,'') AS cur FROM entities WHERE name=?",
                       (name,)).fetchone()
    if row is None:
        return {"entity_id": None, "n_cards": None, "first_card": "", "current": ""}
    where = ("FROM memory_entities me JOIN memories m ON m.id=me.memory_id "
             "WHERE me.entity_id=? AND m.status='active' AND m.target_memory_id IS NULL")
    n = conn.execute(f"SELECT COUNT(*) n {where}", (row["id"],)).fetchone()["n"]
    first = conn.execute(f"SELECT m.content c {where} ORDER BY m.id LIMIT 1",
                         (row["id"],)).fetchone()
    head = (first["c"] or "") if first else ""
    if len(head) > EVIDENCE_HEAD:
        head = head[:EVIDENCE_HEAD] + "…"
    return {"entity_id": row["id"], "n_cards": n, "first_card": head, "current": row["cur"]}

def pending_one_liners(conn):
    已决 = {nightly.parse_one_liner_event(d)[1] for d in _decided(
        conn,
        nightly.ONE_LINER_APPROVED_TAG + nightly.ONE_LINER_SEP,
        nightly.ONE_LINER_REJECTED_TAG + nightly.ONE_LINER_SEP)
        if nightly.parse_one_liner_event(d)}
    rows = conn.execute(
        "SELECT id, detail, created_at FROM review_events WHERE type='alert' AND detail LIKE ? "
        "ORDER BY id",
        (nightly.ONE_LINER_REVIEW_TAG + nightly.ONE_LINER_SEP + "%",)).fetchall()
    出 = {}
    for r in rows:
        p = nightly.parse_one_liner_event(r["detail"])
        if not p or p[0] != nightly.ONE_LINER_REVIEW_TAG or p[1] in 已决:
            continue
        _, 名, 句, 说明 = p
        出[名] = {"event_id": r["id"], "name": 名, "sentence": 句, "note": 说明,
                  "created_at": _her_time(r["created_at"]), **_one_liner_evidence(conn, 名)}
    return sorted(出.values(), key=lambda d: d["event_id"])

def _slot_state(conn, slot):
    budget = SLOT_BUDGET[slot]
    used = len(SLOT_BLOCK[slot](conn))
    rows = conn.execute(
        "SELECT id, text, status FROM resident_notes WHERE slot=? ORDER BY ord, id",
        (slot,)).fetchall()
    return {
        "slot": slot, "budget": budget, "used": used, "over": max(0, used - budget),
        "active": [{"id": r["id"], "text": r["text"], "len": len(r["text"])}
                   for r in rows if r["status"] == "active"],
        "retired": [{"id": r["id"], "text": r["text"]} for r in rows if r["status"] != "active"],
    }

def resident_slots(conn):
    return [_slot_state(conn, s) for s in resident_index.SLOTS]

def pending_resident_notes(conn, slots=None):
    已决 = {(p[1], p[2]) for p in (nightly.parse_resident_note_event(d) for d in _decided(
        conn,
        nightly.RESIDENT_NOTE_APPROVED_TAG + nightly.ONE_LINER_SEP,
        nightly.RESIDENT_NOTE_REJECTED_TAG + nightly.ONE_LINER_SEP)) if p}
    状态 = {s["slot"]: s for s in (slots if slots is not None else resident_slots(conn))}
    rows = conn.execute(
        "SELECT id, detail, created_at FROM review_events WHERE type='alert' AND detail LIKE ? "
        "ORDER BY id",
        (nightly.RESIDENT_NOTE_REVIEW_TAG + nightly.ONE_LINER_SEP + "%",)).fetchall()
    出 = {}
    for r in rows:
        p = nightly.parse_resident_note_event(r["detail"])
        if not p or p[0] != nightly.RESIDENT_NOTE_REVIEW_TAG or (p[1], p[2]) in 已决:
            continue
        _, 格, 正文, 说明 = p
        s = 状态.get(格)
        出[(格, 正文)] = {
            "event_id": r["id"], "slot": 格, "text": 正文, "note": 说明,
            "created_at": _her_time(r["created_at"]),
            "known_slot": s is not None,
            "budget": s["budget"] if s else None,
            "used": s["used"] if s else None,
            "over": s["over"] if s else None,
            "after": (s["used"] + len(正文) + 3) if s else None,
            "active": (s["active"] if s else []),
        }
    return sorted(出.values(), key=lambda d: d["event_id"])

def _tab_today(conn, cfg, ctx):
    ctx["昨晚出事"] = nightly.昨晚出了什么事(conn)
    ctx["note_slots"] = resident_slots(conn)
    ctx["pending_one_liners"] = pending_one_liners(conn)
    ctx["pending_resident"] = pending_resident_notes(conn, ctx["note_slots"])

    findings = diagnose.run_all(conn, cfg)
    ctx["findings_bad"] = [f.as_dict() for f in findings if not f.ok]
    ctx["findings_ok"] = [f.as_dict() for f in findings if f.ok]

    try:
        ctx["sources"] = foraging.diagnose(conn, cfg)
        ctx["unregistered"] = _unregistered_sources(conn)
    except Exception as e:
        ctx["sources"] = {"error": str(e)}
        ctx["unregistered"] = []

def _suggest_match(raw_source):
    text = (raw_source or "").strip()
    if not text:
        return ""
    m = urlparse(text if "//" in text else "//" + text)
    host = (m.netloc or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host or text

def _unregistered_sources(conn):
    rows = conn.execute(
        "SELECT raw_source, COUNT(*) n, MAX(created_at) last FROM note_sources "
        "WHERE verdict='unregistered' GROUP BY raw_source ORDER BY n DESC, last DESC LIMIT 30"
    ).fetchall()
    grouped = {}
    for r in rows:
        key = _suggest_match(r["raw_source"])
        g = grouped.setdefault(key, {"match": key, "n": 0, "samples": []})
        g["n"] += r["n"]
        if len(g["samples"]) < 3:
            g["samples"].append(r["raw_source"])
    return sorted(grouped.values(), key=lambda g: -g["n"])

def _tab_log(conn, ctx):
    ev_filter = request.args.get("ev", "all")
    cond = {
        "new": "AND type='new_memory'",
        "alert": "AND type='alert'",
        "retract": "AND type='retract'",
        "edit": "AND type='edit' AND detail NOT LIKE '夜班情绪回填%'",
        "valence": "AND type='edit' AND detail LIKE '夜班情绪回填%'",
    }.get(ev_filter, "")
    ctx["ev_filter"] = ev_filter
    fv = _field_visibility(conn)
    events = conn.execute(
        "SELECT * FROM review_events WHERE 1=1 "
        f"{cond} ORDER BY id DESC LIMIT 200"
    ).fetchall()
    ctx["events"] = [
        {"event": e,
         "memory": _memory_view(conn, m, fv, with_stances=False)
                   if e["memory_id"] and (m := conn.execute(
                       "SELECT * FROM memories WHERE id=?", (e["memory_id"],)).fetchone()) else None}
        for e in events
    ]

def _tab_inject(conn, cfg, ctx):
    ctx["resident"] = resident_index.body(conn)
    fv = _field_visibility(conn)
    ctx["fv_rows"] = sorted(
        [{"col": c, "label": FIELD_LABELS.get(c, c), **v} for c, v in fv.items()],
        key=lambda r: (not r["visible"], r["col"]),
    )
    ctx["fv_visible_n"] = sum(1 for r in ctx["fv_rows"] if r["visible"])
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
    ctx["fv_missing"] = [c for c in cols if c not in fv]

    ctx["foundation"] = [
        _memory_view(conn, r, fv, with_stances=True)
        for r in conn.execute(
            "SELECT * FROM memories WHERE is_foundation=1 AND status='active' "
            "ORDER BY foundation_category, id"
        ).fetchall()
    ]
    ctx["foundation_quota"] = 35
    try:
        ctx["handover"] = conn.execute(
            "SELECT * FROM handover_marks ORDER BY created_at DESC LIMIT 50").fetchall()
    except Exception:
        ctx["handover"] = []
    try:
        ctx["turn_ids"] = {
            src: handles.current_turn(conn, src) for src in handles.SOURCES
        }
        有的 = [t for t in ctx["turn_ids"].values() if t is not None]
        ctx["turn_id"] = max(有的) if 有的 else None
        ctx["slots"] = conn.execute(
            "SELECT h.turn_id, h.slot, h.memory_id, h.created_at, h.source, m.content "
            "FROM turn_handles h LEFT JOIN memories m ON m.id = h.memory_id "
            "ORDER BY h.turn_id DESC, h.slot LIMIT 60").fetchall()
    except Exception:
        ctx["turn_id"], ctx["turn_ids"], ctx["slots"] = None, {}, []

    ctx["core_rows"] = [
        _memory_view(conn, r, fv, with_stances=False)
        for r in conn.execute(
            "SELECT * FROM memories WHERE is_core=1 AND status='active' ORDER BY id DESC").fetchall()
    ]
    ctx["core_md"] = core_card.ensure_active(conn, cfg)
    active_ver = core_card.get_active(conn)
    ctx["core_generated_at"] = active_ver["generated_at"] if active_ver else None
    ctx["core_retired_at"] = True

def _tab_him(conn, ctx):
    fv = _field_visibility(conn)
    ctx["notes"] = [
        _memory_view(conn, r, fv, with_stances=False)
        for r in conn.execute(
            "SELECT * FROM memories WHERE kind='note' AND status='active' "
            "ORDER BY id DESC LIMIT 200").fetchall()
    ]
    ctx["his_picks"] = [
        _memory_view(conn, r, fv, with_stances=False)
        for r in conn.execute(
            "SELECT * FROM memories WHERE is_foundation=1 AND author='assistant' "
            "AND status='active' ORDER BY id DESC LIMIT 100").fetchall()
    ]
    try:
        ctx["doings"] = conn.execute(
            "SELECT * FROM tool_calls ORDER BY id DESC LIMIT 50").fetchall()
    except Exception:
        ctx["doings"] = []
    try:
        ctx["wishlist"] = conn.execute(
            "SELECT * FROM reading_wishlist ORDER BY id DESC LIMIT 50").fetchall()
    except Exception:
        ctx["wishlist"] = []

def _tab_all(conn, cfg, ctx, q):
    fv = _field_visibility(conn)
    view = request.args.get("view", "")
    status = request.args.get("status", "active")
    ctx["view"] = view
    ctx["status"] = status
    if q:
        results, warnings = search.recall(conn, cfg, q, limit=40, touch=False)
        ctx["warnings"] += warnings
        rows = [r["row"] for r in results]
    else:
        where = "status=?"
        args = [status]
        if view == "her":
            where += " AND about_her=1"
        elif view == "fragment":
            where += " AND is_fragment=1"
        elif view == "disputed":
            where = "status='disputed'"
            args = []
        rows = conn.execute(
            f"SELECT * FROM memories WHERE {where} ORDER BY id DESC LIMIT 200", args).fetchall()
    ctx["memories"] = [_memory_view(conn, r, fv) for r in rows]
    ctx["her_n"] = conn.execute(
        "SELECT COUNT(*) n FROM memories WHERE about_her=1 AND status='active'").fetchone()["n"]

@app.route("/")
def index():
    cfg = _cfg()
    tab = request.args.get("tab", "today")
    if tab not in dict(TABS):
        tab = "today"
    q = request.args.get("q", "").strip()
    conn = get_conn()
    try:
        ctx = {
            "tab": tab,
            "tabs": TABS,
            "q": q,
            "stats": {
                "messages": conn.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"],
                "memories": conn.execute("SELECT COUNT(*) n FROM memories WHERE status='active'").fetchone()["n"],
                "core": conn.execute("SELECT COUNT(*) n FROM memories WHERE is_core=1 AND status='active'").fetchone()["n"],
                "core_quota": cfg["core_card"]["core_quota"],
                "unseen_alerts": conn.execute("SELECT COUNT(*) n FROM review_events WHERE type='alert' AND seen=0").fetchone()["n"],
            },
            "embedding_on": embeddings.is_configured(cfg),
            "warnings": [],
            "events": [],
            "memories": [],
            "core_md": None,
            "core_retired_at": None,
        }
        if tab == "today":
            _tab_today(conn, cfg, ctx)
        elif tab == "log":
            _tab_log(conn, ctx)
        elif tab == "inject":
            _tab_inject(conn, cfg, ctx)
        elif tab == "him":
            _tab_him(conn, ctx)
        elif tab == "all":
            _tab_all(conn, cfg, ctx, q)
        elif tab == "dash":
            ctx["dash"] = _dashboard(conn)
        ctx["stats"]["todo"] = (len(ctx.get("findings_bad", []))
                                + len(ctx.get("pending_one_liners", []))
                                + len(ctx.get("pending_resident", []))) if tab == "today" else None
        return render_template("index.html", **ctx)
    finally:
        conn.close()

@app.post("/memory/<int:mid>/retract")
def retract(mid):
    conn = get_conn()
    try:
        store.retract_memory(conn, mid, reason=request.form.get("reason", "质检台否决"))
        conn.commit()
    finally:
        conn.close()
    return redirect(request.referrer or url_for("index"))

@app.post("/memory/<int:mid>/core")
def toggle_core(mid):
    conn = get_conn()
    try:
        row = conn.execute("SELECT is_core FROM memories WHERE id=?", (mid,)).fetchone()
        try:
            store.set_core(conn, _cfg(), mid, not row["is_core"])
            conn.commit()
        except ValueError as e:
            conn.rollback()
            return f"<p>{e}</p><a href='javascript:history.back()'>返回</a>", 400
    finally:
        conn.close()
    return redirect(request.referrer or url_for("index"))

@app.post("/memory/<int:mid>/about_her")
def toggle_about_her(mid):
    conn = get_conn()
    try:
        row = conn.execute("SELECT about_her FROM memories WHERE id=?", (mid,)).fetchone()
        if row is None:
            return _reject(f"没有 #{mid} 这张卡")
        new = 0 if row["about_her"] else 1
        conn.execute("UPDATE memories SET about_her=? WHERE id=?", (new, mid))
        store.add_review_event(
            conn, "edit", mid,
            f"质检台标记：#{mid} {'标为' if new else '取消'}「关于她」（人工 pick）")
        conn.commit()
    finally:
        conn.close()
    return redirect(request.referrer or url_for("index"))

MSG_MARK = "\n#### "

UNNAMED_WINDOW = "未标注窗口"

def _export_windows(conn):
    rows = conn.execute(
        "SELECT m.*, c.window_name, c.title, c.source_end "
        "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        "ORDER BY m.id"
    ).fetchall()
    buckets = {}
    for r in rows:
        buckets.setdefault(r["window_name"] or UNNAMED_WINDOW, []).append(r)
    return sorted(buckets.items())

def _her_time(utc_text):
    offset = int(load_config().get("local_utc_offset_hours", 8))
    return to_local(utc_text, offset)

def _export_md(window, rows):
    first = _her_time(rows[0]["created_at"])[:10] if rows else ""
    last = _her_time(rows[-1]["created_at"])[:10] if rows else ""
    head = [
        f"# {window}",
        "",
        f"共 {len(rows)} 条消息 · {first} ~ {last}",
        "",
        "> 这是从记忆库账本原样导出的对话原文。**账本不可变，所以这里的每一个字都是当时说的。**",
        "> 整理出来的记忆卡不在这份文件里——那是我们的笔记，这份是原话。",
        "",
        "---",
    ]
    body = []
    for r in rows:
        who = "她" if r["role"] == "user" else "他"
        when = _her_time(r["created_at"])
        tail = ""
        if r["model"]:
            tail = f"（{r['model']}{' · ' + r['effort'] if r['effort'] else ''}）"
        body.append(f"{MSG_MARK}{who} · {when}{tail}")
        body.append("")
        body.append(r["content"] or "")
        if r["thinking"]:
            body.append("")
            body.append("<details><summary>他当时的思考</summary>")
            body.append("")
            body.append(r["thinking"])
            body.append("")
            body.append("</details>")
        body.append("")
    return "\n".join(head) + "\n".join(body)

def _count_in_md(text):
    return text.count(MSG_MARK)

class ExportMismatch(RuntimeError):
    pass

def _build_export(conn):
    windows = _export_windows(conn)
    总数 = conn.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
    files, 累计 = {}, 0

    for window, rows in windows:
        md = _export_md(window, rows)
        数出来 = _count_in_md(md)
        账本的 = conn.execute(
            "SELECT COUNT(*) n FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "WHERE COALESCE(c.window_name, ?) = ?", (UNNAMED_WINDOW, window)
        ).fetchone()["n"]
        if 数出来 != 账本的:
            raise ExportMismatch(
                f"窗口「{window}」导出了 {数出来} 条，账本里是 {账本的} 条 —— 差 {账本的 - 数出来} 条。\n"
                f"🔴 **没有把这份不完整的文件给你** —— 一份「看起来完整」的导出比没有导出更坏：\n"
                f"   你会以为手上有全部，而缺口要到再也回不去的那天才发现。\n"
                f"   （账本不可变，原文一条没丢，是导出这一步漏了。把这句报给维护者。）"
            )
        files[f"对话/{_safe_name(window)}.md"] = md
        累计 += 数出来

    if 累计 != 总数:
        raise ExportMismatch(
            f"全部窗口加起来 {累计} 条，而账本里一共 {总数} 条 —— 差 {总数 - 累计} 条。\n"
            f"🔴 **整批没给你**。逐个窗口都对得上而总数对不上，说明**有一整批消息没被归进任何窗口**。"
        )

    files["记忆库导出.json"] = _export_json(conn, windows, 总数)
    files["先读我.md"] = _export_readme(总数, len(windows))
    return files

def _safe_name(s):
    out = "".join("_" if ch in '/\\:*?"<>|' else ch for ch in (s or "").strip())
    return out or "未命名"

def _export_json(conn, windows, 总数):
    import json

    data = {
        "导出于": now_iso(),
        "消息总数": 总数,
        "说明": "账本原文全量。memories 是我们整理出来的记忆卡，跟原文分开放。",
        "时间口径": {
            "消息.created_at": "UTC（账本原样，未换算）",
            "她的时区偏移小时": int(load_config().get("local_utc_offset_hours", 8)),
            "怎么换算": "UTC + 偏移 = 她那边的当地时间。对话/*.md 里已经换算过了，这份没有。",
            "记忆卡.created_at": "🔴 不是同一条时间轴：它是【机器时间】（写库那台机器的本地时区），"
                                 "不是 UTC。两列同名同格式，别拿来互相比较或算天数。",
        },
        "窗口": [
            {
                "窗口名": window,
                "消息": [
                    {k: r[k] for k in
                     ("id", "conversation_id", "role", "content", "created_at",
                      "thinking", "model", "effort")}
                    for r in rows
                ],
            }
            for window, rows in windows
        ],
        "记忆卡": [
            dict(r) for r in conn.execute(
                "SELECT * FROM memories ORDER BY id").fetchall()
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)

def _export_readme(总数, 窗口数):
    return (
        "# 这份文件里有什么\n\n"
        f"从记忆库导出的全部原文：**{总数} 条消息，分在 {窗口数} 个窗口里**。\n\n"
        "· `对话/*.md` —— **你直接点开就能读**，一个窗口一份，按时间排。\n"
        "· `记忆库导出.json` —— 给程序读的那一份，带上了每条消息的编号和整理出来的记忆卡。\n\n"
        "## 两件事说清楚\n\n"
        "**这不是备份。** 备份是给机器用的数据库文件，你打不开；这份是给你的，"
        "换个电脑、换个软件，甚至哪天不用这套系统了，这些字还在。\n\n"
        "**条数是核对过的。** 每个窗口导出的条数都跟账本里对过一遍，"
        "对不上就整批不给你——**一份看起来完整的导出，比没有导出更坏**。\n"
    )

@app.post("/export")
def export_all():
    import io
    import zipfile

    conn = get_conn()
    try:
        files = _build_export(conn)
    except ExportMismatch as e:
        return _reject(e, title="这份导出没给你，因为它不完整")
    finally:
        conn.close()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    buf.seek(0)
    from flask import send_file
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"记忆库导出-{now_iso()[:10]}.zip")

@app.post("/source/register")
def register_source():
    match = (request.form.get("match") or "").strip()
    raw = request.form.get("breaks_circle")
    if raw not in ("0", "1"):
        return _reject(
            "没说这个来源算哪一档。\n"
            "🔴 这一格不许留空：判不出来就**别登记这一条**，\n"
            "  让它留在「没登记」那一档去显形。\n"
            "  默认算能打破圈 ＝ 闭环预警永不响；默认算同温层 ＝ 天天误报，最后被你关掉。"
        )
    breaks = raw == "1"
    cfg = _cfg()
    try:
        foraging.register_source(match, breaks)
    except ValueError as e:
        return _reject(e)

    cfg = load_config()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT memory_id, raw_source FROM note_sources WHERE verdict='unregistered'"
        ).fetchall()
        改了 = 0
        for r in rows:
            if foraging.record_source(conn, r["memory_id"], r["raw_source"], cfg) != "unregistered":
                改了 += 1
        store.add_review_event(
            conn, "edit", None,
            f"质检台登记来源：{match} → "
            f"{'能打破圈' if breaks else '同温层'}；重判存量 {改了}/{len(rows)} 条")
        conn.commit()
    finally:
        conn.close()
    return redirect(request.referrer or url_for("index", tab="today"))

@app.post("/memory/<int:mid>/supersede")
def supersede(mid):
    content = request.form.get("content", "").strip()
    if not content:
        return redirect(request.referrer or url_for("index"))
    changes = request.form.getlist("change")
    if not changes:
        return _reject(
            "没勾「改说法／改事实」。\n"
            "🔴 这一格不许留空：\n"
            "  · 改说法 → 新卡会显示旧卡的表态（同一件事，换了个说法）\n"
            "  · 改事实 → 不显示（表态的对象不存在了）\n"
            "两种都勾也可以。判不出来就先别改这张卡。"
        )
    fact_changed = "fact" in changes
    conn = get_conn()
    try:
        old = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
        new_id = store.add_memory(
            conn, _cfg(), content,
            kind=request.form.get("kind") or old["kind"],
            importance=int(request.form.get("importance") or old["importance"]),
            occurred_at=request.form.get("occurred_at") or old["occurred_at"],
            src_quote=request.form.get("src_quote") or old["src_quote"],
            author="user",
            supersedes=mid,
            src_conversation_id=_int_or_none(request.form.get("src_conversation_id")) or old["src_conversation_id"],
            src_msg_start=_int_or_none(request.form.get("src_msg_start")) or old["src_msg_start"],
            src_msg_end=_int_or_none(request.form.get("src_msg_end")) or old["src_msg_end"],
            src_sentence_map=_sentence_map_from_form(request.form.get("src_sentence_map")),
            entities=[e.strip() for e in request.form.get("entities", "").replace("，", ",").split(",") if e.strip()]
                     or store.memory_entities_names(conn, mid),
            fact_changed=fact_changed,
            wording_changed="wording" in changes,
        )
        marks = "".join(m for m, on in
                        [(WORDING_CHANGED_MARK, "wording" in changes), (FACT_CHANGED_MARK, fact_changed)] if on)
        store.add_review_event(
            conn, "edit", new_id,
            f"质检台修正{marks}：新卡 #{new_id} 覆盖旧卡 #{mid}"
            + ("　（表态不跟过来：表态的对象不存在了）" if fact_changed else "　（旧卡表态跟过来）"))
        conn.commit()
    except ValueError as e:
        return _reject(e)
    finally:
        conn.close()
    return redirect(request.referrer or url_for("index"))

@app.post("/memory/<int:mid>/valence")
def set_valence(mid):
    conn = get_conn()
    try:
        if request.form.get("clear"):
            conn.execute("UPDATE memories SET valence=NULL, arousal=NULL WHERE id=?", (mid,))
            store.add_review_event(conn, "edit", mid, f"质检台人工改情绪：#{mid} 清空待回填")
        else:
            try:
                v = round(max(-1.0, min(1.0, float(request.form.get("valence")))), 2)
                a = round(max(0.0, min(1.0, float(request.form.get("arousal")))), 2)
            except (TypeError, ValueError):
                return redirect(request.referrer or url_for("index"))
            conn.execute("UPDATE memories SET valence=?, arousal=? WHERE id=?", (v, a, mid))
            store.add_review_event(conn, "edit", mid, f"质检台人工改情绪：#{mid} v={v} a={a}")
        conn.commit()
    finally:
        conn.close()
    return redirect(request.referrer or url_for("index"))

def _reject(err, title="这张卡没能入库"):
    from markupsafe import escape

    return (
        "<meta charset='utf-8'><div style=\"font:15px/1.8 -apple-system,sans-serif;"
        "max-width:640px;margin:60px auto;padding:0 20px\">"
        f"<h3>{escape(title)}</h3><pre style=\"white-space:pre-wrap;background:#f5f3ef;"
        "padding:12px;border-radius:8px\">" + str(err) + "</pre>"
        "<p><a href='javascript:history.back()'>← 返回修改</a></p></div>",
        400,
    )

def _sentence_map_from_form(text):
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        sent, _, ids = line.rpartition("<-")
        if not sent.strip():
            return None
        try:
            msg_ids = [int(x) for x in ids.replace("，", ",").split(",") if x.strip()]
        except ValueError:
            return None
        rows.append({"sent": sent.strip(), "msg_ids": msg_ids})
    return rows or None

def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

@app.post("/add")
def add_manual():
    content = request.form.get("content", "").strip()
    if content:
        conn = get_conn()
        try:
            store.add_memory(
                conn, _cfg(), content,
                kind=request.form.get("kind", "fact"),
                importance=int(request.form.get("importance") or 3),
                occurred_at=request.form.get("occurred_at", ""),
                src_quote=request.form.get("src_quote", ""),
                author="user",
                entities=[e.strip() for e in request.form.get("entities", "").replace("，", ",").split(",") if e.strip()],
                src_conversation_id=_int_or_none(request.form.get("src_conversation_id")),
                src_msg_start=_int_or_none(request.form.get("src_msg_start")),
                src_msg_end=_int_or_none(request.form.get("src_msg_end")),
                src_sentence_map=_sentence_map_from_form(request.form.get("src_sentence_map")),
            )
            conn.commit()
        except ValueError as e:
            return _reject(e)
        finally:
            conn.close()
    return redirect(url_for("index", tab="all"))

def _proposal(conn, eid, want_tag, parser):
    row = conn.execute("SELECT detail FROM review_events WHERE id=?", (eid,)).fetchone()
    if row is None:
        return None
    p = parser(row["detail"])
    if not p or p[0] != want_tag:
        return None
    return p

def _mark_seen(conn, eid):
    conn.execute("UPDATE review_events SET seen=1 WHERE id=?", (eid,))

@app.post("/one_liner/<int:eid>/approve")
def one_liner_approve(eid):
    with write_session(quiet=True) as conn:
        p = _proposal(conn, eid, nightly.ONE_LINER_REVIEW_TAG, nightly.parse_one_liner_event)
        if p is None:
            return _reject(f"#{eid} 不是一条待审的实体一句话提议。", title="这一下没生效")
        _, 名, 句, _说明 = p
        row = conn.execute("SELECT id, COALESCE(one_liner,'') AS cur FROM entities WHERE name=?",
                           (名,)).fetchone()
        if row is None:
            return _reject(f"库里没有「{名}」这个词了（改过名或清过库）。"
                           f"⇒ 这一条无处可写，请点【否决】把它了结掉。", title="这一下没生效")
        if row["cur"].strip() and row["cur"].strip() != 句.strip():
            return _reject(
                f"「{名}」库里已经有一句了：\n\n　{row['cur']}\n\n"
                f"而这条提议是：\n\n　{句}\n\n"
                "🔴 **没有替你选**——两句话都可能是对的，而覆盖是收不回来的。\n"
                "要换成新的这句，先在别处把旧的清掉；要留旧的，点【否决】。",
                title="这一下没生效")
        conn.execute("UPDATE entities SET one_liner=? WHERE id=?", (句, row["id"]))
        store.add_review_event(conn, "edit", None, nightly.one_liner_event_detail(
            nightly.ONE_LINER_APPROVED_TAG, 名, 句, f"质检台：她点了通过（提议 #{eid}）"))
        _mark_seen(conn, eid)
    return redirect(request.referrer or url_for("index"))

@app.post("/one_liner/<int:eid>/reject")
def one_liner_reject(eid):
    with write_session(quiet=True) as conn:
        p = _proposal(conn, eid, nightly.ONE_LINER_REVIEW_TAG, nightly.parse_one_liner_event)
        if p is None:
            return _reject(f"#{eid} 不是一条待审的实体一句话提议。", title="这一下没生效")
        _, 名, 句, _说明 = p
        store.add_review_event(conn, "retract", None, nightly.one_liner_event_detail(
            nightly.ONE_LINER_REJECTED_TAG, 名, 句, f"质检台：她点了否决（提议 #{eid}）"))
        _mark_seen(conn, eid)
    return redirect(request.referrer or url_for("index"))

@app.post("/resident_note/proposal/<int:eid>/approve")
def resident_note_approve(eid):
    with write_session(quiet=True) as conn:
        p = _proposal(conn, eid, nightly.RESIDENT_NOTE_REVIEW_TAG,
                      nightly.parse_resident_note_event)
        if p is None:
            return _reject(f"#{eid} 不是一条待审的常驻层条目提议。", title="这一下没生效")
        _, 格, 正文, _说明 = p
        if 格 not in resident_index.SLOTS:
            return _reject(f"「{格}」不是常驻层的格名（只有 {'、'.join(resident_index.SLOTS)}）。"
                           "⇒ 不猜，这一条不入库。", title="这一下没生效")
        ord_ = (conn.execute("SELECT MAX(ord) m FROM resident_notes WHERE slot=?",
                             (格,)).fetchone()["m"] or 0) + 1
        conn.execute(
            "INSERT INTO resident_notes(slot, text, ord, status, created_at) "
            "VALUES(?,?,?,'active',?)", (格, 正文, ord_, now_iso()))
        store.add_review_event(conn, "edit", None, nightly.resident_note_event_detail(
            nightly.RESIDENT_NOTE_APPROVED_TAG, 格, 正文, f"质检台：她点了通过（提议 #{eid}）"))
        _mark_seen(conn, eid)
    return redirect(request.referrer or url_for("index"))

@app.post("/resident_note/proposal/<int:eid>/reject")
def resident_note_reject(eid):
    with write_session(quiet=True) as conn:
        p = _proposal(conn, eid, nightly.RESIDENT_NOTE_REVIEW_TAG,
                      nightly.parse_resident_note_event)
        if p is None:
            return _reject(f"#{eid} 不是一条待审的常驻层条目提议。", title="这一下没生效")
        _, 格, 正文, _说明 = p
        store.add_review_event(conn, "retract", None, nightly.resident_note_event_detail(
            nightly.RESIDENT_NOTE_REJECTED_TAG, 格, 正文, f"质检台：她点了否决（提议 #{eid}）"))
        _mark_seen(conn, eid)
    return redirect(request.referrer or url_for("index"))

@app.post("/resident_note/<int:nid>/retire")
def resident_note_retire(nid):
    with write_session(quiet=True) as conn:
        row = conn.execute("SELECT slot, text, status FROM resident_notes WHERE id=?",
                           (nid,)).fetchone()
        if row is None:
            return _reject(f"没有 #{nid} 这一条常驻层条目。", title="这一下没生效")
        if row["status"] != "active":
            return _reject(f"#{nid} 已经是「{row['status']}」了，没重复撤。", title="这一下没生效")
        conn.execute("UPDATE resident_notes SET status='retired' WHERE id=?", (nid,))
        store.add_review_event(conn, "retract", None, nightly.resident_note_event_detail(
            nightly.RESIDENT_NOTE_RETIRED_TAG, row["slot"], row["text"],
            f"质检台：她把 #{nid} 撤下了（不再进他的上下文；行还在，标了 retired）"))
    return redirect(request.referrer or url_for("index"))

@app.post("/event/<int:eid>/seen")
def mark_seen(eid):
    conn = get_conn()
    try:
        conn.execute("UPDATE review_events SET seen=1 WHERE id=?", (eid,))
        conn.commit()
    finally:
        conn.close()
    return redirect(request.referrer or url_for("index"))

@app.post("/seen_all")
def seen_all():
    conn = get_conn()
    try:
        conn.execute("UPDATE review_events SET seen=1 WHERE seen=0")
        conn.commit()
    finally:
        conn.close()
    return redirect(request.referrer or url_for("index"))

def main_cli():
    """命令行入口（`nacre-review`）。`python -m nacre.review_app` 走的是同一段。"""
    cfg = load_config()
    app.run(host=cfg["review_app"]["host"], port=cfg["review_app"]["port"], debug=False)


if __name__ == "__main__":
    main_cli()
