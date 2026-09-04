import html as _html
import json
import re
import threading
import time

from .config import load_config
from .db import get_conn, get_watermark, set_watermark, write_session

TG_TEXT_LIMIT = 4096

PER_CHAT_INTERVAL = 1.05

TYPING_REFRESH = 4.0

TOOL_SPOILER_BUDGET = 700

UPDATE_MARK = "tg:last_update_id"
MIRROR_MARK = "tg:mirrored_msg_id"

MIRRORED_SOURCES = ("web",)

SPLIT_STYLES = ("paragraph", "newline")

class TgConfigError(Exception):
    pass

class TelegramError(RuntimeError):
    pass

def settings(cfg=None):
    cfg = load_config() if cfg is None else cfg
    tg = dict(cfg.get("telegram") or {})
    token = str(tg.get("token") or "").strip()
    raw_ids = tg.get("allowed_chat_ids")
    if isinstance(raw_ids, (int, str)):
        raw_ids = [raw_ids]
    raw_ids = list(raw_ids or [])

    问题 = []
    if not token:
        问题.append("token（BotFather 给的那串，形如 `123456:AA…`）")
    elif token.lower() in ("change-me", "changeme", "your-token") or "your" in token.lower():
        问题.append(f"token 还是占位符（{token!r}）")
    if not raw_ids:
        问题.append("allowed_chat_ids（🔴 白名单**必须至少一个**，空着 ＝ 谁都能进）")

    风格 = str(tg.get("split_style") or "paragraph")
    if 风格 not in SPLIT_STYLES:
        问题.append(f"split_style 只认 {SPLIT_STYLES}，填的是 {风格!r}")

    ids = []
    for x in raw_ids:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            问题.append(f"allowed_chat_ids 里这个不是数字：{x!r}")

    if 问题:
        raise TgConfigError(
            "🔴 `config.json` 的 `telegram` 段不全 —— **拒绝启动**：\n"
            + "".join(f"  · 缺／错：{p}\n" for p in 问题)
            + "怎么办（三步，`config.example.json` 里有同样的一段可以照抄）：\n"
            "  ① 在 Telegram 里找 @BotFather → `/newbot` → 它给你一串 token\n"
            "  ② 把那串填进 `config.json` 的 `telegram.token`"
            "（🔴 `config.json` 在 `.gitignore` 里，**token 不进 git**）\n"
            "  ③ 给你自己的 bot 发一句话，然后跑 "
            "`python -m nacre.tg_bridge --whoami`，\n"
            "     它会把 `chat_id` 打出来 —— 填进 `telegram.allowed_chat_ids`。\n"
            "⚠️ **白名单空着不许启动**：桥一旦跑起来，任何人给这个 bot 说话都会**真跑一发 `-p`**，"
            "烧的是她的订阅额度。"
        )

    return {
        "token": token,
        "allowed_chat_ids": ids,
        "poll_timeout": int(tg.get("poll_timeout") or 30),
        "split_min_chars": int(tg.get("split_min_chars") or 120),
        "split_max_messages": int(tg.get("split_max_messages") or 4),
        "split_style": 风格,
        "show_thinking": bool(tg.get("show_thinking", True)),
        "show_tools": bool(tg.get("show_tools", True)),
        "show_usage": bool(tg.get("show_usage", True)),
        "tool_spoiler_budget": int(tg.get("tool_spoiler_budget") or TOOL_SPOILER_BUDGET),
        "mirror_web": bool(tg.get("mirror_web", True)),
        "use_rich": bool(tg.get("use_rich", True)),
        "proxy": tg.get("proxy") or ((cfg or {}).get("chat") or {}).get("proxy") or None,
    }

def _遮token(s):
    return re.sub(r"/bot\d+:[A-Za-z0-9_-]+", "/bot<token>", s or "")

class HttpTelegram:

    def __init__(self, token, base="https://api.telegram.org", timeout=60, proxy=None):
        self._url = f"{base.rstrip('/')}/bot{token}"
        self._timeout = timeout
        self._proxy = proxy

    NET_RETRY = 3

    def _call(self, method, **params):
        import requests
        proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
        r = None
        for 第几次 in range(self.NET_RETRY):
            try:
                r = requests.post(f"{self._url}/{method}", json=params,
                                  timeout=self._timeout, proxies=proxies)
                break
            except requests.exceptions.RequestException as e:
                if 第几次 == self.NET_RETRY - 1:
                    raise TelegramError(
                        f"{method} 连了 {self.NET_RETRY} 次都没通："
                        f"{type(e).__name__}: {_遮token(str(e))}\n"
                        "  🔴 这条路必须走代理（api.telegram.org 直连不通）⇒ **先看代理还开着没有**。\n"
                        "  ⚠️ 代理断了的症状是【她发的消息像石沉大海】，而 TG 那边显示「已送达」。"
                    ) from e
                time.sleep(1.5 * (第几次 + 1))
        try:
            body = r.json()
        except ValueError:
            raise TelegramError(f"{method} 返回的不是 JSON（HTTP {r.status_code}）：{r.text[:200]}")
        if not body.get("ok"):
            raise TelegramError(json.dumps(body, ensure_ascii=False))
        return body["result"]

    def get_updates(self, offset=None, timeout=30):
        return self._call("getUpdates", offset=offset, timeout=timeout,
                          allowed_updates=["message"])

    def send_message(self, chat_id, text, parse_mode=None):
        kw = {"chat_id": chat_id, "text": text}
        if parse_mode:
            kw["parse_mode"] = parse_mode
        return self._call("sendMessage", **kw)

    def send_rich_message(self, chat_id, blocks):
        return self._call("sendRichMessage", chat_id=chat_id,
                          rich_message={"blocks": blocks})

    def send_chat_action(self, chat_id, action="typing"):
        return self._call("sendChatAction", chat_id=chat_id, action=action)

def esc(text):
    return _html.escape(str(text), quote=False)

_FENCE = re.compile(r"^\s*```")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}(?:&gt;|>)\s?(.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)

def md_to_tg_html(text):
    坑 = []

    def 存(inner_html):
        坑.append(inner_html)
        return f"\x00{len(坑) - 1}\x00"

    def 代码块(m):
        body = m.group(2)
        return 存(f"<pre><code>{esc(body)}</code></pre>")

    out = re.sub(r"```([^\n`]*)\n(.*?)```", 代码块, text, flags=re.S)
    out = re.sub(r"```(.*?)```", lambda m: 存(f"<pre>{esc(m.group(1))}</pre>"), out, flags=re.S)
    out = re.sub(r"`([^`\n]+)`", lambda m: 存(f"<code>{esc(m.group(1))}</code>"), out)

    out = esc(out)
    out = _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", out)

    行 = []
    引用 = []

    def 收引用():
        if 引用:
            行.append("<blockquote>" + "\n".join(引用) + "</blockquote>")
            引用.clear()

    for line in out.split("\n"):
        q = _QUOTE.match(line)
        if q:
            引用.append(q.group(1))
            continue
        收引用()
        h = _HEADING.match(line)
        行.append(f"<b>{h.group(2)}</b>" if h else line)
    收引用()
    out = "\n".join(行)

    for i, inner in enumerate(坑):
        out = out.replace(f"\x00{i}\x00", inner)
    return out

def _blocks(text):
    块, 手上, 栅栏中 = [], [], False
    for line in text.split("\n"):
        if _FENCE.match(line):
            if not 栅栏中:
                if 手上:
                    块.append("\n".join(手上))
                    手上 = []
                栅栏中 = True
                手上.append(line)
            else:
                手上.append(line)
                块.append("\n".join(手上))
                手上, 栅栏中 = [], False
            continue
        if 栅栏中 or line.strip():
            手上.append(line)
            continue
        if 手上:
            块.append("\n".join(手上))
            手上 = []
    if 手上:
        块.append("\n".join(手上))
    return [b for b in 块 if b.strip()]

def _hard_split(s, limit):
    出 = []
    while len(s) > limit:
        窗 = s[:limit]
        切 = max(窗.rfind("\n"), 0)
        if 切 < limit // 2:
            m = None
            for m in re.finditer(r"[。！？!?；;…]", 窗):
                pass
            切 = m.end() if m else 0
        if 切 <= 0:
            切 = limit
        出.append(s[:切].strip())
        s = s[切:].lstrip()
    if s.strip():
        出.append(s.strip())
    return 出

def split_message(text, limit=TG_TEXT_LIMIT, min_chars=120, max_messages=4):
    text = (text or "").strip()
    if not text:
        return []
    块 = _blocks(text) or [text]

    if min_chars:
        并 = []
        for b in 块:
            if 并 and (len(并[-1]) < min_chars or len(b) < min_chars) \
                    and len(并[-1]) + 2 + len(b) <= limit:
                并[-1] = 并[-1] + "\n\n" + b
            else:
                并.append(b)
        块 = 并

    if max_messages and len(块) > max_messages:
        并 = []
        for b in 块:
            if 并 and len(并) + (len(块) - 块.index(b)) > max_messages \
                    and len(并[-1]) + 2 + len(b) <= limit:
                并[-1] = 并[-1] + "\n\n" + b
            else:
                并.append(b)
        块 = 并

    出 = []
    for b in 块:
        出.extend(_hard_split(b, limit) if len(b) > limit else [b])
    return 出

def split_by_newline(text, limit=TG_TEXT_LIMIT):
    出 = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        出.extend(_hard_split(line, limit) if len(line) > limit else [line])
    return 出

def render_thinking(thinking, limit=TG_TEXT_LIMIT):
    t = (thinking or "").strip()
    if not t:
        return None
    return _裹住(t, "<blockquote expandable>💭 ", "…（还有，网页里看得全）", limit)

def render_recall(recall, only_failure=False):
    if not recall:
        return None
    status = recall.get("status")
    if only_failure and status != "broken":
        return None
    if status == "broken":
        原因 = recall.get("error") or "原因没带回来"
        return f"🔴 检索没查成（不是没查到）：{esc(原因)}"
    if status == "skipped":
        return None
    if status == "empty":
        return "🔎 查了，这次没翻到相关的"
    n = recall.get("hit_total") or recall.get("total") or 0
    共 = recall.get("hit_total")
    给 = recall.get("total") or 0
    if 共 and 共 > 给:
        return f"🔎 翻到 {共} 条，带了 {给} 条给他"
    return f"🔎 翻到 {n} 条"

def cap_spoiler(text, budget):
    t = str(text or "")
    if not budget or budget <= 0 or len(t) <= budget:
        return t
    for keep in range(budget, -1, -1):
        尾 = f"…（还有 {len(t) - keep} 字没显示，网页那边看得全）"
        if keep + len(尾) <= budget:
            return t[:keep] + 尾
    return t[:budget]

def render_tools(tools, tools_error=None, budget=TOOL_SPOILER_BUDGET):
    if tools_error:
        return ("🔧 <tg-spoiler>🔴 这一轮的工具记录【读不到】（不是「他没调工具」）——\n"
                f"{esc(cap_spoiler(str(tools_error), budget))}\n"
                "工具的输入与结果正文只活在会话文件里，账本里没有第二份。</tg-spoiler>")
    if not tools:
        return None
    记 = {"ok": "✓", "error": "✗", "unknown": "…没看到结果"}
    名单 = " · ".join(f"{esc(t.get('name') or '?')} {记.get(t.get('state'), '·')}"
                      for t in tools)
    每件 = max(80, budget // max(1, len(tools))) if budget else 0
    段 = []
    for t in tools:
        进 = str(t.get("input") or "").strip()
        出 = str(t.get("result") or "").strip()
        行 = [f"— {esc(t.get('name') or '?')} {记.get(t.get('state'), '·')}"]
        行.append("　看了：" + (esc(cap_spoiler(进, 每件)) if 进 else "（没带参数）"))
        if t.get("body_error"):
            行[-1] = "　看了：🔴 读不到（" + esc(str(t["body_error"])) + "）"
            行.append("　拿到：🔴 读不到（同上）")
        else:
            行.append("　拿到：" + (esc(cap_spoiler(出, 每件)) if 出 else
                                   ("【没看到结果】（不是空结果 —— 多半是这一件的结果"
                                    "落在下一份会话文件里）"
                                    if t.get("state") == "unknown" else "（空）")))
        段.append("\n".join(行))
    return f"🔧 {名单}\n<tg-spoiler>" + "\n".join(段) + "</tg-spoiler>"

def render_usage(u):
    if u is None:
        return ("💰 <tg-spoiler>这一轮的用量【没记上】（不是 0，是没查到）—— "
                "turn_usage 里没有认到他这句话上的行。</tg-spoiler>")

    def 取(键):
        try:
            v = u[键]
        except (KeyError, IndexError, TypeError):
            return None
        return v

    def 数(键):
        v = 取(键)
        return "取不到" if v is None else f"{int(v):,}"

    钱 = 取("cost_usd")
    钱文 = "花费取不到" if 钱 is None else f"${float(钱):.4f}"
    return ("💰 <tg-spoiler>缓存 命中 {命中} · 重建 {重建} · "
            "新读 {入} · 出 {出} · {钱}</tg-spoiler>").format(
        命中=数("cache_read"), 重建=数("cache_write"),
        入=数("input_tokens"), 出=数("output_tokens"), 钱=钱文)

def render_echo(text, limit=TG_TEXT_LIMIT):
    return _裹住((text or "").strip(), "<blockquote>🗣 ", "…", limit)

def _裹住(body, 前缀, 截断记号, limit):
    尾 = "</blockquote>"

    def 包(s, 带记号):
        return f"{前缀}{esc(s)}{截断记号 if 带记号 else ''}{尾}"

    out = 包(body, False)
    if len(out) <= limit:
        return out
    lo, hi = 0, len(body)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(包(body[:mid], True)) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return 包(body[:lo].rstrip(), True)

def render_failure(err, where="这一轮"):
    因 = f"{type(err).__name__}: {err}" if isinstance(err, BaseException) else str(err)
    因 = 因.strip().replace("\n", " ")
    if len(因) > 500:
        因 = 因[:500] + "…"
    return (f"🔴 {where}没跑成（不是慢，是真的没跑）。\n"
            f"原因：{esc(因)}\n"
            f"你可以再发一遍这句；连着两次一样的错就是桥那边坏了，去看终端里的日志。")

class _Typing:

    def __init__(self, api, chat_id, every=TYPING_REFRESH, enabled=True):
        self._api, self._chat_id, self._every = api, chat_id, every
        self._enabled = enabled
        self._stop = threading.Event()
        self._t = None

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._api.send_chat_action(self._chat_id, "typing")
            except Exception as e:
                print(f"⚠️ 「他在想…」没发出去（这一轮照常跑）：{type(e).__name__}: {e}")
                return
            self._stop.wait(self._every)

    def __enter__(self):
        if self._enabled:
            self._t = threading.Thread(target=self._loop, daemon=True)
            self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        if self._t:
            self._t.join(timeout=2)
        return False

class Sender:

    def __init__(self, api, interval=PER_CHAT_INTERVAL, sleep=None, now=None):
        self._api = api
        self._interval = interval
        self._sleep = sleep or time.sleep
        self._now = now or time.monotonic
        self._last = None

    def _gap(self):
        if self._last is None or not self._interval:
            return
        等 = self._interval - (self._now() - self._last)
        if 等 > 0:
            self._sleep(等)

    def send(self, chat_id, text, parse_mode="HTML"):
        self._gap()
        try:
            r = self._api.send_message(chat_id, text, parse_mode=parse_mode)
        except TelegramError as e:
            秒 = _retry_after(e)
            if 秒 is not None:
                print(f"⏳ TG 限流了，等 {秒}s 再发（单聊 1 条/秒是它的规矩）")
                self._sleep(秒)
                self._last = self._now()
                return self._api.send_message(chat_id, text, parse_mode=parse_mode)
            if parse_mode and "parse" in str(e).lower():
                print(f"🔴 HTML 排版被 TG 拒了，降级成纯文本重发（**这是 bug，要修**）：{e}")
                self._last = self._now()
                return self._api.send_message(chat_id, _strip_tags(text), parse_mode=None)
            raise
        self._last = self._now()
        return r

    def send_rich(self, chat_id, blocks):
        self._gap()
        try:
            r = self._api.send_rich_message(chat_id, blocks)
        except TelegramError as e:
            秒 = _retry_after(e)
            if 秒 is None:
                raise
            print(f"⏳ TG 限流了，等 {秒}s 再发（单聊 1 条/秒是它的规矩）")
            self._sleep(秒)
            self._last = self._now()
            return self._api.send_rich_message(chat_id, blocks)
        self._last = self._now()
        return r

    def send_all(self, chat_id, texts, parse_mode="HTML"):
        return [self.send(chat_id, t, parse_mode=parse_mode) for t in texts if t and t.strip()]

def _retry_after(err):
    try:
        body = json.loads(str(err))
    except (ValueError, TypeError):
        return None
    if body.get("error_code") != 429:
        return None
    return (body.get("parameters") or {}).get("retry_after")

def _strip_tags(s):
    s = re.sub(r"<[^>]+>", "", s)
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&amp;", "&"))

class Bridge:

    def __init__(self, db_path, chat_cfg, tg_cfg, api, cfg_all=None, say=None,
                 sender=None, typing=True):
        from . import chat_api
        self.db_path = db_path
        self.c = chat_cfg
        self.tg = tg_cfg
        self.api = api
        self.cfg_all = cfg_all
        self.say = say or chat_api.say_once
        self.sender = sender or Sender(api)
        self.typing = typing
        self.split_style = tg_cfg["split_style"]
        self.chat_id = tg_cfg["allowed_chat_ids"][0]

    def _read(self):
        return get_conn(self.db_path)

    def _effort(self):
        from . import chat_api
        try:
            with self._read() as conn:
                上一轮 = chat_api.这扇窗上一轮用的effort(conn, self.cfg_all)
        except Exception as e:
            print(f"⚠️ TG 读不到上一轮的 effort（{e}）⇒ 退回配置里的默认。"
                  f"\n   这意味着这一轮可能跟网页那侧不一致 ⇒ 缓存会重建一次。")
            上一轮 = None
        return 上一轮 or self.c.get("default_effort") or "high"

    def _mark(self, key, value):
        with write_session(self.db_path, quiet=True) as conn:
            set_watermark(conn, key, value)

    def pump_updates(self):
        conn = self._read()
        try:
            last = get_watermark(conn, UPDATE_MARK)
        finally:
            conn.close()
        offset = int(last) + 1 if last not in (None, "") else None
        updates = self.api.get_updates(offset=offset, timeout=self.tg["poll_timeout"]) or []
        for u in updates:
            try:
                self._handle(u)
            finally:
                self._mark(UPDATE_MARK, u["update_id"])
        return len(updates)

    def _handle(self, update):
        msg = update.get("message")
        if not msg:
            return
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id not in self.tg["allowed_chat_ids"]:
            print(f"⛔ 白名单外的 chat_id={chat_id!r}，静默不响应")
            return
        text = (msg.get("text") or "").strip()
        if not text:
            种类 = next((k for k in ("photo", "voice", "video", "document", "sticker",
                                     "audio", "animation", "location") if k in msg), "非文字")
            self.sender.send(chat_id, render_failure(
                f"收到的是{种类}，桥这一版只认文字", where="这条"))
            return

        idem = f"tg:{chat_id}:{msg['message_id']}"
        with _Typing(self.api, chat_id, enabled=self.typing):
            try:
                out = self.say(self.db_path, self.c, text,
                               effort=self._effort(),
                               cfg_all=self.cfg_all, source="tg", idem_key=idem)
            except Exception as e:
                self.sender.send(chat_id, render_failure(e))
                return
        self.send_turn(chat_id, out)
        if out.get("reply_id"):
            self._mark(MIRROR_MARK, out["reply_id"])

    def send_turn(self, chat_id, out):
        if self.tg.get("use_rich", True):
            try:
                return self.send_turn_rich(chat_id, out)
            except TelegramError as e:
                print(f"🔴 rich 那条路被拒了，这一轮退回 HTML 旧路径（**这是 bug，要修**）：{e}")
        return self.send_turn_html(chat_id, out)

    def send_turn_rich(self, chat_id, out):
        from . import tg_rich
        mid = out.get("reply_id")
        usage = self._turn_usage(mid) if self.tg.get("show_usage") else None
        r = out.get("recall") or {}
        状态 = r.get("status")
        失败 = (("这轮的记忆检索没跑成", r.get("error") or f"recall {状态}")
                if 状态 and 状态 not in ("hit", "empty") else None)
        消息 = tg_rich.build_turn(
            out.get("text"),
            thinking=out.get("thinking") if self.tg.get("show_thinking") else None,
            usage=usage,
            tools=out.get("tools") if self.tg.get("show_tools") else None,
            tools_error=out.get("tools_error") if self.tg.get("show_tools") else None,
            明细=self._明细(out, mid) if self.tg.get("show_tools") else None,
            失败=失败)
        return [self.sender.send_rich(chat_id, b) for b in 消息]

    def _明细(self, out, message_id):
        出 = {}
        卡 = self._turn_cards(message_id)
        if 卡:
            出[("💡", "想起")] = 卡
        for t in out.get("tools") or []:
            from . import tg_rich
            格 = tg_rich.认(t.get("name"))
            if 格["emoji"] == "💡" and 卡:
                continue
            行 = 出.setdefault((格["emoji"], 格["verb"]), [])
            if t.get("body_error"):
                行.append(f"{t.get('name')} · 🔴 正文读不到：{t['body_error']}")
                continue
            简 = (t.get("input") or t.get("result") or "").replace("\n", " ").strip()
            行.append(f"{t.get('name')} · {t.get('state') or '?'}"
                      + (f"　{简[:60]}" if 简 else ""))
        return 出

    def _turn_cards(self, message_id):
        if message_id is None:
            return []
        conn = self._read()
        try:
            m = conn.execute("SELECT turn_id FROM turn_meta WHERE message_id=?",
                             (message_id,)).fetchone()
            if not m:
                return []
            rows = conn.execute(
                "SELECT mm.occurred_at, mm.src_quote FROM turn_handles h "
                "JOIN memories mm ON mm.id = h.memory_id "
                "WHERE h.turn_id=? ORDER BY h.slot", (m["turn_id"],)).fetchall()
            return ["{}  「{}」".format((x["occurred_at"] or "")[:10],
                                        (x["src_quote"] or "")[:30]) for x in rows]
        except Exception as e:
            print(f"⚠️ 这一轮的卡没查上：{type(e).__name__}: {e}")
            return []
        finally:
            conn.close()

    def send_turn_html(self, chat_id, out):
        条 = []
        if self.tg.get("show_thinking"):
            条.append(render_thinking(out.get("thinking")))
        正文 = (split_by_newline(out.get("text") or "")
                if self.split_style == "newline"
                else split_message(out.get("text") or "",
                                   min_chars=self.tg["split_min_chars"],
                                   max_messages=self.tg["split_max_messages"]))
        正文 = [md_to_tg_html(x) for x in 正文]

        mid = out.get("reply_id")
        脚注 = [render_recall(out.get("recall"), only_failure=True)]
        if self.tg.get("show_usage"):
            脚注.append(render_usage(self._turn_usage(mid)))
        if self.tg.get("show_tools"):
            脚注.append(render_tools(out.get("tools"), out.get("tools_error"),
                                     self.tg["tool_spoiler_budget"]))
        脚注 = [x for x in 脚注 if x]

        尾 = "\n".join(脚注)
        if 正文 and 尾:
            if len(正文[-1]) + 2 + len(尾) <= TG_TEXT_LIMIT:
                正文[-1] = 正文[-1] + "\n\n" + 尾
            else:
                正文.append(尾)
        elif 尾:
            正文 = [尾]
        条.extend(正文)
        return self.sender.send_all(chat_id, [x for x in 条 if x])

    def _turn_usage(self, message_id):
        if message_id is None:
            return None
        conn = self._read()
        try:
            return conn.execute(
                "SELECT cache_read, cache_write, input_tokens, output_tokens, cost_usd, model "
                "FROM turn_usage WHERE message_id=? ORDER BY id DESC LIMIT 1",
                (message_id,)).fetchone()
        except Exception as e:
            print(f"⚠️ 用量那一行没查上：{type(e).__name__}: {e}")
            return None
        finally:
            conn.close()

    def _init_mirror_mark(self, conn):
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()
        return int(row[0])

    def pump_mirror(self):
        if not self.tg.get("mirror_web"):
            return 0
        conn = self._read()
        try:
            last = get_watermark(conn, MIRROR_MARK)
            if last in (None, ""):
                起点 = self._init_mirror_mark(conn)
                self._mark(MIRROR_MARK, 起点)
                print(f"📍 镜像水位线第一次对齐到 messages.id={起点}（**一条历史都不推**）")
                return 0
            占位 = ",".join("?" * len(MIRRORED_SOURCES))
            rows = conn.execute(
                f"SELECT id, role, content, thinking FROM messages "
                f"WHERE id > ? AND source IN ({占位}) ORDER BY id",
                tuple([int(last)] + list(MIRRORED_SOURCES)),
            ).fetchall()
            料 = [(r["id"], r["role"], r["content"], r["thinking"],
                   self._turn_extras(conn, r["id"]) if r["role"] == "assistant" else None)
                  for r in rows]
        finally:
            conn.close()

        推了 = 0
        for mid, role, content, thinking, extras in 料:
            if role == "user":
                self.sender.send(self.chat_id, render_echo(content))
            else:
                self.send_turn(self.chat_id, {"text": content, "thinking": thinking,
                                              "reply_id": mid, **(extras or {})})
            self._mark(MIRROR_MARK, mid)
            推了 += 1
        return 推了

    def _turn_extras(self, conn, message_id):
        from . import chat_api
        m = conn.execute(
            "SELECT recall_status, recall_total, recall_error FROM turn_meta WHERE message_id=?",
            (message_id,)).fetchone()
        recall = ({"status": m["recall_status"], "total": m["recall_total"],
                   "error": m["recall_error"]} if m else None)
        cap = chat_api.tool_body_cap(self.cfg_all)
        history_dir = (self.c or {}).get("history_dir") or ""
        tools = []
        for r in conn.execute(
                "SELECT tool, ok, result_state, call_id, session_path FROM tool_calls "
                "WHERE message_id=? ORDER BY id", (message_id,)):
            件 = {"name": r["tool"],
                  "state": r["result_state"] or ("ok" if r["ok"] else "error")}
            try:
                正 = chat_api.read_tool_body(r["call_id"], r["session_path"], history_dir, cap)
                件["input"], 件["result"] = 正.get("input"), 正.get("result")
            except Exception as e:
                件["body_error"] = f"{type(e).__name__}: {e}".replace("\n", " ")[:200]
            tools.append(件)
        return {"recall": recall, "tools": tools}

    def run_forever(self, once=False):
        while True:
            try:
                self.pump_updates()
                self.pump_mirror()
            except Exception as e:
                if once:
                    raise
                print(f"⚠️ 这一轮出错了，桥继续跑：{type(e).__name__}: {e}", flush=True)
                time.sleep(3)
            if once:
                return

def build(cfg=None, db_path=None, api=None, **kw):
    from . import chat_api
    cfg = load_config() if cfg is None else cfg
    tg = settings(cfg)
    c = chat_api._settings(cfg)
    for w in chat_api._check_recall_config(cfg):
        print(w)
    db_path = db_path or cfg.get("db_path")
    api = api or HttpTelegram(tg["token"], proxy=tg.get("proxy"))
    return Bridge(db_path, c, tg, api, cfg_all=cfg, **kw)

def whoami(cfg=None):
    cfg = load_config() if cfg is None else cfg
    tg = settings({**cfg, "telegram": {**(cfg.get("telegram") or {}),
                                       "allowed_chat_ids": [0]}})
    api = HttpTelegram(tg["token"])
    updates = api.get_updates(offset=None, timeout=0) or []
    见到 = {}
    for u in updates:
        chat = ((u.get("message") or {}).get("chat") or {})
        if chat.get("id") is not None:
            见到[chat["id"]] = chat.get("username") or chat.get("first_name") or ""
    if not 见到:
        print("没看到任何消息 —— 先在 Telegram 里给你的 bot 发一句话，再跑一次这条命令。")
        return {}
    for cid, who in 见到.items():
        print(f"chat_id = {cid}    （{who}）")
    print("\n把上面那个数字填进 config.json 的 telegram.allowed_chat_ids，例如：[%s]"
          % ", ".join(str(c) for c in 见到))
    return 见到

def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if "--whoami" in argv:
            whoami()
            return 0
        bridge = build()
    except TgConfigError as e:
        print(e)
        return 2
    print(f"✅ TG 桥起来了（白名单 {bridge.tg['allowed_chat_ids']}，"
          f"切分 {bridge.split_style}）。Ctrl-C 退出。")
    try:
        bridge.run_forever(once="--once" in argv)
    except KeyboardInterrupt:
        print("\n再见。")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
