from .config import load_config
from .db import her_day_bounds, now_iso, on_machine_axis, on_utc_axis

CONTEXT_WALL = 279_550

SILENT_DAYS = {"note": 14, "stance": 14}

class Finding:

    def __init__(self, cls, name, ok, checked, detail=""):
        self.cls, self.name, self.ok, self.checked, self.detail = cls, name, ok, checked, detail

    def __repr__(self):
        mark = "✅" if self.ok else "🔴"
        return f"{mark} [{self.cls}] {self.name}（检查了 {self.checked} 条）{'：' + self.detail if self.detail else ''}"

    def as_dict(self):
        return {"cls": self.cls, "name": self.name, "ok": self.ok,
                "checked": self.checked, "detail": self.detail}

def check_a(conn, records=None, anchor_id=None, conversation_id=None):
    out = []
    if anchor_id is None:
        return [Finding("A", "近况层比对", True, 0, "没有锚点，这一窗还没开始打包")]

    sql = "SELECT id FROM messages WHERE id>=?"
    args = [anchor_id]
    if conversation_id is not None:
        sql += " AND conversation_id=?"
        args.append(conversation_id)
    账本的 = [r["id"] for r in conn.execute(sql + " ORDER BY id", args)]

    if records is None:
        out.append(Finding("A", "近况层比对", True, 0,
                           "没有传打包结果进来 —— **这一项这次没查**，不是通过了"))
    else:
        进请求的 = {d["_ledger_id"] for d in records if d.get("_ledger_id") is not None}
        应该在的 = set(账本的)
        while 账本的 and 账本的[-1] not in 进请求的:
            账本的.pop()
        应该在的 = set(账本的)
        缺的 = sorted(应该在的 - 进请求的)
        out.append(Finding(
            "A", "近况层逐条比对", not 缺的, len(应该在的),
            "" if not 缺的 else f"账本里有、请求里没有：{缺的[:5]}（共 {len(缺的)} 条）"))

    最大 = conn.execute(
        "SELECT MAX(id) AS m FROM messages" + (" WHERE conversation_id=?" if conversation_id else ""),
        (conversation_id,) if conversation_id else (),
    ).fetchone()["m"]
    if 最大 is not None and 账本的:
        落后 = 最大 - 账本的[-1]
        out.append(Finding(
            "A", "近况层跟得上账本", 落后 <= 1, 1,
            "" if 落后 <= 1 else f"账本已经到 {最大}，而近况层最后一条是 {账本的[-1]}（差 {落后} 条）"))
    return out

def check_b(conn, cfg=None):
    cfg = load_config() if cfg is None else cfg
    out = []

    起, 止 = her_day_bounds(cfg.get("local_utc_offset_hours", 8))
    今天说了几句 = conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE created_at >= ? AND created_at < ?",
        (on_utc_axis(起), on_utc_axis(止)),
    ).fetchone()["c"]
    今天几张卡 = conn.execute(
        "SELECT COUNT(*) c FROM memories WHERE created_at >= ? AND created_at < ?",
        (on_machine_axis(起), on_machine_axis(止)),
    ).fetchone()["c"]
    out.append(Finding(
        "B", "今天有对话就该有卡", not (今天说了几句 > 0 and 今天几张卡 == 0), 今天说了几句,
        "" if not (今天说了几句 > 0 and 今天几张卡 == 0)
        else f"今天说了 {今天说了几句} 句，却一张卡都没有"))

    缺锚的 = conn.execute(
        "SELECT COUNT(*) c FROM memories WHERE status='active' "
        "AND (src_quote IS NULL OR trim(src_quote)='')"
    ).fetchone()["c"]
    总卡数 = conn.execute("SELECT COUNT(*) c FROM memories WHERE status='active'").fetchone()["c"]
    out.append(Finding(
        "B", "缺原话锚的卡应当恒为 0", 缺锚的 == 0, 总卡数,
        "" if 缺锚的 == 0
        else f"🔴 有 {缺锚的} 张卡没有原话锚 —— **写卡闸拦得住这个，所以它不为 0 就说明有人绕过了 `add_memory`**"))

    try:
        近期自留地 = conn.execute(
            "SELECT trigger_type, COUNT(*) c FROM memories "
            "WHERE kind='note' AND status='active' GROUP BY trigger_type"
        ).fetchall()
    except Exception:
        近期自留地 = []
    总数 = sum(r["c"] for r in 近期自留地)
    外部的 = sum(r["c"] for r in 近期自留地 if r["trigger_type"] == "external")
    out.append(Finding(
        "B", "自留地不该全是他自己想的", 总数 == 0 or 外部的 > 0, 总数,
        "" if 总数 == 0 or 外部的 > 0
        else f"🟣 {总数} 条自留地笔记，**一条外部来源都没有** —— 闭环预警"))
    return out

def record_usage(conn, result, message_id=None, ok=True, source="chat"):
    u = (result or {}).get("usage") or {}
    conn.execute(
        "INSERT INTO turn_usage(message_id, occurred_at, model, ok, input_tokens, output_tokens, "
        "cache_read, cache_write, cost_usd, duration_ms, source, effort, roundtrips) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (message_id, now_iso(), (result or {}).get("model"), 1 if ok else 0,
         int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
         int(u.get("cache_read_input_tokens") or 0),
         int(u.get("cache_creation_input_tokens") or 0),
         (result or {}).get("total_cost_usd"),
         ((result or {}).get("raw") or {}).get("duration_ms"),
         source,
         (result or {}).get("effort"),
         (result or {}).get("num_turns")),
    )

def turn_context(row):
    return int(row["input_tokens"] or 0) + int(row["cache_read"] or 0) + int(row["cache_write"] or 0)

def _缓存重建那条(conn, cfg, 今天总轮数):
    今天 = now_iso()[:10]
    if not 今天总轮数:
        return Finding("C", "今天有几轮在重建缓存", True, 0,
                       "今天一轮都还没有 —— **这不是「正常」，是「没得查」**")
    重建的 = conn.execute(
        "SELECT COUNT(*) c FROM turn_usage "
        "WHERE cache_write > 0 AND substr(occurred_at,1,10)=?", (今天,)).fetchone()["c"]
    预期 = int((load_config() if cfg is None else cfg).get("v3", {}).get("cache_write_daily_expected", 2))
    return Finding(
        "C", "今天有几轮在重建缓存", 重建的 <= 预期, 今天总轮数,
        f"{重建的} / {今天总轮数} 轮写了缓存（预期每天 {预期} 轮以内〔推算·试行〕）"
        + ("" if 重建的 <= 预期 else
           "　🔴 **前缀可能在漂** —— 那意味着每轮都在为全部历史重新付费，"
           "而它【唯一的症状就是额度烧得快】。\n"
           "   去看两处："
           "① 常驻索引里是不是混进了随时间变的东西（时间戳／「还有 K 轮未蒸馏」这类计数）"
           "② 是不是有内容被插进了已有内容【之前】，而不是追加在末尾"))

def check_c(conn, spike_ratio=2.0, cfg=None):
    out = []
    rows = list(conn.execute(
        "SELECT * FROM turn_usage ORDER BY id DESC LIMIT 50"))
    out.append(Finding("C", "有没有在记用量", bool(rows), len(rows),
                       "" if rows else "一条用量都没记 —— 看板会是空的"))
    if not rows:
        out.append(_缓存重建那条(conn, cfg, 今天总轮数=0))
        return out

    最近 = rows[0]
    占用 = turn_context(最近)
    比例 = 占用 / CONTEXT_WALL
    out.append(Finding(
        "C", "离换窗还有多远", 比例 < 0.7, 1,
        f"这一轮占 {占用:,} / {CONTEXT_WALL:,}（{比例:.0%}）"
        + ("　🔴 过 70% 了，该换窗" if 比例 >= 0.7 else "")))

    if len(rows) >= 4:
        往前 = sorted(turn_context(r) for r in rows[1:11])
        中位 = 往前[len(往前) // 2]
        突增 = 中位 > 0 and 占用 > 中位 * spike_ratio
        out.append(Finding(
            "C", "有没有突增", not 突增, len(往前),
            "" if not 突增 else f"这一轮 {占用:,}，前几轮中位数才 {中位:,}（{占用/中位:.1f} 倍）"))

    今天 = now_iso()[:10]
    日累计 = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) s, COUNT(*) c FROM turn_usage "
        "WHERE substr(occurred_at,1,10)=?", (今天,)).fetchone()
    out.append(Finding("C", "今天花了多少", True, 日累计["c"],
                       f"${日累计['s']:.4f}（{日累计['c']} 轮）"))

    out.append(_缓存重建那条(conn, cfg, 日累计["c"]))

    失败的 = conn.execute(
        "SELECT COUNT(*) c FROM turn_usage WHERE ok=0 AND substr(occurred_at,1,10)=?",
        (今天,)).fetchone()["c"]
    率 = 失败的 / 日累计["c"] if 日累计["c"] else 0
    out.append(Finding(
        "C", "今天失败了几轮", 率 < 0.2, 日累计["c"],
        f"{失败的} / {日累计['c']} 轮失败"
        + ("　🔴 失败也是要计费的 —— 钱在烧，而她那边只看到「发不出去」" if 率 >= 0.2 else "")))
    return out

def check_d(conn, days=None):
    days = days or SILENT_DAYS
    out = []
    今天 = now_iso()[:10]

    def 多久没有了(sql, args=()):
        row = conn.execute(sql, args).fetchone()
        return (row["last"] if row else None), (row["c"] if row else 0)

    最后, 总数 = 多久没有了(
        "SELECT MAX(created_at) last, COUNT(*) c FROM memories "
        "WHERE kind='note' AND status='active'")
    if 总数 == 0:
        out.append(Finding("D", "自留地", True, 0,
                           "库里一条自留地笔记都没有 —— **这个功能还没接上，这是预期的**，"
                           "不是机制死了"))
    else:
        沉默 = _days_between(最后[:10], 今天)
        out.append(Finding("D", "多久没写自留地", 沉默 <= days["note"], 总数,
                           f"最后一条在 {最后[:10]}（{沉默} 天前）"))

    最后, 总数 = 多久没有了(
        "SELECT MAX(created_at) last, COUNT(*) c FROM memories "
        "WHERE stance IS NOT NULL AND status='active'")
    if 总数 == 0:
        out.append(Finding("D", "表态", True, 0,
                           "一条表态都没有 —— 表态工具刚上，**攒几天再看**"))
    else:
        沉默 = _days_between(最后[:10], 今天)
        out.append(Finding("D", "多久没表态", 沉默 <= days["stance"], 总数,
                           f"最后一条在 {最后[:10]}（{沉默} 天前）"))

    表态数 = conn.execute(
        "SELECT COUNT(*) c FROM memories WHERE stance IS NOT NULL AND status='active'"
    ).fetchone()["c"]
    异议数 = conn.execute(
        "SELECT COUNT(*) c FROM memories WHERE stance IN ('reject','suspend') AND status='active'"
    ).fetchone()["c"]
    out.append(Finding(
        "D", "有没有过不同意", 表态数 == 0 or 异议数 > 0, 表态数,
        "" if 表态数 == 0 or 异议数 > 0
        else f"🔴 {表态数} 条表态**全是认或批注，一条不认／悬置都没有** —— "
             "表态可能已经退化成盖章"))

    碎片 = conn.execute(
        "SELECT COUNT(*) c FROM memories WHERE is_fragment=1 AND status='active'"
    ).fetchone()["c"]
    总卡 = conn.execute("SELECT COUNT(*) c FROM memories WHERE status='active'").fetchone()["c"]
    out.append(Finding(
        "D", "碎片占比不该为零", 总卡 == 0 or 碎片 > 0, 总卡,
        "" if 总卡 == 0 or 碎片 > 0
        else "🔴 一张碎片都没有 —— **平淡的日子被整个跳过了**"))

    out.append(_排队本那条(conn))
    return out

def _排队本那条(conn):
    import os
    import sqlite3 as _sq

    from . import turn_queue as _tq
    try:
        db_file = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    except _sq.Error:
        db_file = ""
    if not db_file or not os.path.exists(_tq.queue_path(db_file)):
        return Finding("D", "排队本", True, 0, "还没有排队本（没跑过排队轮次）—— 预期")
    qc = _sq.connect(_tq.queue_path(db_file), timeout=10)
    try:
        rows = dict(qc.execute(
            "SELECT status, COUNT(*) FROM turn_queue "
            "WHERE status IN ('failed','orphaned') GROUP BY status").fetchall())
        总 = qc.execute("SELECT COUNT(*) FROM turn_queue").fetchone()[0]
    finally:
        qc.close()
    坏 = sum(rows.values())
    return Finding(
        "D", "排队本里没得到回应的轮次", 坏 == 0, 总,
        "" if 坏 == 0
        else f"🔴 failed {rows.get('failed', 0)} 条 · orphaned {rows.get('orphaned', 0)} 条 —— "
             "每一条都是她说了没人回的一句；orphaned **不许自动重跑**（账本可能已写入），"
             "去 `turn_queue.items()` 对着账本人工核")

def _days_between(d1, d2):
    from datetime import date
    try:
        a = date(*[int(x) for x in d1.split("-")])
        b = date(*[int(x) for x in d2.split("-")])
        return (b - a).days
    except Exception:
        return 0

def run_all(conn, cfg=None, records=None, anchor_id=None, conversation_id=None):
    return (check_a(conn, records=records, anchor_id=anchor_id, conversation_id=conversation_id)
            + check_b(conn, cfg) + check_c(conn, cfg=cfg) + check_d(conn))

def report(findings):
    坏的 = [f for f in findings if not f.ok]
    head = (f"诊断：{len(findings)} 项，{len(坏的)} 项要看一眼"
            if 坏的 else f"诊断：{len(findings)} 项全过")
    return head + "\n" + "\n".join("  " + repr(f) for f in findings)
