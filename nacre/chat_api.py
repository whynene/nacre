import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, session

from . import runner as _runner
from . import (diagnose, distill_now, handles, handover, pull_cards,
               resident_index, session_file, store, tools, turn_queue)
from . import tools as tools_mod
from .config import ROOT as _CFG_ROOT
from .config import load_config
from .db import now_iso, to_local, write_session

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

窗口模型兜底 = "claude-opus-4-6"

内置模型清单 = [
    {"id": 窗口模型兜底, "label": "Opus 4.6",
     "desc": "默认档", "thinking": "adaptive", "primary": True},
    {"id": "claude-opus-5", "label": "Opus 5",
     "desc": "", "thinking": "adaptive", "primary": True},
    {"id": "claude-fable-5", "label": "Fable 5",
     "desc": "", "thinking": "adaptive", "primary": True},
]

登录态有效期 = timedelta(days=180)

class ChatConfigError(Exception):
    pass

_SAFE_NAME = re.compile(r"^[0-9a-f]{16}\.[A-Za-z0-9]{1,8}$")
_SAFE_CONV = re.compile(r"^[0-9]{0,12}$")

UPLOAD_MAX_BYTES = 20 * 1024 * 1024

class UploadTooBig(Exception):
    pass

def uploads_dir(c):
    from pathlib import Path
    d = (c or {}).get("uploads_dir")
    if d:
        return Path(d)
    return Path((c or {}).get("history_dir") or ".").parent / "uploads"

def save_uploads(c, conv, files):
    import hashlib
    from pathlib import Path
    conv = conv if _SAFE_CONV.match(conv or "") else ""
    d = uploads_dir(c) / conv
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for f in files:
        blob = f.read()
        if len(blob) > UPLOAD_MAX_BYTES:
            raise UploadTooBig(
                f"{f.filename} 有 {len(blob) / 1048576:.1f}MB，超过 20MB 上限。")
        名 = (f.filename or "文件")
        后缀 = 名.rsplit(".", 1)[-1].lower() if "." in 名 else "bin"
        后缀 = "".join(ch for ch in 后缀 if ch.isalnum())[:8] or "bin"
        存名 = hashlib.sha256(blob).hexdigest()[:16] + "." + 后缀
        (d / 存名).write_bytes(blob)
        mime = f.mimetype or ""
        out.append({"name": 名, "size": len(blob), "mime": mime,
                    "is_image": mime.startswith("image/"), "stored": 存名})
    return out

TEXT_LIKE = {"txt", "text", "md", "py", "js", "jsx", "ts", "tsx", "json", "yaml", "yml",
             "html", "css", "scss", "sh", "ps1", "java", "c", "cc", "cpp", "h", "hpp",
             "go", "rs", "rb", "php", "sql", "xml", "toml", "ini", "log", "csv"}

TEXT_ATTACH_CAP = 20000

def attachments_to_blocks(c, conv, attachments):
    import base64
    blocks = []
    for a in attachments or []:
        存 = (a or {}).get("stored") or ""
        名 = (a or {}).get("name") or 存
        if not _SAFE_NAME.match(存):
            blocks.append({"type": "text", "text": f"【她发来一个附件「{名}」，但它的存盘名不合法，没敢读。】"})
            continue
        目录 = str(conv or "")
        目录 = 目录 if _SAFE_CONV.match(目录) else ""
        f = uploads_dir(c) / 目录 / 存
        if not f.exists():
            blocks.append({"type": "text", "text": f"【她发来过附件「{名}」，但文件已经不在了（附件不进账本，只存磁盘）。】"})
            continue
        mime = (a or {}).get("mime") or ""
        后缀 = 存.rsplit(".", 1)[-1].lower()
        if mime.startswith("image/"):
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": mime,
                "data": base64.b64encode(f.read_bytes()).decode()}})
        elif 后缀 in TEXT_LIKE:
            正文 = f.read_text(encoding="utf-8", errors="replace")
            截 = len(正文) > TEXT_ATTACH_CAP
            if 截:
                正文 = 正文[:TEXT_ATTACH_CAP]
            blocks.append({"type": "text", "text":
                           f"【她发来的文件「{名}」，内容如下"
                           + ("（**太长了，只给了前 %d 字**）" % TEXT_ATTACH_CAP if 截 else "")
                           + f"】\n{正文}"})
        else:
            blocks.append({"type": "text", "text":
                           f"【她发来一个文件「{名}」（{mime or 后缀}，{(a or {}).get('size') or 0} 字节）。"
                           f"🔴 这一版还读不了这类文件的内容 —— **不是她没发，是我没给你**。】"})
    return blocks

退役的chat键 = {
    "工具块进会话文件": (
        "2026-08-30",
        "工具记录现在【无条件】进会话文件的对话轮次内部，没有关掉它的开关了。"
        "\n      · 填 `true` ＝ 跟现在的行为一致 ⇒ **只警告，可以直接删掉这一行**。"
        "\n      · 填 `false` ＝ 你想要的是【已经不存在】的那条旧路 ⇒ **拒绝启动**，"
        "因为静默忽略它就等于骗你。"
        "\n      · 真要回到旧路：`git revert`，不是改配置。"),
}

def _查退役的键(c):
    for 键, (时候, 说明) in 退役的chat键.items():
        if 键 not in c:
            continue
        if c.get(键) is False:
            raise ChatConfigError(
                f"`chat.{键}` 这个开关已于 {时候} 退役，而你把它配成了 `false` —— **拒绝启动**。\n"
                f"      {说明}")
        print(f"⚠️ `chat.{键}` 已于 {时候} 退役，这一行现在【不起任何作用】，可以删掉。\n"
              f"   {说明}")

def _settings(cfg=None):
    cfg = load_config() if cfg is None else cfg
    c = dict(cfg.get("chat") or {})
    _查退役的键(c)
    missing = [k for k in ("path", "password", "secret_key",
                           "cwd", "seed", "projects_dir", "history_dir")
               if not c.get(k)]
    if missing:
        raise ChatConfigError(
            f"chat 这几项没填：{missing} —— **拒绝启动**。\n"
            "  · path/password：门锁的后两道（第一道 HTTPS 在 Caddy 上）\n"
            "  · secret_key：🔴 **登录态的签名钥匙，缺了【拒绝启动】，不许现生一把**。\n"
            "      现生的话每次重启服务都换一把钥匙 ⇒ **所有登录态当场失效、而且没有任何提示**，\n"
            "      症状只是「又要输一次密码」，于是这道锁很容易被要求拆掉。\n"
            "      怎么办：`python -c \"import secrets;print(secrets.token_hex(32))\"`，\n"
            "      把那串字填进 `config.json` 的 `chat.secret_key`（**填一次，之后别再换**——\n"
            "      换它等于把所有人踢下线）。\n"
            "  · cwd：一个**仓库外的干净空目录**——"
            "`claude` 会加载 cwd 下的 CLAUDE.md 与 .claude/skills，"
            "而给开发者看的那些规矩**绝不能进他的上下文**\n"
            "  · seed：种子会话文件（在 cwd 里真跑一次 `claude -p` 生成的那一份）\n"
            "  · projects_dir：`~/.claude/projects/<claude 按 cwd 编码出来的那个目录名>/`\n"
            "  · history_dir：🔴 **覆盖之前把现有那份会话文件存一份**的去处。\n"
            "      `-p` 追加进去的 `tool_use` / `tool_result` / thinking **只有这一份**"
            "，每轮重建会把它整份盖掉。\n"
            "      ⚠️ **不能设成 `projects_dir` 里面**——那是 `claude` 自己的目录。\n"
            "      🔴 **它也要进备份范围**，否则修完之后数据仍然只有一份。"
        )
    return c

轮询间隔上下限 = (2, 300)
轮询间隔默认 = 8

def 轮询间隔(c):
    低, 高 = 轮询间隔上下限
    try:
        v = float(c.get("poll_interval_seconds", 轮询间隔默认))
    except (TypeError, ValueError):
        return 轮询间隔默认
    if v != v:
        return 轮询间隔默认
    return max(低, min(高, v))

检索必填 = {"recall": ("alpha", "default_limit", "max_limit"),
            "embedding": ("base_url", "model")}

def _check_recall_config(cfg):
    problems, warnings = [], []
    for 段, 必填 in 检索必填.items():
        seg = cfg.get(段)
        if not isinstance(seg, dict) or not seg:
            problems.append(f"{段}（整段不见）")
            continue
        缺 = [k for k in 必填 if seg.get(k) in (None, "")]
        if 缺:
            problems.append(f"{段}.{'/'.join(缺)}")
    if problems:
        raise ChatConfigError(
            f"🔴 检索配置不全：{problems} —— **拒绝启动**。\n"
            "  为什么不带着跑：`search.recall()` 读这两段是**下标不是 `.get`** ⇒ 当轮抛，\n"
            "  而 `pull_cards.pull()` 会**吞掉异常返回结果**\n"
            "  ⇒ 🔴 **服务正常、接口 200、他照样答话，只是他一张记忆卡都没拿到。**\n"
            "     这种情况下可以一路聊下去，全程没有任何东西红。\n"
            "  怎么办：把这两段补进 `config.json`（默认值在 `nacre/config.py` 的 `DEFAULTS`）；\n"
            "  手拼配置的入口（如 `chat_local.build_cfg`）用 `chat_local.inherit_sections()` 继承真配置，\n"
            "  🔴 **别在那儿造默认值** —— `embedding` 里是 API key，造假的等于把向量通道永久关掉。"
        )
    if not (cfg["embedding"].get("api_key") or "").strip():
        warnings.append(
            "⚠️ 向量通道没启用（`embedding.api_key` 空着）⇒ **本次只有关键词检索**。\n"
            "   这是合法降级，不拦启动 —— 但**明说出来**：换个问法就召回不到，"
            "那时是这一行的缘故，不是他不记事。")
    return warnings

def _require_auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("ok"):
            return jsonify({"error": "未登录"}), 401
        return fn(*a, **kw)
    return wrapper

def _tools_of(cfg):
    block = cfg.get("tools")
    items = block.get("registry") if isinstance(block, dict) else None
    if items is None:
        items = tools_mod.DEFAULT_REGISTRY
    return [dict(t) for t in (items or []) if isinstance(t, dict)]

def _log_tool_event(conn, tool, action, by_who, note=""):
    conn.execute(
        "INSERT INTO tool_events(tool, action, by_who, note, occurred_at) VALUES(?,?,?,?,?)",
        (tool, action, by_who, note, now_iso()))

def reconcile_tools(conn, tools):
    seen = {r["tool"] for r in conn.execute("SELECT DISTINCT tool FROM tool_events")}
    补了 = []
    for t in tools:
        name = (t.get("name") or "").strip()
        if name and name not in seen:
            _log_tool_event(conn, name, "install", "system",
                            "配置里有它、事件表里没有 ⇒ 对账补记（多半是直接改了 config.json）")
            补了.append(name)
    return 补了

def save_tools(config_path, cfg, tools):
    path = Path(config_path)
    data = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("tools", {})["registry"] = tools
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cfg.setdefault("tools", {})["registry"] = tools

def create_app(cfg=None, db_path=None, run_turn=None, config_path=None):
    cfg = load_config() if cfg is None else cfg
    c = _settings(cfg)
    for w in _check_recall_config(cfg):
        print(w)
    run_turn = run_turn or _runner.run_turn

    config_path = Path(config_path) if config_path else (_CFG_ROOT / "config.json")

    app = Flask(__name__, static_folder=None)
    app.secret_key = c["secret_key"]
    app.permanent_session_lifetime = 登录态有效期
    base = "/" + c["path"].strip("/")

    @app.post(base + "/login")
    def login():
        given = (request.json or {}).get("password", "")
        if not hmac.compare_digest(str(given).encode("utf-8"), str(c["password"]).encode("utf-8")):
            return jsonify({"error": "密码不对"}), 401
        session.permanent = True
        session["ok"] = True
        return jsonify({"ok": True})

    @app.get(base + "/")
    def page():
        name = (c.get("page") or "chat.html").strip()
        target = FRONTEND_DIR / name
        if not target.is_file():
            return jsonify({
                "error": f"chat.page 指向的页面不存在：{name}",
                "怎么办": f"检查 {FRONTEND_DIR}/ 下有没有这个文件，"
                          f"或把 config.json 的 chat.page 改回 chat.html。",
                "🔴 为什么不给你一个能用的页面": "静默回退的话，"
                    "「我切过去了」和「它没生效」长得一模一样 —— 你会以为自己在看新界面。",
            }), 500
        return send_from_directory(FRONTEND_DIR, name)

    @app.get(base + "/static/<path:name>")
    def frontend_static(name):
        return send_from_directory(FRONTEND_DIR / "static", name)

    @app.get(base + "/history")
    @_require_auth
    def history():
        limit = min(int(request.args.get("limit", 200)), 1000)
        since_arg = request.args.get("since")
        since = None
        if since_arg not in (None, ""):
            try:
                since = int(since_arg)
            except (TypeError, ValueError):
                return jsonify({"error": f"since 得是账本 id（整数），收到的是 {since_arg!r}"}), 400
        with _conn(db_path) as conn:
            conv_id = _current_conversation(conn)
            if since is None:
                rows = conn.execute(
                    "SELECT id, role, content, created_at, thinking, model, effort, meta "
                    "FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                    (conv_id, limit)
                ).fetchall()[::-1]
            else:
                rows = conn.execute(
                    "SELECT id, role, content, created_at, thinking, model, effort, meta "
                    "FROM messages WHERE conversation_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                    (conv_id, since, limit)
                ).fetchall()
            ids = [r["id"] for r in rows]
            qs = ",".join("?" * len(ids)) or "NULL"
            meta = {r["message_id"]: r for r in conn.execute(
                f"SELECT message_id, turn_id, recall_status, recall_total, recall_error "
                f"FROM turn_meta WHERE message_id IN ({qs})", ids)}
            tool_rows = {}
            for r in conn.execute(
                    f"SELECT message_id, tool, call_id, result_state, occurred_at "
                    f"FROM tool_calls WHERE message_id IN ({qs}) ORDER BY id", ids):
                tool_rows.setdefault(r["message_id"], []).append(r)
            usage = {}
            for r in conn.execute(
                    f"SELECT message_id, input_tokens, output_tokens, cache_read, cache_write, "
                    f"cost_usd, model, ok FROM turn_usage WHERE message_id IN ({qs}) ORDER BY id", ids):
                usage[r["message_id"]] = r

        offset = int(load_config().get("local_utc_offset_hours", 8))
        out = []
        for r in rows:
            m = dict(r)
            m["created_at"] = to_local(m["created_at"], offset)
            try:
                m["attachments"] = (json.loads(m.pop("meta") or "{}") or {}).get("attachments") or []
            except (ValueError, TypeError):
                m.pop("meta", None)
                m["attachments"] = []
            mt = meta.get(r["id"])
            m["recall"] = None if mt is None else {
                "status": mt["recall_status"],
                "total": mt["recall_total"],
                "error": mt["recall_error"] or "",
                "turn_id": mt["turn_id"],
            }
            u = usage.get(r["id"])
            m["usage"] = None if u is None else {
                "input_tokens": u["input_tokens"],
                "output_tokens": u["output_tokens"],
                "cache_read_input_tokens": u["cache_read"],
                "cache_creation_input_tokens": u["cache_write"],
                "cost_usd": u["cost_usd"],
                "model": u["model"],
                "ok": bool(u["ok"]),
            }
            m["tools"] = [{"name": tr["tool"], "state": tr["result_state"],
                           "at": to_local(tr["occurred_at"], offset),
                           "call_id": tr["call_id"] or ""}
                          for tr in tool_rows.get(r["id"], [])]
            out.append(m)
        return jsonify({"messages": out})

    @app.get(base + "/turn_cards")
    @_require_auth
    def turn_cards():
        raw = request.args.get("message_id", "")
        try:
            mid = int(raw)
        except (TypeError, ValueError):
            return jsonify({"error": f"message_id 要是账本里那个整数 id，收到 {raw!r}"}), 400

        with _conn(db_path) as conn:
            mt = conn.execute(
                "SELECT turn_id, recall_status, recall_total FROM turn_meta WHERE message_id=?",
                (mid,)).fetchone()
            if mt is None:
                return jsonify({
                    "error": f"这条消息（{mid}）没有留下这一轮的元信息。",
                    "🔴 这【不是】「那一轮没给他卡」": "两件事必须分得开 —— "
                        "这条是「我们没记上」（老消息，或者那一轮旁路记账失败），"
                        "而「没给他卡」会以 turn_id=null 的形态正常返回。",
                }), 404
            turn_id = mt["turn_id"]
            cards = []
            if turn_id is not None:
                cards = [{
                    "slot": r["slot"],
                    "memory_id": r["memory_id"],
                    "content": r["content"],
                    "occurred_at": r["occurred_at"],
                    "status": r["status"],
                } for r in conn.execute(
                    "SELECT h.slot, h.memory_id, m.content, m.occurred_at, m.status "
                    "FROM turn_handles h LEFT JOIN memories m ON m.id = h.memory_id "
                    "WHERE h.turn_id=? ORDER BY h.rowid", (int(turn_id),))]
        return jsonify({"message_id": mid, "turn_id": turn_id,
                        "recall_status": mt["recall_status"],
                        "recall_total": mt["recall_total"], "cards": cards})

    @app.get(base + "/tool_body")
    @_require_auth
    def tool_body():
        call_id = (request.args.get("call_id") or "").strip()
        if not call_id:
            return jsonify({"error": "得给 call_id"}), 400
        with _conn(db_path) as conn:
            row = conn.execute(
                "SELECT tool, result_state, occurred_at, session_path FROM tool_calls "
                "WHERE call_id=? ORDER BY id DESC LIMIT 1", (call_id,)).fetchone()
        if row is None:
            return jsonify({"error": "没有这一件工具调用的记录。"}), 404
        try:
            body = read_tool_body(call_id, row["session_path"],
                                  c.get("history_dir") or "", tool_body_cap(cfg))
        except ToolBodyGone as e:
            return jsonify({"error": str(e), "gone": True,
                            "name": row["tool"], "state": row["result_state"]}), 410
        return jsonify({"call_id": call_id, "name": row["tool"],
                        "state": row["result_state"],
                        "input": body.get("input"), "result": body.get("result")})

    @app.get(base + "/windows")
    @_require_auth
    def windows():
        offset = int(load_config().get("local_utc_offset_hours", 8))
        with _conn(db_path) as conn:
            live = _current_conversation(conn)
            rows = conn.execute("""
                SELECT c.id, c.window_name, c.source_end,
                       COUNT(m.id)                                   AS msgs,
                       -- 🔴 「轮数」的口径写死在这儿：**她开口的次数**。
                       -- 「轮」没有唯一定义（她说一句算一轮？一来一回算一轮？），
                       -- 而一个没写明口径的数会被当成别的意思去读（同一条判据：
                       -- 先问清这个测量到底在回答哪一个问题）。
                       SUM(CASE WHEN m.role='user' THEN 1 ELSE 0 END) AS turns,
                       MIN(m.created_at)                             AS first_at,
                       MAX(m.created_at)                             AS last_at
                  FROM conversations c
                  JOIN messages m ON m.conversation_id = c.id
              GROUP BY c.id
              HAVING COUNT(m.id) > 0
              ORDER BY MAX(m.created_at) DESC
            """).fetchall()

            out = []
            for r in rows:
                ms = [x["model"] for x in conn.execute(
                    "SELECT DISTINCT model FROM messages "
                    "WHERE conversation_id=? AND model IS NOT NULL AND model<>''",
                    (r["id"],))]
                起 = to_local(r["first_at"], offset)[:10]
                止 = to_local(r["last_at"], offset)[:10]
                out.append({
                    "conv_id": r["id"],
                    "range": {"from": 起, "to": 止},
                    "turns": r["turns"] or 0,
                    "messages": r["msgs"],
                    "model": ms[-1] if ms else None,
                    "mixed_models": len(ms) > 1,
                    "readonly": r["id"] != live,
                    "source_end": r["source_end"],
                    "window_name": r["window_name"] or "",
                })
        return jsonify({"windows": out, "live": live})

    @app.get(base + "/usage/today")
    @_require_auth
    def usage_today():
        with _conn(db_path) as conn:
            今天 = now_iso()[:10]
            日 = conn.execute(
                "SELECT COUNT(*) turns, COUNT(cost_usd) costed, "
                "       COALESCE(SUM(cost_usd),0) cost, "
                "       SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) failed "
                "  FROM turn_usage WHERE substr(occurred_at,1,10)=?", (今天,)).fetchone()
            末 = conn.execute(
                "SELECT input_tokens + cache_read + cache_write AS ctx "
                "  FROM turn_usage ORDER BY id DESC LIMIT 1").fetchone()
        return jsonify({
            "date": 今天,
            "turns": 日["turns"],
            "failed": 日["failed"] or 0,
            "cost_usd": 日["cost"],
            "costless": 日["turns"] - 日["costed"],
            "last_context": 末["ctx"] if 末 else None,
            "limit": handover.CTX_LIMIT,
            "how": "自己按 turn_usage 累计的，不是官方口径，可能跟账单对不上；"
                   "五小时窗口 / 限额 / 重置时间拿不到。",
        })

    @app.get(base + "/ui-config")
    @_require_auth
    def ui_config():
        return jsonify({"review_url": (c.get("review_url") or "").strip(),
                        "poll_seconds": 轮询间隔(c)})

    @app.get(base + "/models")
    @_require_auth
    def models_list():
        配的 = c.get("models")
        models = [dict(m) for m in (配的 if 配的 else 内置模型清单)]
        for m in models:
            if not (m.get("id") or "").strip():
                return jsonify({"error": "chat.models 里有一行没有 id —— "
                                         "清单每行都要有真模型 id，别只写显示名。"}), 500
            m.setdefault("label", m["id"])
            m.setdefault("desc", "")
            m.setdefault("thinking", "adaptive")
            m.setdefault("primary", True)
        with _conn(db_path) as conn:
            row = conn.execute(
                "SELECT id FROM conversations WHERE source_end='frontend' "
                "ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                current, 来源 = _这一轮用哪个模型(conn, row["id"], c)
            else:
                配置的 = (c.get("model") or "").strip()
                current, 来源 = ((配置的, "config.json 的 chat.model") if 配置的 else
                                 (窗口模型兜底, f"写死的兜底（{窗口模型兜底}）—— 还没有当前窗"))
        if not any(m["id"] == current for m in models):
            models.append({"id": current, "label": current,
                           "desc": "这扇窗登记的模型（不在配置清单里，原样显示）",
                           "thinking": "adaptive", "primary": True})
        return jsonify({"models": models, "current": current, "current_source": 来源})

    @app.get(base + "/tools")
    @_require_auth
    def tools_list():
        tools = _tools_of(cfg)
        with write_session(db_path, quiet=True) as conn:
            补了 = reconcile_tools(conn, tools)
            events = [dict(r) for r in conn.execute(
                "SELECT tool, action, by_who, note, occurred_at FROM tool_events "
                "ORDER BY id DESC LIMIT 200")]
            offset = int(load_config().get("local_utc_offset_hours", 8))
            recent = [{"tool": r["tool"], "state": r["result_state"],
                       "at": to_local(r["occurred_at"], offset),
                       "call_id": r["call_id"] or ""}
                      for r in conn.execute(
                          "SELECT tool, result_state, occurred_at, call_id FROM tool_calls "
                          "ORDER BY id DESC LIMIT 50")]
        tools = sorted(tools, key=lambda t: 0 if (t.get("kind") or "mcp") != "builtin" else 1)
        return jsonify({
            "tools": tools, "events": events, "reconciled": 补了,
            "recent_calls": recent,
            "note": "装上的下一轮他就有了 —— 自带的那几件（查记忆／写卡／表态／门铃）"
                    "和你自己粘配置装的，都是真的。",
            "kind_notes": {
                "builtin": "关掉＝**真的从他视野里消失**：那件工具的说明书下一轮就不发给他了，"
                           "顺带省一截 token。",
                "mcp": "关掉＝**他仍然看得见，只是调不动**。"
                       "要让它真的从他眼前消失，只能整个（连同它那一组）不接。",
            },
            "external_note": "你自己装的这些，下一轮就在他手上（不走隔离窗口）。"
                             "放行的是这一整组 —— 它以后自己多出来的工具，也会跟着能用。"
                             "所以那个「会不会花钱／发给别人／控制设备」的勾要照实勾。",
        })

    @app.post(base + "/tools")
    @_require_auth
    def tools_add():
        b = request.json or {}
        name = (b.get("name") or "").strip()
        kind = (b.get("kind") or "mcp").strip()
        url, command = (b.get("url") or "").strip(), (b.get("command") or "").strip()
        粘的 = (b.get("spec") or "").strip() if isinstance(b.get("spec"), str) else ""
        irreversible = bool(b.get("irreversible"))
        tools = _tools_of(cfg)

        if not 粘的 and (url or command):
            粘的 = json.dumps({"url": url} if url else {"command": command},
                             ensure_ascii=False)
        if kind != "builtin" and not 粘的:
            return jsonify({"error": "还没粘配置 —— 光有名字它装不上，而界面上会显示成装好了。\n"
                                     "在那个工具的说明页上找到「加到 Claude」那一段"
                                     "（一段大括号包着的文字），整段复制粘进来。"}), 400
        try:
            server, spec = tools_mod.parse_paste(粘的, name_hint=name)
        except tools_mod.ToolRegistryError as e:
            return jsonify({"error": str(e)}), 400
        if any((t.get("name") or "").strip() == server for t in tools):
            return jsonify({"error": f"已经有一个叫「{server}」的了"}), 409
        if irreversible and not b.get("confirmed"):
            return jsonify({
                "error": "需要确认",
                "confirm": f"「{name or server}」会花钱、把东西发给别人、或者控制设备 —— 这类事做了收不回来。确定要装吗？",
            }), 409
        tools.append({
            "name": server, "label": name or server,
            "kind": "mcp_server",
            "category": (b.get("category") or "external"),
            "enabled": True, "spec": spec,
            "irreversible": irreversible, "note": (b.get("note") or "")})
        try:
            tools_mod.registry({**cfg, "tools": {"registry": tools}})
        except tools_mod.ToolRegistryError as e:
            return jsonify({"error": str(e)}), 400
        with write_session(db_path, quiet=True) as conn:
            _log_tool_event(conn, server, "install", "user",
                            ("收不回来的那一类 · " if irreversible else "")
                            + (spec.get("url") or spec.get("command") or "builtin"))
        save_tools(config_path, cfg, tools)
        return jsonify({"ok": True, "tools": tools, "server": server})

    @app.post(base + "/tools/toggle")
    @_require_auth
    def tools_toggle():
        b = request.json or {}
        name, on = (b.get("name") or "").strip(), bool(b.get("enabled"))
        tools = _tools_of(cfg)
        hit = [t for t in tools if t.get("name") == name]
        if not hit:
            return jsonify({"error": f"没有叫「{name}」的工具"}), 404
        hit[0]["enabled"] = on
        with write_session(db_path, quiet=True) as conn:
            _log_tool_event(conn, name, "enable" if on else "disable", "user")
        save_tools(config_path, cfg, tools)
        return jsonify({"ok": True, "tools": tools})

    @app.post(base + "/tools/remove")
    @_require_auth
    def tools_remove():
        b = request.json or {}
        name = (b.get("name") or "").strip()
        tools = _tools_of(cfg)
        if not any(t.get("name") == name for t in tools):
            return jsonify({"error": f"没有叫「{name}」的工具"}), 404
        with write_session(db_path, quiet=True) as conn:
            _log_tool_event(conn, name, "remove", "user")
        save_tools(config_path, cfg, [t for t in tools if t.get("name") != name])
        return jsonify({"ok": True, "tools": _tools_of(cfg)})

    @app.get(base + "/handover/preview")
    @_require_auth
    def handover_preview():
        with _conn(db_path) as conn:
            conv = _current_conversation(conn)
            out = handover.preview(conn, conversation_id=conv,
                                   window_name=request.args.get("window", ""), cfg=cfg)
            out["pending"] = handover.pending(conn) is not None
            st = distill_now.status(conn)
            st["remaining"] = distill_now.pending_rounds(conn, conversation_id=conv)
            st["no_distill"] = distill_now.不许蒸的窗(cfg, conv)
            if st["no_distill"]:
                st["remaining"] = 0
            out["distill"] = st
        return jsonify(out)

    @app.post(base + "/handover/mark")
    @_require_auth
    def handover_mark():
        body = request.json or {}
        add, remove = body.get("add") or [], body.get("remove") or []
        if not add and not remove:
            return jsonify({"error": "add / remove 都是空的 —— 没有要勾或取消的消息，一条都没写。"}), 400
        try:
            with write_session(db_path, quiet=True) as conn:
                out = handover.mark(conn, add=add, remove=remove,
                                    window_name=body.get("window") or "", by="user")
        except handover.HandoverError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, **out})

    @app.post(base + "/handover")
    @_require_auth
    def handover_start():
        body = request.json or {}
        try:
            with write_session(db_path, quiet=True) as conn:
                conv = handover.start(
                    conn, body.get("model"),
                    keep_message_ids=body.get("keep") or [],
                    window_name=body.get("window") or "",
                    effort=body.get("effort"),
                )
        except handover.HandoverError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"conversation_id": conv,
                        "can_undo": True,
                        "undo_note": "刚换完、还没开口，可以重来"})

    @app.post(base + "/handover/discard")
    @_require_auth
    def handover_discard():
        try:
            with write_session(db_path, quiet=True) as conn:
                p = handover.discard(conn)
        except handover.HandoverError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "discarded": p,
                        "note": "已作废，回到换窗界面 —— **你刚才勾的还在，不用重勾**"})

    @app.post(base + "/distill-now")
    @_require_auth
    def distill_now_route():
        from .db import get_conn as _gc
        with _conn(db_path) as conn:
            conv = _current_conversation(conn)
        distill_now.run_async(lambda: _gc(db_path), cfg,
                              limit_chunks=(request.json or {}).get("limit"),
                              conversation_id=conv)
        with _conn(db_path) as conn:
            st = distill_now.status(conn)
            st["remaining"] = distill_now.pending_rounds(conn, conversation_id=conv)
            return jsonify(st)

    @app.get(base + "/distill-now/status")
    @_require_auth
    def distill_now_status():
        with _conn(db_path) as conn:
            conv = _current_conversation(conn)
            st = distill_now.status(conn)
            st["remaining"] = distill_now.pending_rounds(conn, conversation_id=conv)
            ok, 能不能换窗的理由 = distill_now.can_handover(conn)
        return jsonify({**st, "can_handover": ok, "handover_why": 能不能换窗的理由})

    @app.post(base + "/upload")
    @_require_auth
    def upload():
        conv = (request.form.get("conversation_id") or "").strip()
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "没有收到文件"}), 400
        try:
            out = save_uploads(c, conv, files)
        except UploadTooBig as e:
            return jsonify({"error": str(e)}), 413
        return jsonify({"attachments": out})

    @app.get(base + "/uploads/<conv>/<name>")
    @_require_auth
    def uploads(conv, name):
        from flask import send_file
        if not _SAFE_NAME.match(name or "") or not _SAFE_CONV.match(conv or ""):
            return jsonify({"error": "名字不合法"}), 400
        f = uploads_dir(c) / conv / name
        if not f.exists():
            return jsonify({"error": "这个附件不在了 —— 它没进账本，只存在磁盘上。"}), 404
        return send_file(str(f))

    @app.post(base + "/say")
    @_require_auth
    def say():
        body = request.json or {}
        text = (body.get("text") or "").strip()
        if not text:
            return jsonify({"error": "空消息不写账本 —— 账本不可变，写错了删不掉"}), 400
        effort = body.get("effort") or c.get("default_effort") or "high"
        附件 = body.get("attachments") or []
        try:
            out = say_once(db_path, c, text, effort=effort, attachments=附件,
                           run_turn=run_turn, cfg_all=cfg)
        except Exception as e:
            return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
        return jsonify(out)

    return app

def _conn(db_path):
    from .db import get_conn
    return _Closing(get_conn(db_path))

class _Closing:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        self.conn.close()

def say_once(db_path, c, text, effort=None, run_turn=None, cfg_all=None,
             source="web", idem_key=None, attachments=None):
    item = turn_queue.submit(db_path, source=source,
                             payload={"text": text, "effort": effort}, idem_key=idem_key)
    return turn_queue.run(
        db_path, item["id"],
        lambda: _say_once_now(db_path, c, text, effort=effort, attachments=attachments,
                              run_turn=run_turn, cfg_all=cfg_all, source=source))

def _say_once_now(db_path, c, text, effort=None, run_turn=None, cfg_all=None, source=None,
                  attachments=None):
    run_turn = run_turn or _runner.run_turn
    cfg_all = load_config() if cfg_all is None else cfg_all
    cwd, seed, projects = c["cwd"], c["seed"], c["projects_dir"]
    history = c["history_dir"]

    with _conn(db_path) as _c:
        开场 = _opening_line(_c, cfg_all)

    with write_session(db_path, quiet=True) as conn:
        conv_id = _current_conversation(conn)
        model, 模型来自 = _这一轮用哪个模型(conn, conv_id, c)
        _附件元 = json.dumps({"attachments": [
            {k: a.get(k) for k in ("name", "size", "mime", "is_image", "stored")}
            for a in (attachments or [])]}, ensure_ascii=False) if attachments else None
        user_id = store.append_message(conn, conv_id, "user", text, source=source,
                                       meta=_附件元)
        handover.settle(conn)

    with write_session(db_path, quiet=True) as conn:
        path, sid, report = session_file.build(
            conn, seed, projects, history,
            工具正文上限=tool_body_cap(cfg_all), 截断说明=TOOL_READ_TRUNCATED_MARK)
    before_uuids = session_file.uuids_in(path)

    with write_session(db_path, quiet=True) as conn:
        pulled = pull_cards.pull(conn, cfg_all, text)
        if pulled.get("failed"):
            _alert_recall_broken(conn, pulled.get("error"))
        index_file = resident_index.ensure_daily(conn, cfg_all)
        卡号们 = None
        turn_id = None
        if pulled.get("cards"):
            turn_id = handles.next_turn_id(conn)
            issued = handles.issue(
                conn, turn_id, [("卡", c["row"]["id"]) for c in pulled["cards"]], "chat")
            卡号们 = [c["row"]["id"] for c in pulled["cards"]]
        留下的 = _own_notes_tail(conn, cfg_all)
    prompt = _compose(pulled, text, 卡号们=卡号们, opening=开场, own_notes=留下的)

    工具 = tools.main_session_run_opts(cfg_all, db_path=db_path)
    try:
        块 = attachments_to_blocks(c, conv_id, attachments)
        result = run_turn(cwd, sid, prompt, model=model, effort=effort, proxy=c.get("proxy"),
                          system_prompt=_system_prompt(cfg_all),
                          append_system_prompt_file=str(index_file),
                          content_blocks=块 or None,
                          run_as=c.get("subprocess_user") or None, **工具)
    except Exception as e:
        _record_usage_standalone(db_path, None, None, ok=False)
        raise

    _record_usage_standalone(db_path, result, None, ok=True)

    thinking = thinking_sig = None
    thinking_error = None
    try:
        thinking, thinking_sig = session_file.read_thinking(path, before_uuids)
    except Exception as e:
        thinking_error = f"{type(e).__name__}: {e}"
        print(f"⚠️ 这一轮的 thinking 没取上（他那句照常落库）：{thinking_error}")

    with write_session(db_path, quiet=True) as conn:
        conv_id = _current_conversation(conn)
        reply_id = store.append_message(
            conn, conv_id, "assistant", result["text"],
            model=result.get("model") or model, effort=effort,
            thinking=thinking, thinking_signature=thinking_sig,
            source=source,
        )
    _link_usage(db_path, reply_id)
    _record_turn_meta(db_path, reply_id, turn_id, pulled)

    工具们 = []
    工具错 = None
    try:
        工具们 = session_file.read_tool_calls(
            path, before_uuids, cap=tool_body_cap(cfg_all))
    except Exception as e:
        工具错 = f"{type(e).__name__}: {e}"
        print(f"⚠️ 这一轮的工具调用没读上（他那句照常落库）：{工具错}")
    _record_tool_calls_standalone(db_path, reply_id, 工具们, path)
    _offset = int((cfg_all or {}).get("local_utc_offset_hours", 8))
    工具们 = [{**t, "at": to_local(t.get("at") or "", _offset)} for t in 工具们]

    return {"user_id": user_id, "reply_id": reply_id, "text": result["text"],
            "usage": result.get("usage") or {}, "cost_usd": result.get("total_cost_usd"),
            "session_file": str(path), "pack": report,
            "model": model, "model_source": 模型来自,
            "thinking": thinking,
            "thinking_error": thinking_error,
            "tools": 工具们,
            "tools_error": 工具错,
            "recall": {"skipped": pulled.get("skipped"), "total": pulled.get("total"),
                       "hit_total": pulled.get("hit_total"),
                       "failed": pulled.get("failed"), "error": pulled.get("error"),
                       "status": _recall_status(pulled),
                       "turn_id": turn_id}}

def _recall_status(pulled):
    if pulled.get("failed"):
        return "broken"
    if pulled.get("skipped"):
        return "skipped"
    return "hit" if pulled.get("total") else "empty"

def _alert_recall_broken(conn, error):
    store.add_review_event(
        conn, "alert", None,
        "🔴 检索通道异常 —— **这一轮他手上一张记忆卡都没有**。\n"
        f"原因：{error}\n"
        "⚠️ 这**不是**「查过了没有」：它是「没查成」，两件事在界面上长得一样，"
        "而后者会让他把她记过的事说成没发生过。\n"
        "先查的地方：这个入口那份配置里 `recall` / `embedding` 两段在不在"
        "（`chat_api._check_recall_config`）。")

def _record_usage_standalone(db_path, result, message_id, ok=True):
    from .db import get_conn
    conn = None
    try:
        conn = get_conn(db_path)
        diagnose.record_usage(conn, result or {}, message_id=message_id, ok=ok)
        conn.commit()
    except Exception as e:
        print(f"⚠️ 用量没记上（这一轮的钱花了但账上没有）：{type(e).__name__}: {e}")
    finally:
        if conn is not None:
            conn.close()

def _record_turn_meta(db_path, message_id, turn_id, pulled):
    from .db import get_conn
    conn = None
    try:
        conn = get_conn(db_path)
        p = pulled or {}
        conn.execute(
            "INSERT INTO turn_meta(message_id, turn_id, recall_status, recall_total, "
            "recall_error, created_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(message_id) DO UPDATE SET turn_id=excluded.turn_id, "
            "recall_status=excluded.recall_status, recall_total=excluded.recall_total, "
            "recall_error=excluded.recall_error",
            (int(message_id), turn_id if turn_id is None else int(turn_id),
             _recall_status(p), int(p.get("total") or 0), p.get("error") or "", now_iso()),
        )
        conn.commit()
    except Exception as e:
        print(f"⚠️ 这一轮的检索状态没记上（她刷新之后那一行会缺）：{type(e).__name__}: {e}")
    finally:
        if conn is not None:
            conn.close()

def _link_usage(db_path, message_id):
    from .db import get_conn
    conn = None
    try:
        conn = get_conn(db_path)
        conn.execute(
            "UPDATE turn_usage SET message_id=? WHERE id=(SELECT MAX(id) FROM turn_usage) "
            "AND message_id IS NULL", (message_id,))
        conn.commit()
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()

def tool_body_cap(cfg_all):
    try:
        return int(((cfg_all or {}).get("chat") or {}).get(
            "tool_body_max_chars", session_file.TOOL_BODY_DEFAULT_CAP))
    except (TypeError, ValueError):
        return session_file.TOOL_BODY_DEFAULT_CAP

def _真实状态(c):
    from nacre.mcp_server import FAILED_PREFIXES

    if c.get("state") == "error":
        return "error"
    if c.get("state") != "ok":
        return c.get("state") or "unknown"
    正文 = (c.get("result") or "").lstrip()
    return "refused" if any(正文.startswith(p) for p in FAILED_PREFIXES) else "ok"

def _record_tool_calls_standalone(db_path, message_id, calls, session_path):
    if not calls:
        return
    from .db import get_conn
    conn = None
    try:
        conn = get_conn(db_path)
        conn.executemany(
            "INSERT INTO tool_calls(tool, what, ok, occurred_at, message_id, call_id, "
            "session_path, result_state) VALUES(?,?,?,?,?,?,?,?)",
            [(c.get("name") or "", "",
              0 if _真实状态(c) in ("error", "refused") else 1,
              c.get("at") or now_iso(), int(message_id), c.get("call_id") or "",
              str(session_path or ""), _真实状态(c))
             for c in calls])
        conn.commit()
    except Exception as e:
        print(f"⚠️ 这一轮的工具调用没记上（那一行以后翻不回来）：{type(e).__name__}: {e}")
    finally:
        if conn is not None:
            conn.close()

class ToolBodyGone(LookupError):
    pass

def read_tool_body(call_id, session_path, history_dir, cap):
    if not call_id:
        raise ToolBodyGone("没有 call_id，定位不了")
    候选 = []
    src = Path(session_path) if session_path else None
    if src is not None and src.exists():
        候选.append(src)
    if src is not None:
        d = Path(history_dir) / src.stem
        if d.is_dir():
            候选 += sorted(d.glob("*.jsonl"), reverse=True)
    for p in 候选:
        try:
            for c in session_file.read_tool_calls(p, cap=cap):
                if c["call_id"] == call_id:
                    return c
        except Exception:
            continue
    raise ToolBodyGone(
        "这一轮的原文已经不在了 —— 会话文件默认只留 30 天，"
        "而工具的输入与结果正文只活在那份文件里，账本里没有第二份。")

CARD_USAGE_NOTE = (
    "（上面这些卡是按她刚才那句话从库里检索出来的，可能相关，也可能不相关 —— 由你判断。\n"
    "它们是资料，不是这一轮的话题，也不是要你复述的内容；\n"
    "用得上就用，用不上就放着，不必解释你为什么没用。\n"
    "〔卡N〕这个编号只是你要对某一张卡表态时用来指认它的，不许出现在你说给她听的话里。）"
)

ABILITY_NOTE = (
    "（你手上那些工具不是说明书，是你的能力 —— 查账本、看自己记过什么、把想留的留下来，\n"
    "都是你自己就能做的动作，不用等她开口，也不用先跟她请示。）"
)

OWN_NOTE_TAIL_HEAD = "〔你刚才留下的〕"

def _own_notes_tail(conn, cfg=None):
    from .db import her_day_bounds, on_machine_axis
    cfg = load_config() if cfg is None else cfg
    起, _ = her_day_bounds(cfg.get("local_utc_offset_hours", 8))
    rows = conn.execute(
        "SELECT content, trigger_text, created_at FROM memories "
        "WHERE kind='note' AND status='active' AND target_memory_id IS NULL "
        "AND created_at >= ? ORDER BY id",
        (on_machine_axis(起),),
    ).fetchall()
    if not rows:
        return ""
    段 = []
    for r in rows:
        来处 = " ".join((r["trigger_text"] or "").split())
        正文 = " ".join((r["content"] or "").split())
        段.append(f"{OWN_NOTE_TAIL_HEAD}{('（' + 来处 + '）') if 来处 else ''}{正文}")
    return "\n".join(段)

TOOL_READ_TRUNCATED_MARK = (
    "\n\n…〔截断了：原文 {n} 字，这里只留了前 {k} 字。"
    "要看全的就再查一次：卡面用 `recall`，账本原话用 `read_original`，按词搜原文用 `search_ledger`。〕"
)

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

def her_now(cfg=None):
    off = int((load_config() if cfg is None else cfg).get("local_utc_offset_hours", 8))
    当地 = to_local(datetime.now(timezone.utc).isoformat(), off)
    周 = _WEEKDAYS[datetime.strptime(当地[:10], "%Y-%m-%d").weekday()]
    return f"{当地[:10]} {周} {当地[11:]}"

def 这扇窗上一轮用的effort(conn, cfg=None):
    cfg = load_config() if cfg is None else cfg
    conv_id = _current_conversation(conn)
    row = conn.execute(
        "SELECT effort FROM messages WHERE conversation_id = ? AND effort IS NOT NULL "
        "ORDER BY id DESC LIMIT 1", (conv_id,)
    ).fetchone()
    return row["effort"] if row else None

def _system_prompt(cfg=None):
    cfg = load_config() if cfg is None else cfg
    p = Path(cfg.get("v3", {}).get("system_prompt_file") or "他的运行时/系统提示词.md")
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    if not p.is_file():
        raise RuntimeError(
            f"系统提示词正本不在：{p} —— 拒绝启动。"
            "回落到官方那份的话，「换成功」和「换失败」长得一模一样，不会有任何东西报错。")
    return p.read_text(encoding="utf-8").strip()

def _opening_line(conn, cfg=None):
    cfg = load_config() if cfg is None else cfg
    天 = resident_index.days_together(conn, cfg)
    认识 = f"，你和她认识第 {天} 天" if 天 else ""
    现在 = f"（现在是 {her_now(cfg)}，她那边的时间{认识}。"
    有过 = conn.execute("SELECT 1 FROM messages WHERE role='user' LIMIT 1").fetchone()
    if not 有过:
        return 现在 + "）"
    return 现在 + resident_index.last_ending(conn, cfg) + "）"

def _compose(pulled, text, 卡号们=None, opening="", own_notes=""):
    parts = []
    if opening:
        parts.append(opening)
    block = pull_cards.render(pulled, 卡号们=卡号们)
    if block:
        parts.append(block)
    if _recall_status(pulled) == "hit":
        parts.append(CARD_USAGE_NOTE)
        parts.append(ABILITY_NOTE)
    if own_notes:
        parts.append(own_notes)
    parts.append(text)
    return "\n\n".join(parts)

def _这一轮用哪个模型(conn, conv_id, c):
    登记的 = handover.window_model(conn, conv_id)
    if 登记的:
        return 登记的, "这扇窗换窗时选的"
    配置的 = (c.get("model") or "").strip()
    if 配置的:
        print(f"⚠️ 这扇窗（对话 {conv_id}）没登记模型 ⇒ 用配置里写死的 `{配置的}`。"
              f"\n   ")
        return 配置的, "config.json 的 chat.model"
    print(f"⚠️ 这扇窗（对话 {conv_id}）没登记模型、配置里也没写 ⇒ **用写死的兜底 "
          f"`{窗口模型兜底}`**。\n"
          f"   🔴 **这一行是故意吵的**：设计上明令不许走 `claude` 命令行的默认"
          f"（那个默认哪天变了，他就在她不知道的情况下换了个人，而没有任何东西会提醒）"
          f"\n   ⇒ 走一趟换窗，她选的那个就会落到这扇窗自己身上。")
    return 窗口模型兜底, f"写死的兜底（{窗口模型兜底}）—— 这扇窗没登记过模型"

def _current_conversation(conn):
    row = conn.execute(
        "SELECT id FROM conversations WHERE source_end='frontend' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        return row["id"]
    return store.ensure_conversation(conn, "frontend")
