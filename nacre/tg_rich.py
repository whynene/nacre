import re

_INLINE = [
    (re.compile(r"\*\*(.+?)\*\*", re.S), "bold"),
    (re.compile(r"__(.+?)__", re.S), "bold"),
    (re.compile(r"`([^`\n]+?)`"), "code"),
    (re.compile(r"~~(.+?)~~", re.S), "strikethrough"),
    (re.compile(r"(?<![\*\w])\*([^*\n]+?)\*(?![\*\w])"), "italic"),
]

def md_to_inline(text):
    片 = [text or ""]
    for 正则, 类型 in _INLINE:
        新 = []
        for x in 片:
            if not isinstance(x, str):
                新.append(x)
                continue
            上 = 0
            for m in 正则.finditer(x):
                if m.start() > 上:
                    新.append(x[上:m.start()])
                新.append({"type": 类型, "text": m.group(1)})
                上 = m.end()
            if 上 < len(x):
                新.append(x[上:])
        片 = [y for y in 新 if y != ""]
    if not 片:
        return ""
    if len(片) == 1 and isinstance(片[0], str):
        return 片[0]
    return 片

def 段(text):
    return {"type": "paragraph", "text": md_to_inline(text)}

_H = re.compile(r"^(#{1,6})\s+(.*)$")
_UL = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_Q = re.compile(r"^\s*>\s?(.*)$")

def md_to_blocks(text):
    行 = (text or "").split("\n")
    块, 缓冲, 列表, 引用 = [], [], [], []

    def 冲():
        if 缓冲:
            块.append(段("\n".join(缓冲)))
            缓冲.clear()

    def 冲列表():
        if 列表:
            块.append({"type": "list", "items": [
                {"label": lab, "blocks": [段(t)]} for lab, t in 列表]})
            列表.clear()

    def 冲引用():
        if 引用:
            块.append({"type": "blockquote", "blocks": [段(x) for x in 引用]})
            引用.clear()

    for ln in 行:
        m = _H.match(ln)
        if m:
            冲(); 冲列表(); 冲引用()
            块.append({"type": "heading", "text": md_to_inline(m.group(2)),
                       "size": min(6, max(1, len(m.group(1))))})
            continue
        m = _OL.match(ln)
        if m:
            冲(); 冲引用()
            列表.append((m.group(1) + ".", m.group(2)))
            continue
        m = _UL.match(ln)
        if m:
            冲(); 冲引用()
            列表.append(("•", m.group(1)))
            continue
        m = _Q.match(ln)
        if m:
            冲(); 冲列表()
            引用.append(m.group(1))
            continue
        冲列表(); 冲引用()
        缓冲.append(ln)
    冲(); 冲列表(); 冲引用()
    return 块 or [段("")]

TOOLS = {
    "recall":        {"emoji": "💡", "verb": "想起",   "writes": False, "from": "ledger"},
    "read_original": {"emoji": "📖", "verb": "读原文", "writes": False, "from": "ledger"},
    "keep":          {"emoji": "✍️", "verb": "写下",   "writes": True,  "from": "ledger"},
    "stance":        {"emoji": "🖊", "verb": "表态",   "writes": True,  "from": "ledger"},
    "want_to_read":  {"emoji": "🔖", "verb": "想读",   "writes": True,  "from": "ledger"},
    "go_again":      {"emoji": "🔁", "verb": "再看",   "writes": True,  "from": "ledger"},
    "WebSearch":     {"emoji": "🌐", "verb": "搜了",   "writes": False, "from": "web"},
    "WebFetch":      {"emoji": "🌐", "verb": "看了",   "writes": False, "from": "web"},
    "ToolSearch":    {"emoji": "🧰", "verb": "取工具", "writes": False, "from": "harness"},
}

未登记 = {"emoji": "⚠️", "verb": "未登记的工具", "writes": None, "from": "unknown"}

def 认(tool):
    名 = (tool or "").split("__")[-1]
    return TOOLS.get(名) or TOOLS.get(tool or "") or dict(未登记, verb=f"未登记的工具：{名 or '?'}")

def 分组(tools):
    出 = []
    for t in tools or []:
        名 = t.get("name") if isinstance(t, dict) else str(t)
        格 = 认(名)
        for g in 出:
            if g[0]["emoji"] == 格["emoji"] and g[0]["verb"] == 格["verb"]:
                g[1].append(名)
                break
        else:
            出.append((格, [名]))
    return [(格, len(名单), 名单) for 格, 名单 in 出]

def emoji_行(组, recall_total=None):
    return "".join(g[0]["emoji"] for g in 组)

def 下钻块(组, 明细, summary="..."):
    引块 = []
    for 格, n, 名单 in 组:
        行 = 明细.get((格["emoji"], 格["verb"])) or [f"{名}" for 名 in 名单]
        引块.append({"type": "blockquote",
                     "blocks": [段(x) for x in 行],
                     "credit": "{} {} · {} 条".format(格["emoji"], 格["verb"], n)})
    if not 引块:
        return None
    return {"type": "details", "summary": summary, "blocks": 引块}

def thinking_块(thinking, usage=None):
    t = (thinking or "").strip()
    if not t:
        return None
    块 = {"type": "blockquote",
          "blocks": [段(p.strip()) for p in re.split(r"\n\s*\n", t) if p.strip()]}
    if usage is not None:
        命中 = usage["cache_read"] if not isinstance(usage, dict) else usage.get("cache_read")
        重建 = usage["cache_write"] if not isinstance(usage, dict) else usage.get("cache_write")
        if 命中 is not None and 重建 is not None:
            块["credit"] = "缓存 命中 {:,} · 重建 {:,}".format(int(命中), int(重建))
    return {"type": "details", "summary": "💭", "blocks": [块]}

def 失败块(说明, 原因):
    return {"type": "blockquote",
            "blocks": [段(f"**⚠️ {说明}**"),
                       段("不是「他没去查」——是查了、路上断了。他上面那句话没有记忆作支撑。")],
            "credit": 原因}

def 贴emoji(blocks, e):
    if not e:
        return
    for b in reversed(blocks):
        t = b.get("type")
        if t in ("paragraph", "heading"):
            b["text"] = (b["text"] + e) if isinstance(b["text"], str) else list(b["text"]) + [e]
            return
        if t == "list" and b.get("items"):
            内 = b["items"][-1].get("blocks") or []
            for x in reversed(内):
                if x.get("type") == "paragraph":
                    x["text"] = (x["text"] + e) if isinstance(x["text"], str) else list(x["text"]) + [e]
                    return
    blocks.append(段(e))

def build_turn(text, thinking=None, usage=None, tools=None, tools_error=None,
               明细=None, 失败=None):
    段落 = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    消息 = [md_to_blocks(p) for p in 段落] or [[段("")]]

    头 = thinking_块(thinking, usage)
    if 头:
        消息[0].insert(0, 头)

    if 失败:
        消息[-1].append(失败块(*失败))

    组 = 分组(tools)
    if tools_error:
        组 = 组 + [({"emoji": "⚠️", "verb": "工具记录读不到", "writes": None,
                     "from": "unknown"}, 1, [])]
        明细 = dict(明细 or {})
        明细[("⚠️", "工具记录读不到")] = [str(tools_error)]
    if 组:
        尾 = 消息[-1]
        贴emoji(尾, emoji_行(组))
        块 = 下钻块(组, 明细 or {})
        if 块:
            尾.append(块)
    return 消息
