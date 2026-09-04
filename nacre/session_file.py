import json
import uuid as _uuid
from pathlib import Path

from .db import now_iso

RECENT_TURNS = 50

ANCHOR_KEY = "pack:anchor_msg_id"
SESSION_KEY = "pack:session_id"

MESSAGE_TYPES = ("user", "assistant")

TOOL_BODY_DEFAULT_CAP = 4000

TRUNCATED_MARK = "\n\n…〔这里被截断了：原文 {n} 字，只给了前 {k} 字。完整的一份在会话文件里，没有第二份〕"

class SessionFileError(Exception):
    pass

def opening_anchor(conn, limit_turns=RECENT_TURNS, conversation_id=None):
    if limit_turns <= 0:
        raise SessionFileError("近况层轮数必须为正，收到 %r" % (limit_turns,))
    sql = "SELECT id FROM messages"
    args = []
    if conversation_id is not None:
        sql += " WHERE conversation_id=?"
        args.append(conversation_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit_turns * 2)
    rows = list(conn.execute(sql, args))
    if not rows:
        return None
    return rows[-1]["id"]

def resolve_anchor(conn, limit_turns=RECENT_TURNS, conversation_id=None):
    row = conn.execute("SELECT value FROM watermarks WHERE key=?", (ANCHOR_KEY,)).fetchone()
    if row and str(row["value"]).strip():
        return int(row["value"]), False
    anchor = opening_anchor(conn, limit_turns=limit_turns, conversation_id=conversation_id)
    if anchor is None:
        return None, False
    conn.execute(
        "INSERT INTO watermarks(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (ANCHOR_KEY, str(anchor), now_iso()),
    )
    return anchor, True

def resolve_session_id(conn):
    row = conn.execute("SELECT value FROM watermarks WHERE key=?", (SESSION_KEY,)).fetchone()
    if row and str(row["value"]).strip():
        return str(row["value"]), False
    sid = str(_uuid.uuid4())
    conn.execute(
        "INSERT INTO watermarks(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (SESSION_KEY, sid, now_iso()),
    )
    return sid, True

def conversation_prefix(records):
    out = []
    for d in records:
        if d.get("type") not in MESSAGE_TYPES:
            continue
        content = (d.get("message") or {}).get("content")
        if isinstance(content, list):
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        out.append((d["type"], content))
    return out

def recent_messages(conn, anchor_id, conversation_id=None):
    if anchor_id is None:
        return []
    sql = "SELECT id, role, content, created_at FROM messages WHERE id>=?"
    args = [anchor_id]
    if conversation_id is not None:
        sql += " AND conversation_id=?"
        args.append(conversation_id)
    rows = list(conn.execute(sql + " ORDER BY id", args))

    while rows and rows[-1]["role"] != "assistant":
        rows.pop()
    return rows

def load_seed(seed_path):
    p = Path(seed_path)
    if not p.exists():
        raise SessionFileError(
            f"找不到种子会话文件：{seed_path}\n"
            "   种子是**在目标 cwd 里真跑一次 `claude -p` 生成的那一份**，不能手写、不能猜。\n"
            "   （少一个我们没见过的字段，症状可能是静默的）"
        )
    lines = []
    for n, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError as e:
            raise SessionFileError(f"种子会话文件第 {n} 行不是合法 JSON：{e}")
    if not lines:
        raise SessionFileError(f"种子会话文件是空的：{seed_path}")
    return lines

def _templates(lines):
    u = next((d for d in lines if d.get("type") == "user"), None)
    a = next((d for d in lines if d.get("type") == "assistant"), None)
    if u is None or a is None:
        raise SessionFileError(
            "种子会话文件里缺 user 或 assistant 记录 —— 它必须是**真跑过一来一回**的会话，"
            "只跑了一半的种子当不了模板"
        )
    return u, a

def _clone(d):
    return json.loads(json.dumps(d, ensure_ascii=False))

def build_records(seed_lines, messages, session_id=None, 工具块=None, 落单工具块=None):
    sid = session_id or str(_uuid.uuid4())
    lines = [_clone(d) for d in seed_lines]
    for d in lines:
        d["sessionId"] = sid

    u_tpl, a_tpl = _templates(lines)
    idx = [i for i, d in enumerate(lines) if d.get("type") in MESSAGE_TYPES]
    if not idx:
        raise SessionFileError("种子会话文件里一条消息都没有")
    last_idx = max(idx)
    tail = lines[last_idx]["uuid"]

    工具块 = dict(工具块 or {})
    落单工具块 = list(落单工具块 or [])
    第一条assistant = None
    for m in messages:
        if m["role"] == "assistant":
            第一条assistant = m["id"]
            break

    injected = []
    for m in messages:
        nu = str(_uuid.uuid4())
        ts = _iso_z(m["created_at"])
        if m["role"] == "assistant":
            这条的 = list(工具块.get(m["id"]) or [])
            if m["id"] == 第一条assistant:
                这条的 = 落单工具块 + 这条的
            for 调用 in 这条的:
                for rec in tool_pair_records(调用, tail, sid, u_tpl, a_tpl):
                    injected.append(rec)
                    tail = rec["uuid"]
        if m["role"] == "user":
            rec = {**_clone(u_tpl), "parentUuid": tail, "uuid": nu, "timestamp": ts,
                   "sessionId": sid, "message": {"role": "user", "content": m["content"]}}
            rec["promptId"] = str(_uuid.uuid4())
        else:
            msg = _clone(a_tpl["message"])
            msg["content"] = [{"type": "text", "text": m["content"]}]
            msg["id"] = "msg_ledger_%d" % m["id"]
            rec = {**_clone(a_tpl), "parentUuid": tail, "uuid": nu, "timestamp": ts,
                   "sessionId": sid, "message": msg}
            rec.pop("requestId", None)
        rec["_ledger_id"] = m["id"]
        injected.append(rec)
        tail = nu

    out = lines[:last_idx + 1] + injected + lines[last_idx + 1:]

    seed_msg = {d["uuid"] for d in lines if d.get("type") in MESSAGE_TYPES and d.get("uuid")}
    parent_of = {d.get("uuid"): d.get("parentUuid") for d in out}

    def _活着的祖先(u):
        seen = set()
        while u in seed_msg:
            if u in seen:
                raise SessionFileError(f"种子里的父子链有环，摘不动（绕回 {u}）")
            seen.add(u)
            u = parent_of.get(u)
        return u

    out = [d for d in out if d.get("uuid") not in seed_msg]
    for d in out:
        if d.get("parentUuid") in seed_msg:
            d["parentUuid"] = _活着的祖先(d["parentUuid"])

    for d in out:
        if d.get("type") == "last-prompt":
            d["leafUuid"] = tail
    return out, sid

def _iso_z(ts):
    ts = (ts or "").strip()
    if not ts:
        raise SessionFileError("账本消息缺 created_at —— 时间戳不许现编")
    if ts.endswith("Z"):
        return ts
    return ts + (".000Z" if "." not in ts else "Z")

def check_records(records):
    if not records:
        raise SessionFileError("会话文件一条记录都没有")

    seen, dup = set(), []
    for d in records:
        u = d.get("uuid")
        if u is None:
            continue
        if u in seen:
            dup.append(u)
        seen.add(u)
    if dup:
        raise SessionFileError(
            f"uuid 重复 {len(dup)} 个：{dup[:3]}\n"
            "   🔴 这是唯一一个会骗过所有绿灯的失败：\n"
            "   重复时零报错 · 进程正常 · 退出码 0 · 第一次请求成功，**而那段历史整个静默消失**。"
        )

    known, broken = set(), []
    for d in records:
        p = d.get("parentUuid")
        if p is not None and p not in known:
            broken.append(d.get("uuid"))
        if d.get("uuid"):
            known.add(d["uuid"])
    if broken:
        raise SessionFileError(
            f"父子链断了 {len(broken)} 处（第一处 uuid={broken[0]}）。\n"
            "   ⚠️ 链会**穿过非消息记录**（attachment / system 都在链上）⇒ 克隆模板时一行都不许丢。"
        )

    msgs = [d for d in records if d.get("type") in MESSAGE_TYPES]
    if not msgs:
        raise SessionFileError("会话文件里一条消息都没有")

    for d in msgs:
        if d.get("type") != "assistant":
            continue
        content = (d.get("message") or {}).get("content")
        if not isinstance(content, list):
            raise SessionFileError(
                f"assistant 记录 {d.get('uuid')} 的 content 不是块数组——是 {type(content).__name__}"
            )
        for b in content:
            if b.get("type") == "thinking" and not b.get("thinking"):
                raise SessionFileError(
                    f"assistant 记录 {d.get('uuid')} 里有空 thinking 块 —— 空的会被 API 拒。"
                    "⚠️ 没有 thinking 的回合**不造空的**"
                )

    if msgs[-1].get("type") != "assistant":
        raise SessionFileError(
            "会话文件最后一条消息不是 assistant —— 切点必须落在【他的回合结束处】。\n"
            "   ✅ 切在他说完之后：轮次自然，等她开口。\n"
            "   ❌ 切在她说完之后：新窗口一开，它必须先答一句一个月前的话。"
        )

    return f"自查通过：{len(records)} 条记录（其中消息 {len(msgs)} 条）· uuid {len(seen)} 个各不相同"

NO_HISTORY = "__no_history__"

def snapshot(path, history_dir):
    src = Path(path)
    if not src.exists():
        return None
    dest_dir = Path(history_dir) / src.stem
    dest_dir.mkdir(parents=True, exist_ok=True)
    used = [int(p.stem) for p in dest_dir.glob("*.jsonl") if p.stem.isdigit()]
    dest = dest_dir / f"{max(used) + 1 if used else 1:04d}.jsonl"
    dest.write_bytes(src.read_bytes())
    return dest

def write_session_file(records, out_dir, session_id, history_dir):
    if not history_dir:
        raise SessionFileError(
            "history_dir 必须显式给 —— 它是「覆盖之前把现有那份存一份」的去处。\n"
            "🔴 漏传就静默不留档，而那正是早先修掉的那个 bug 的形状：\n"
            "   `-p` 追加进去的 `tool_use` / `tool_result` / thinking **只有这一份**"
            "，每轮重建会把它整份盖掉。\n"
            "⇒ 真的不想留档，就传 `session_file.NO_HISTORY`，**把这个决定写出来**。"
        )
    report = check_records(records)
    out = Path(out_dir)
    if not out.exists():
        raise SessionFileError(
            f"会话文件目录不存在：{out_dir}\n"
            "   它是 `claude` 按 cwd 编码出来的目录，**应该由真跑一次 `claude -p` 生成**，不该由我们造。"
        )
    path = out / f"{session_id}.jsonl"
    kept = None if history_dir == NO_HISTORY else snapshot(path, history_dir)
    with path.open("w", encoding="utf-8") as fh:
        for d in records:
            d = {k: v for k, v in d.items() if k != "_ledger_id"}
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    if kept:
        report += f" · 覆盖前留档 {kept.parent.name}/{kept.name}"
    return path, report

def uuids_in(path):
    src = Path(path)
    if not src.exists():
        return set()
    out = set()
    with src.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict) and d.get("uuid"):
                out.add(d["uuid"])
    return out

def read_thinking(path, known_uuids=()):
    src = Path(path)
    if not src.exists():
        raise SessionFileError(
            f"要取 thinking 的会话文件不在：{path}\n"
            "🔴 **这里刻意抛，不是返回空** —— 「他这轮没思考」和「我根本没找到文件」"
            "必须长得不一样。"
        )
    known = set(known_uuids or ())
    blocks = []
    with src.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict) or d.get("uuid") in known:
                continue
            content = (d.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict) and b.get("type") == "thinking" and (b.get("thinking") or "").strip():
                    blocks.append((b["thinking"], b.get("signature") or None))
    if not blocks:
        return None, None
    if len(blocks) == 1:
        return blocks[0]
    return "\n\n".join(t for t, _ in blocks), None

def build(conn, seed_path, out_dir, history_dir, limit_turns=RECENT_TURNS, conversation_id=None,
          工具正文上限=TOOL_BODY_DEFAULT_CAP, 截断说明=None):
    anchor, fresh = resolve_anchor(conn, limit_turns=limit_turns, conversation_id=conversation_id)
    sid, _ = resolve_session_id(conn)
    msgs = recent_messages(conn, anchor, conversation_id=conversation_id)
    if not msgs:
        raise SessionFileError(
            "账本里没有可带的近况层消息 —— **不兜底造一句开场白**"
        )
    分好的, 落单的 = window_tool_calls(
        conn, Path(out_dir) / f"{sid}.jsonl", history_dir, sid,
        cap=工具正文上限, mark=截断说明)
    records, sid = build_records(load_seed(seed_path), msgs, session_id=sid,
                                 工具块=分好的, 落单工具块=落单的)
    path, report = write_session_file(records, out_dir, sid, history_dir)
    how = "开窗现定" if fresh else "沿用"
    n = sum(len(v) for v in 分好的.values())
    工具说明 = f" · 工具块 {n + len(落单的)} 条进了会话文件（其中定位不到归属的 {len(落单的)} 条）"
    return path, sid, (f"{report} · 近况层 {len(msgs)} 条"
                       f"（账本 id {msgs[0]['id']}~{msgs[-1]['id']} · 锚点 {anchor} {how}）{工具说明}")

def cap_body(text, cap, mark=None):
    if text is None:
        return None
    if cap is None or cap <= 0 or len(text) <= cap:
        return text
    return text[:cap] + (mark or TRUNCATED_MARK).format(n=len(text), k=cap)

def _flatten_result(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                out.append(b["text"])
            else:
                out.append(json.dumps(b, ensure_ascii=False))
        return "\n".join(out)
    return json.dumps(content, ensure_ascii=False)

def read_tool_calls(path, known_uuids=(), cap=TOOL_BODY_DEFAULT_CAP, mark=None):
    calls, results = _scan_tool_blocks(path, set(known_uuids or ()), cap)
    _pair_tool_calls(calls, results, cap, mark)
    return calls

def read_tool_calls_across(paths, known_uuids=(), cap=TOOL_BODY_DEFAULT_CAP, mark=None):
    known = set(known_uuids or ())
    calls, results, seen = [], {}, set()
    for p in paths:
        try:
            c, r = _scan_tool_blocks(p, known, cap)
        except (SessionFileError, OSError):
            continue
        for one in c:
            if one["call_id"] and one["call_id"] in seen:
                continue
            seen.add(one["call_id"])
            calls.append(one)
        results.update(r)
    _pair_tool_calls(calls, results, cap, mark)
    return calls

def _pair_tool_calls(calls, results, cap, mark=None):
    for c in calls:
        r = results.get(c["call_id"])
        c["raw_result"] = r
        if r is None:
            continue
        c["result"] = cap_body(_flatten_result(r.get("content")), cap, mark)
        c["state"] = "error" if r.get("is_error") else "ok"

def _scan_tool_blocks(path, known, cap):
    src = Path(path)
    if not src.exists():
        raise SessionFileError(
            f"要取工具调用的会话文件不在：{path}\n"
            "🔴 **这里刻意抛，不是返回空** —— 「他这轮没调工具」和「我根本没找到文件」"
            "必须长得不一样。"
        )
    calls, results = [], {}
    with src.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict) or d.get("uuid") in known:
                continue
            content = (d.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    calls.append({
                        "call_id": b.get("id") or "",
                        "name": b.get("name") or "",
                        "at": d.get("timestamp") or "",
                        "input": cap_body(
                            json.dumps(b.get("input"), ensure_ascii=False)
                            if not isinstance(b.get("input"), str) else b["input"], cap),
                        "result": None,
                        "state": "unknown",
                        "raw": b,
                    })
                elif b.get("type") == "tool_result":
                    results[b.get("tool_use_id") or ""] = b
    return calls, results

TOOL_RESULT_MISSING_NOTE = "〔这一次的结果没读到 —— 不是「没有结果」，是那条记录我没找到〕"

def tool_pair_records(调用, 父uuid, sid, u_tpl, a_tpl, cap=TOOL_BODY_DEFAULT_CAP, mark=None):
    cid = (调用.get("call_id") or "").strip()
    if not cid:
        raise SessionFileError(
            "工具调用没有 call_id —— 还原不出 `tool_use` / `tool_result` 的配对。\n"
            "   🔴 这里刻意抛：配不上对的话，API 会因为「有 tool_use 没有 tool_result」拒掉整轮，"
            "而那时的症状是**她这句话发不出去**，跟记忆库坏了长得一模一样。"
        )
    原始 = 调用.get("raw")
    if not isinstance(原始, dict):
        raise SessionFileError(f"工具调用 {cid} 没带原始块 —— 见 `_scan_tool_blocks()` 里 `raw` 那段")
    ts = (调用.get("at") or "").strip() or "1970-01-01T00:00:00.000Z"

    用 = _clone(a_tpl)
    用["uuid"], 用["parentUuid"], 用["sessionId"], 用["timestamp"] = (
        str(_uuid.uuid4()), 父uuid, sid, ts)
    用["message"] = {**_clone(a_tpl["message"]),
                     "id": "msg_tool_%s" % cid,
                     "stop_reason": "tool_use",
                     "content": [_clone(原始)]}
    用.pop("requestId", None)
    用.pop("_ledger_id", None)

    if 调用.get("state") == "unknown":
        正文 = TOOL_RESULT_MISSING_NOTE
    else:
        正文 = cap_body(_flatten_result((调用.get("raw_result") or {}).get("content")), cap, mark)
    块 = {"type": "tool_result", "tool_use_id": cid, "content": 正文}
    if 调用.get("state") == "error":
        块["is_error"] = True
    果 = _clone(u_tpl)
    果["uuid"], 果["parentUuid"], 果["sessionId"], 果["timestamp"] = (
        str(_uuid.uuid4()), 用["uuid"], sid, ts)
    果["promptId"] = str(_uuid.uuid4())
    果["message"] = {"role": "user", "content": [块]}
    果["sourceToolAssistantUUID"] = 用["uuid"]
    果.pop("_ledger_id", None)
    return [用, 果]

def window_tool_calls(conn, live_path, history_dir, sid,
                      cap=TOOL_BODY_DEFAULT_CAP, mark=None):
    份 = []
    if history_dir and history_dir != NO_HISTORY and sid:
        d = Path(history_dir) / str(sid)
        if d.is_dir():
            份 = sorted(d.glob("*.jsonl"))
    if live_path and Path(live_path).exists():
        份.append(Path(live_path))
    if not 份:
        return {}, []
    调用 = read_tool_calls_across(份, cap=cap, mark=mark)
    if not 调用:
        return {}, []
    归属 = {}
    for r in conn.execute(
            "SELECT call_id, message_id FROM tool_calls WHERE call_id IS NOT NULL"):
        if r["call_id"] and r["message_id"] is not None:
            归属[r["call_id"]] = int(r["message_id"])
    分好的, 落单的 = {}, []
    for c in 调用:
        mid = 归属.get(c.get("call_id"))
        if mid is None:
            落单的.append(c)
        else:
            分好的.setdefault(mid, []).append(c)
    return 分好的, 落单的
