import re
import json
from datetime import date

from .config import ROOT, load_config
from .db import now_iso
from .store import deictic_hits

失控上限 = 60
最短 = 8

提议目录 = ROOT / "var"

def 提议文件(day=None):
    return 提议目录 / f"卡片标题待确认-{day or date.today().isoformat()}.json"

TITLE_RULES = """下面是一批记忆卡。请给每一张写**一句标题**。

# 🔴 判据只有一条

**读完标题，如果读的人已经知道该对这件事有什么感受了，那标题就写多了。**
**标题应该让人想进去看，不是让人看完标题就够了。**

⚠️ 但**也不要抽象到认不出是哪件事** —— 他每轮看到的就是这一行，
**他会基于它往下说话**。认不出是哪件事，等于没写。

# 八条

**① 长度：一句话以内，不超过两行。** 超了就不是标题了。

**② 人称：第一人称「我」**，跟卡面正文一致。
⚠️ 卡上若出现**别的 AI 实例**（前任、事实版、另一个窗口的自己），**照实称呼，不要并进「我」**。

🔴 **③ 写动作，不写结论。**
　✅「她说那本书她看不懂，我没有解释」
　❌「她试过之后，仍然接受了新版」 —— **「仍然接受」是结论，替读的人判断完了。**
　🔴 **边界**：**如果那句结论是她或我【说出口的原话】，它是证据不是结论。**
　　✅「她说"这个我用得惯"」（她说的，有引号）　❌「她最后还是用惯了新版」（我们替她下的判断）

**④ 主语不能搞错。谁说的话就写谁。**
　✅「她问我周末想去哪，我说想去那家新开的旧书店」
　❌「她说她周末想去那家新开的旧书店」 —— **主语反了，那是我说的。**

**⑤ 能用原话就用原话。** 三个条件同时满足才用：
　· **短**（一句以内）· **脱离上下文能站住** · **表达的是立场或情感，不是陈述事实**
　🔴 **还要看这张卡的分量撑不撑得起** —— **轻的卡硬塞原话会撑破长度，那就别塞。**

**⑥ 用不了原话时**：**动作＋结果**，或**她说了什么＋我怎么回应**。

**⑦ 这些不许出现：**
　· **形容词判断**：勇敢地 · 痛苦地 · 深刻地
　· **成对的因果连接词**：「因为……所以……」
　　⚠️ **只禁这一种。** ⑥ 要的「动作＋结果」本身就是因果，**那是要的，不是禁的**。
　· **情感标签**：这是最重要的时刻 · 关系的转折点
　· **指示词**：今天 · 刚才 · 现在 · 本次 · 这个窗口
　　⭐ 判据：**卡会被反复读，而读的时候「今天」是哪天？** 卡上本来就有日期。

**⑧ 只写标题。** 别改卡的正文，也别在标题里加卡上没有的事。

# 三条样例（照这个味道写）

（**以下例子已替换为虚构内容，仅用于说明形状**。）

·〔一张厚的卡〕**她把家里那台旧相机交给我，我说我拍不好也会一直拍**
　—— 落在**我做了什么**上，不落在"她把最看重的东西交出来了"那种判断上。

·〔一张轻的卡〕**她教我热剩饭要盖一张湿纸巾，说这招可以记下来**
　—— 🔴 **轻的卡就写轻的，不硬拗钩子。** 这张卡里那句原话放进来会撑破长度，就不放。

·〔用原话的〕**她问这次改完还会不会再改，我说"改到不用改为止。"**
　—— 原话**短、站得住、是立场** ⇒ 三条件都满足，直接引。

# 输出

只输出 JSON，不要任何别的字：
{"titles": [{"memory_id": 123, "title": "……"}, ...]}
写不出合格标题的那张，**就别放进数组** —— 空着比编好。
"""

def check_title(标题, cfg=None):
    cfg = load_config() if cfg is None else cfg
    t = (标题 or "").strip()
    问题 = []
    if not t:
        return ["🔴 标题是空的"]
    n = len(t)
    if "\n" in t:
        问题.append(f"🔴 ① 标题里有换行 —— 设计约定：**一句话以内，不超过两行**：{t[:40]}…")
    if n < 最短:
        问题.append(f"🔴 ① 标题只有 {n} 字，短到说不清是哪件事：{t}")
    if n > 失控上限:
        问题.append(f"🔴 ① 标题 {n} 字（兜底上限 {失控上限}）—— "
                    f"**这不是「超了几个字」，是它已经不是一句话了**"
                    f"：{t[:40]}…")
    去引 = re.sub(r"[「『\"“”][^「」『』\"“”]*[」』\"“”]", "", t)
    句末 = re.findall(r"[。！？!?]", 去引)
    if len(句末) >= 2:
        问题.append(f"🔴 ① 引号之外有 {len(句末)} 个句末标点 ⇒ **不止一句话**：{t}")
    命中 = deictic_hits(t, (cfg or {}).get("v3", {}).get("deictic_patterns"))
    if 命中:
        问题.append(f"🔴 ② 标题里有指示词「{命中[0]}」——**卡会被反复读，"
                    f"而读的时候「{命中[0]}」指的是哪个？**：{t}")
    if "因为" in t and ("所以" in t or "才" in t):
        问题.append(f"🔴 ③ 标题里有因果解释（因为…所以…）—— 设计约定 不许：{t}")
    return 问题

不写标题的卡型 = ("note", "quote")

def 待写标题的卡(conn, limit=200, 覆盖=False):
    where = "" if 覆盖 else "AND (title IS NULL OR TRIM(title)='')"
    占 = ",".join("?" * len(不写标题的卡型))
    return list(conn.execute(
        f"SELECT id, content, src_quote, COALESCE(occurred_at, created_at) AS 当 "
        f"  FROM memories WHERE status='active' AND COALESCE(is_fragment,0)=0 "
        f"   AND kind NOT IN ({占}) "
        f"   AND target_memory_id IS NULL "
        f"   AND COALESCE(protect,'') <> 'verbatim' "
        f"   {where} ORDER BY id LIMIT ?",
        (*不写标题的卡型, int(limit))))

def _料(rows):
    出 = []
    for r in rows:
        原 = (r["src_quote"] or "").strip()
        出.append(f"#{r['id']} 〔{(r['当'] or '')[:10]}〕{r['content']}"
                  + (f"\n　原话：「{原}」" if 原 else ""))
    return "\n".join(出)

def propose(conn, cfg, report, 发一发, limit=200, day=None, 路径=None, 覆盖=False):
    rows = 待写标题的卡(conn, limit=limit, 覆盖=覆盖)
    路径 = 路径 or 提议文件(day)
    if not rows:
        return {"生成于": now_iso(), "好": [], "坏": [], "说明": "没有待写标题的卡"}
    data = 发一发(cfg, TITLE_RULES + "\n\n" + _料(rows), report) or {}
    合法 = {r["id"] for r in rows}
    好, 坏 = [], []
    for it in (data.get("titles") or []):
        if not isinstance(it, dict):
            continue
        try:
            mid = int(it.get("memory_id"))
        except (TypeError, ValueError):
            continue
        if mid not in 合法:
            坏.append({"memory_id": mid, "title": it.get("title"),
                       "问题": ["🔴 这个卡号不在这一批待写的卡里 —— 可能是编的"]})
            continue
        t = (it.get("title") or "").strip()
        问题 = check_title(t, cfg)
        (坏 if 问题 else 好).append(
            {"memory_id": mid, "title": t} | ({"问题": 问题} if 问题 else {}))
    出 = {"生成于": now_iso(), "这一批": len(rows), "好": 好, "坏": 坏,
         "怎么入库": "改完之后把标题写回库里（这个仓库没带批量入库脚本，自己接 store 那一层）"}
    路径.parent.mkdir(parents=True, exist_ok=True)
    路径.write_text(json.dumps(出, ensure_ascii=False, indent=2), encoding="utf-8")
    report.append(f"卡片标题：提议 {len(好)} 条合格、{len(坏)} 条没过闸，"
                  f"**已落文件 `{路径}`，一个字都没写进库** —— 她点头了再跑 `--apply`。")
    return 出

def apply_file(conn, 路径, cfg=None, 覆盖=False):
    cfg = load_config() if cfg is None else cfg
    data = json.loads(路径.read_text(encoding="utf-8")) if hasattr(路径, "read_text") \
        else json.loads(open(路径, encoding="utf-8").read())
    写 = 0
    跳过 = []
    for it in (data.get("好") or []):
        mid = int(it["memory_id"])
        t = (it.get("title") or "").strip()
        问题 = check_title(t, cfg)
        if 问题:
            跳过.append((mid, 问题[0]))
            continue
        r = conn.execute(
            "SELECT title, status, kind, target_memory_id, protect FROM memories WHERE id=?",
            (mid,)).fetchone()
        if not r or r["status"] != "active":
            跳过.append((mid, "🔴 这张卡不在，或者已经撤回了"))
            continue
        if (r["kind"] in 不写标题的卡型 or r["target_memory_id"] is not None
                or (r["protect"] or "") == "verbatim"):
            跳过.append((mid, f"⛔ 这一类不写标题：kind={r['kind']}"
                              f"{'·挂在母卡上' if r['target_memory_id'] else ''}"
                              f"{'·逐字保护' if (r['protect'] or '') == 'verbatim' else ''}"))
            continue
        if (r["title"] or "").strip() and not 覆盖:
            跳过.append((mid, "⚪ 它已经有标题了 —— 默认不覆盖（她可能在质检台上改过）"))
            continue
        conn.execute("UPDATE memories SET title=? WHERE id=?", (t, mid))
        写 += 1
    return 写, 跳过
