import json
from datetime import date

from . import verdict
from .config import ROOT
from .db import now_iso

场景上限 = 15

每块字数上限 = 1500

摘要字数上限 = 60

一批最多新增 = 1

橙色预警 = 场景上限 - 1

CREATE, UPDATE, MERGE = "CREATE", "UPDATE", "MERGE"
_合法动作 = (CREATE, UPDATE, MERGE)

ACTIVE, SUPERSEDED = "active", "superseded"
_合法状态 = (ACTIVE, SUPERSEDED)

产出目录 = ROOT / "var"

def 产出文件(day=None, 后缀="md"):
    return 产出目录 / f"地图谱系-{day or date.today().isoformat()}.{后缀}"

_定容那一段 = f"""# 🔴 定容 —— 这不是一个越攒越大的阁楼

· **场景总数硬上限 {场景上限} 个。**
· **每块正文最多 {每块字数上限} 字**，摘要最多 {摘要字数上限} 字。
· 🔴 **默认动作是 UPDATE，不是 CREATE。** 犹豫的时候选 UPDATE。
· **这一批最多新增 {一批最多新增} 个场景**，而且 CREATE 之前必须先确认
  它真的融不进任何一个现有场景。
· 🔴 **满了（已经有 {场景上限} 个）就必须先 MERGE 腾出位置，才能收新的。**
· ⛔ **不许删任何一块。** 被合并掉的那几块要原样留下、把 `status` 写成
  `superseded`，并由那条 MERGE 的 `supersedes` 指着它们 ——
  **我们的账本铁律不许物理删除，这一层照同一条走。**"""

_输出那一段 = """# 输出

只输出 JSON，不要任何别的字：

{"场景": [
  {"编号": "示例-谱系编号",
   "标题": "……",
   "摘要": "……",
   "动作": "UPDATE",
   "status": "active",
   "supersedes": [],
   "凭据": [123, 456],
   "正文": "……"}
]}

· `编号` 是这一块的稳定标识，**同一个话题在不同批次里要用同一个编号**。
· `动作` 只有三种：`CREATE` / `UPDATE` / `MERGE`。
· `supersedes` 只在 `MERGE` 时填，**列出被合并掉的那几块的编号（至少两个）**。
· 🔴 `凭据` 填**这一块是从哪几张卡看出来的卡号**。**不许填不在下面这批料里的卡号。**
· 写不出来的那一块**就别放进数组** —— 空着比编好。"""

_正文那一段 = """# 🔴🔴 每一块正文怎么写 —— 照这个样张的形状

（**下面的样张为虚构内容，仅用于说明形状**，卡号也是编的。）

```
> 🔴 **这条线讲的是：阳台那盆绿萝是怎么从「她的」变成「我们的」的。**

**起点是某天。** 她端来一盆快死的绿萝，说花店老板讲还有救 ⤷#101。
我照说明书浇水，第三天叶子掉得更快了 —— **说明书写的是「保持湿润」，
而那盆土根本没在排水。**

**关键的一步是我们换了盆。** 她翻出一个旧陶盆，我在底下垫了一层石子 ⤷#108，
**换完那天她说这盆以后归我管** ⤷#109。

**现在停在哪**：它活下来了，长到能分株 ⤷#132。**分出去的那一盆搁在窗台上，
而浇水是谁的活这件事，后来再没有人提过。**
```

## 从那个样张里要抄走的六条

**① 🔴 开头先给一句「这条线讲的是什么」**（引用块那一行）—— **让读的人先有抓手，再进细节。**

**② 🔴 人称写「她」和「我」，一句都不许省主语。**
　❌ 反例：「浇水的结果是…」「结论是…」「换盆之后活了」
　　—— **读的人要自己补主语，每一句都在费劲。**
　✅「我照说明书浇水」「她说这盆以后归我管」

**③ 🔴 分段，每段一个承重点。** 段落开头用粗体点出这一段在讲什么
　（**关键的一步是我们换了盆。** ／ **这条线后来翻了个面。**）
　⚠️ **几段由你定，别凑数** —— **有几个转折就几段。**

**④ 🔴 结尾必须回答「现在停在哪」** —— 不是停在最后一件事上，是**这条线现在的状态**。

**⑤ 指针贴着句子给**（`⤷#123`），**不要在末尾堆一串卡号。**

**⑥ 🔴🔴 长度 300~500 字，写不满就不写满。**
　⚠️ **只给字数上限不管用，每一块都会写到贴着上限**，读起来
　**每一句都很累**。⭐ **上限会被当成目标 —— 所以这里给的是区间，不是上限。**

# ⛔ 不许写成这样

· **不许逐条罗列时间线。**
　⭐ **判据：读完这一节，读的人该知道「这条线现在停在哪」，而不是「这条线上发生过哪 12 件事」。**
· **不许出现指示词**（"这个窗口""今天""刚才""本次"）—— **这份东西会被反复读。**
· 🔴 **只写料里有的**。看不出来的就别写，**不许推断她没说过的事**。
· 冲突的地方**不要直接覆盖**，写成"后来变成了什么"。"""

ATLAS_RULES = f"""你是这段关系里的那个 AI。下面给你一批**记忆卡**。
请把它们整理成一张**地图谱系**：一堆**场景块** ＋ 后面自动生成的一张导航页。

她要的是：**系统性地知道，她跟你到底都聊过哪些话题，每个话题下的形状／延展是什么样的。**

{_定容那一段}

{_正文那一段}

{_输出那一段}"""

ATLAS_RULES_对照 = ATLAS_RULES

def 取卡(conn, limit=400):
    return list(conn.execute(
        "SELECT id, title, content, src_quote, "
        "       COALESCE(occurred_at, created_at) AS 当 "
        "  FROM memories WHERE status='active' AND COALESCE(is_fragment,0)=0 "
        " ORDER BY COALESCE(occurred_at, created_at), id LIMIT ?", (int(limit),)))

def 料_卡(rows):
    出 = []
    for r in rows:
        原 = (r["src_quote"] or "").strip()
        出.append(f"#{r['id']} 〔{(r['当'] or '')[:10]}〕{r['content']}"
                  + (f"\n　原话：「{原}」" if 原 else ""))
    return "\n".join(出)

def 料_标题(rows):
    出 = []
    for r in rows:
        t = (r["title"] or "").strip()
        if not t:
            continue
        出.append(f"#{r['id']} 〔{(r['当'] or '')[:10]}〕{t}")
    return "\n".join(出)

def _键(块, k, 默认=None):
    v = 块.get(k) if isinstance(块, dict) else None
    return 默认 if v is None else v

def _编号(块):
    return str(_键(块, "编号", "") or "").strip()

def _状态(块):
    return str(_键(块, "status", ACTIVE) or ACTIVE).strip()

def check(场景, 现有=(), 合法卡号=None):
    if not isinstance(场景, list):
        return verdict.判决书((), ["🔴 `场景` 不是一个数组 —— 模型返回的结构不对"])

    整份问题 = []
    现有 = [str(x).strip() for x in 现有]
    活的 = [b for b in 场景 if _状态(b) == ACTIVE]

    单元 = []
    索引 = {}
    见过 = {}
    for i, b in enumerate(场景, 1):
        e = _编号(b)
        见过[e] = 见过.get(e, 0) + 1
        名 = (f"场景 {e}" if e else f"第{i}块（没编号）") + (
            f"（第{见过[e]}次出现）" if e and 见过[e] > 1 else "")
        s = verdict.段判决(名, 过=True, 原文=渲染一块(b), 载荷=b,
                        引用卡号=tuple(_键(b, "凭据", []) or ()))
        单元.append(s)
        索引[id(b)] = s

    def _拒(b, 处置, 话):
        s = 索引[id(b)]
        s.过 = False
        s.处置 = 处置
        s.问题 = s.问题 + (话,)

    def _整批(名, 处置, 话, 料=""):
        单元.append(verdict.段判决(名, 过=False, 问题=(话,), 处置=处置, 原文=料))

    编号们 = [_编号(b) for b in 场景]
    for b in 场景:
        if not _编号(b):
            _拒(b, verdict.拒段,
                "🔴 ① 这一块没写编号 —— 编号是它的稳定标识，没有它就没法 UPDATE，"
                "**下一批只能重新 CREATE，而那正是「越攒越多」的起点**")
    见 = set()
    for b in 场景:
        e = _编号(b)
        if e and e in 见:
            _拒(b, verdict.拒段, f"🔴 ① 编号 `{e}` 出现了不止一次 —— **重复的这一块不落**")
        if e:
            见.add(e)
    重 = sorted({e for e in 编号们 if e and 编号们.count(e) > 1})
    if 重:
        整份问题.append(f"🔴 ① 这几个编号出现了不止一次：{重}")

    for b in 场景:
        动 = str(_键(b, "动作", "") or "").strip()
        if 动 not in _合法动作:
            _拒(b, verdict.拒段, f"🔴 ② `{_编号(b) or '（没编号）'}` 的动作是 {动!r} —— "
                              f"只认 {list(_合法动作)}")
        if _状态(b) not in _合法状态:
            _拒(b, verdict.拒段, f"🔴 ② `{_编号(b) or '（没编号）'}` 的 status 是 {_状态(b)!r} —— "
                              f"只认 {list(_合法状态)}")

    if len(活的) > 场景上限:
        _整批("这一批·场景数", verdict.退回,
             f"🔴 ③ 这一批留下了 {len(活的)} 个场景，硬上限 {场景上限} 个"
             f"—— **先合并，别扩容。**\n"
             f"   🔴 **这 {len(活的)} 个并列无序 ⇒ 程序不许砍尾巴**，"
             "退回让它自己 MERGE 到 15 个以内。",
             "\n".join(f"{_编号(b)}　{_键(b, '标题', '') or ''}" for b in 活的))

    for b in 场景:
        n = len(str(_键(b, "正文", "") or ""))
        if n > 每块字数上限:
            _拒(b, verdict.拒段,
                f"🔴 ④ `{_编号(b)}` 正文 {n} 字，封顶 {每块字数上限} 字")
        m = len(str(_键(b, "摘要", "") or ""))
        if m > 摘要字数上限:
            _拒(b, verdict.拒段,
                f"🔴 ④ `{_编号(b)}` 摘要 {m} 字，封顶 {摘要字数上限} 字 —— "
                f"导航页的用处就是一眼扫过 {场景上限} 行，一行写成一段就白搭了")

    新增 = [b for b in 场景 if str(_键(b, "动作", "")).strip() == CREATE]
    合并 = [b for b in 场景 if str(_键(b, "动作", "")).strip() == MERGE]

    腾出 = sum(max(0, len([e for e in (_键(b, "supersedes", []) or []) if str(e).strip() in 现有]) - 1)
              for b in 合并)
    if 新增 and len(现有) >= 场景上限 and 腾出 <= 0:
        _整批("这一批·满了还要新增", verdict.退回,
             f"🔴 ⑤ 现在已经有 {len(现有)} 个场景（满了，上限 {场景上限}），"
             f"这一批却要新增 {len(新增)} 个而**一个位置都没腾出来** —— "
             f"设计上：**满了必须先合并才能收新的。**\n"
             f"   ⚠️ 判的是「合并真的吃掉了旧块」，不是「有没有写 MERGE 这两个字」。",
             "\n".join(_编号(b) for b in 新增))

    if len(新增) > 一批最多新增:
        _整批("这一批·新增个数", verdict.退回,
             f"🔴 ⑥ 这一批新增了 {len(新增)} 个场景，一批最多 {一批最多新增} 个 —— "
             f"设计上：**默认 UPDATE，不 CREATE。**"
             f"新增的是：{[_编号(b) for b in 新增]}\n"
             "   🔴 **哪一个该留是内容判断 ⇒ 程序不许挑**，退回让它自己定。",
             "\n".join(_编号(b) for b in 新增))

    在这一批 = {e for e in 编号们 if e}
    被合并 = {str(e).strip() for b in 场景 for e in (_键(b, "supersedes", []) or [])}
    没了 = [e for e in 现有 if e not in 在这一批 and e not in 被合并]
    if 没了:
        _整批("这一批·凭空不见的块", verdict.退回,
             f"🔴 ⑦ 这几块**凭空不见了**：{没了} —— "
             "**不许物理删除**（铁律）。要么留着，要么被某条 MERGE 的 "
             "`supersedes` 指着并转 `superseded`。\n"
             "   ⚠️ 没有这道闸的话，「合并了」和「删掉了」在文件里长得一模一样。",
             "\n".join(没了))

    for b in 合并:
        旧 = [str(e).strip() for e in (_键(b, "supersedes", []) or []) if str(e).strip()]
        if len(旧) < 2:
            _拒(b, verdict.拒段,
                f"🔴 ⑧ `{_编号(b)}` 说自己是 MERGE，`supersedes` 却只有 {len(旧)} 个 —— "
                "**合并至少要吃掉两块**，否则它只是改了个名字")
        指不到 = [e for e in 旧 if 现有 and e not in 现有]
        if 指不到:
            _拒(b, verdict.拒段,
                f"🔴 ⑧ `{_编号(b)}` 说它合并了 {指不到}，"
                "而那几个不在现有的场景里 —— **指向空处的引用比没有引用更坏**")
    for e in 被合并:
        块 = next((b for b in 场景 if _编号(b) == e), None)
        if 块 is not None and _状态(块) != SUPERSEDED:
            _拒(块, verdict.拒段,
                f"🔴 ⑧ `{e}` 被合并掉了，status 却还是 {_状态(块)!r} —— "
                f"应该是 {SUPERSEDED!r}")

    if 合法卡号 is not None:
        合法 = {int(x) for x in 合法卡号}
        for b in 场景:
            坏 = []
            for c in (_键(b, "凭据", []) or []):
                try:
                    if int(c) not in 合法:
                        坏.append(int(c))
                except (TypeError, ValueError):
                    坏.append(c)
            if 坏:
                _拒(b, verdict.拒段,
                    f"🔴 ⑨ `{_编号(b)}` 的凭据里有不在这一批料里的卡号：{坏} —— "
                    "**可能是编的，而编出来的号照样打印得出来**")
    return verdict.判决书(单元, 整份问题)

def remarks(场景, 现有=()):
    出 = []
    活的 = [b for b in 场景 if _状态(b) == ACTIVE]
    if len(活的) >= 橙色预警:
        出.append(f"🟠 已经有 {len(活的)} 个场景（上限 {场景上限}）—— "
                  f"**下一批只该 UPDATE，不该 CREATE**；再满就必须先合并。")
    薄 = [_编号(b) for b in 活的 if len(str(_键(b, "正文", "") or "")) < 80]
    if 薄:
        出.append(f"⚪ 这几块正文很短（不到 80 字）：{薄} —— "
                  "**不拒收**：料本来就可能不够；但值得看一眼是不是该并进别的块。")
    孤 = [_编号(b) for b in 活的 if not (_键(b, "凭据", []) or [])]
    if 孤:
        出.append(f"⚪ 这几块一个凭据卡号都没写：{孤} —— **不拒收**，但它们指不回任何一张卡。")
    return 出

导航凭据条数 = 5

导航抬头 = ("你们一起走过的这些线（从全部记忆卡里梳理出来的，不是这一轮查到的）\n"
            "想看某一条背后的原始记录，按它后面那几个卡号 recall。")

def 导航页(场景, 给他=False):
    活的 = [b for b in 场景 if _状态(b) == ACTIVE]
    活的.sort(key=lambda b: (-len(_键(b, "凭据", []) or []), _编号(b)))

    if 给他:
        行 = [导航抬头, ""]
        for b in 活的:
            凭 = [str(c) for c in (_键(b, "凭据", []) or [])][:导航凭据条数]
            锚 = ("　" + " ".join("⤷#" + c for c in 凭)) if 凭 else ""
            行.append(f"· {_键(b, '标题', '') or _编号(b)} —— "
                      f"{_键(b, '摘要', '') or '（没写摘要）'}{锚}")
        return "\n".join(行)

    行 = ["## 🗺️ 场景导航", "",
          f"*一共 {len(活的)} 个场景（上限 {场景上限}）。按「有几张卡撑着它」排。*", ""]
    for b in 活的:
        n = len(_键(b, "凭据", []) or [])
        行.append(f"### {_键(b, '标题', '') or _编号(b)}　`{_编号(b)}`")
        行.append(f"**{n} 张卡撑着** ｜ 摘要：{_键(b, '摘要', '') or '（没写摘要）'}")
        行.append("")
    return "\n".join(行)

def 渲染一块(块):
    行 = [f"### {_键(块, '标题', '') or _编号(块)}　`{_编号(块)}`"]
    元 = [f"动作：{_键(块, '动作', '?')}", f"状态：{_状态(块)}"]
    旧 = [str(e) for e in (_键(块, "supersedes", []) or [])]
    if 旧:
        元.append("合并掉了：" + "、".join(f"`{e}`" for e in 旧))
    凭 = [str(c) for c in (_键(块, "凭据", []) or [])]
    元.append(f"凭据 {len(凭)} 张卡" + (f"：{'、'.join('#' + c for c in 凭[:20])}" if 凭 else ""))
    行 += ["　｜　".join(元), "", str(_键(块, "正文", "") or "").strip(), ""]
    return "\n".join(行)

def 渲染(场景, 标题="地图谱系"):
    活的 = [b for b in 场景 if _状态(b) == ACTIVE]
    退的 = [b for b in 场景 if _状态(b) != ACTIVE]
    出 = [f"# {标题}", "", 导航页(场景), "", "---", "", "## 场景块", ""]
    出 += [渲染一块(b) for b in 活的]
    if 退的:
        出 += ["---", "",
               "## 已被合并（`superseded`）—— **留着不删（铁律：不许物理删除）**", ""]
        出 += [渲染一块(b) for b in 退的]
    return "\n".join(出)

def _卡索引(rows):
    return {int(r["id"]): f"〔{(r['当'] or '')[:10]}〕{r['content']}" for r in rows}

def _并回去(原, 新, 判):
    if not isinstance(新, list) or not 新:
        return 原, False, "段级重试没回来能用的东西 —— **原样保留上一发的产出**。"
    好 = [b for b in 新 if isinstance(b, dict) and _编号(b)]
    if not 好:
        return 原, False, ("段级重试回来的**形状不对**（不是场景对象）—— "
                           "**整份丢掉，原样保留上一发的产出。**")
    被拒 = {s.单元 for s in 判.不合格()}
    改 = {_编号(b): b for b in 好}
    出, 换了 = [], 0
    for b in 原:
        k = _编号(b)
        if k in 改:
            出.append(改.pop(k)); 换了 += 1
        else:
            出.append(b)
    出 += list(改.values())
    return 出, True, ("" if 换了 else
                      f"段级重试回来的块跟被拒的那几个（{sorted(被拒)}）对不上号 —— "
                      f"**没有替换掉任何一块，只是把新块并了进来。**")

def _一路(名, 提示词, 料, cfg, report, 发一发, 合法卡号, 现有, 卡索引=None):
    if not 料.strip():
        return {"这一路": 名, "料": 0, "场景": [],
                "问题": verdict.判决书((), [f"🔴 {名}这一路的料是空的 —— 没发请求"]),
                "提醒": [], "发了": False, "重试了": False}
    data = 发一发(cfg, 提示词 + "\n\n" + 料, report) or {}
    场景 = data.get("场景") or []
    判 = check(场景, 现有=现有, 合法卡号=合法卡号)

    重试了 = False
    if 判.不合格() and not 判.没救了:
        小 = verdict.段级重试提示词(
            名, 判.不合格(), 卡索引=卡索引,
            输出说明='# 输出\n\n只输出 JSON，不要任何别的字。\n'
                 '🔴 **只给被拒的那几块的改好版**，键跟上一发一样：\n{"场景": [ … ]}\n'
                 '⛔ **不要把没被拒的那些也一起返回** —— 它们已经落盘了。')
        try:
            回 = 发一发(cfg, 小, report) or {}
        except Exception as e:
            回 = {}
            report.append(f"⚠️ {名}段级重试那一发炸了（{type(e).__name__}: {e}）。")
        场景, 重试了, 说 = _并回去(场景, (回 or {}).get("场景"), 判)
        if 说:
            report.append(f"⚠️ {名}{说}")
        if 重试了:
            判 = check(场景, 现有=现有, 合法卡号=合法卡号)

    return {"这一路": 名, "料": len(料), "场景": 场景, "重试了": 重试了,
            "问题": 判, "提醒": remarks(场景, 现有=现有), "发了": True,
            "解析后的原样": data}

def propose(conn, cfg, report, 发一发, limit=400, day=None, 路径=None, 对照=True):
    rows = 取卡(conn, limit=limit)
    合法卡号 = {r["id"] for r in rows}
    路径 = 路径 or 产出文件(day, "md")
    if not rows:
        return {"生成于": now_iso(), "这一批": 0, "主产出": None, "对照组": None,
                "说明": "库里没有可以进地图的 active 卡 —— 没发请求"}

    卡索引 = _卡索引(rows)
    主 = _一路("主产出（喂记忆卡）", ATLAS_RULES, 料_卡(rows), cfg, report, 发一发,
             合法卡号, 现有=(), 卡索引=卡索引)
    对 = None
    if 对照:
        对 = _一路("对照组（喂标题）", ATLAS_RULES_对照, 料_标题(rows), cfg, report, 发一发,
                 合法卡号, 现有=(), 卡索引=卡索引)

    出 = {"生成于": now_iso(), "这一批": len(rows), "主产出": 主, "对照组": 对,
         "这是什么": "🔴 只给她看的产出——**没有入库这一步**，"
                 "也没有任何东西把它接进注入路径。她看完点头之后再谈接不接。"}

    路径.parent.mkdir(parents=True, exist_ok=True)
    路径.write_text(渲染成文档(出), encoding="utf-8")
    json路径 = 路径.with_suffix(".json")
    存 = dict(出)
    for k in ("主产出", "对照组"):
        一 = 存.get(k)
        if 一:
            一 = dict(一)
            判 = 一.get("问题")
            if hasattr(判, "as_dict"):
                一["问题"] = list(判)
                一["逐段判决"] = 判.as_dict()
            存[k] = 一
    json路径.write_text(json.dumps(存, ensure_ascii=False, indent=2), encoding="utf-8")

    report.append(
        f"地图谱系：这一批 {len(rows)} 张卡。"
        f"主产出 {len(主['场景'])} 个场景、{len(主['问题'])} 条没过闸"
        + (f"；对照组 {len(对['场景'])} 个场景、{len(对['问题'])} 条没过闸" if 对 else "；没跑对照组")
        + f"。**已落文件 `{路径}` 与 `{json路径}`，一个字都没写进库，也没接进任何注入路径。**")
    return 出

def 渲染成文档(出):
    段 = [f"# 地图谱系　生成于 {出.get('生成于', '')}",
         "",
         f"这一批喂了 **{出.get('这一批', 0)}** 张卡。",
         "",
         "> 🔴 **这份东西只是给她看的。** 它**没有入库**，也**没有**被接进他的上下文 ——"
         "规矩是：先只给她看，她看完点头说 ok 才算数。",
         "",
         "> **两套并排是故意的**：**主产出喂记忆卡**· "
         "**对照组喂标题** ⇒ 看「输入多一跳到底漂多少」。"
         "设计上「不读别的生成物」那条此前只有理由、没有实测，这一次几乎白送一个证据。",
         ""]
    for key in ("主产出", "对照组"):
        一 = 出.get(key)
        段 += ["---", "", f"# {key}"]
        if not 一:
            段 += ["", f"（没跑{key}。）", ""]
            continue
        段 += ["", f"料 {一['料']} 字。", ""]
        if 一["问题"]:
            段 += ["## 🔴 没过闸的", ""] + [f"- {p}" for p in 一["问题"]] + [""]
            判 = 一["问题"]
            不合格 = 判.不合格() if hasattr(判, "不合格") else []
            if 不合格:
                段 += ["### 被拒的那几块，原样留在这儿", ""]
                for u in 不合格:
                    段 += [f"**`{u.单元}`　〔处置：{u.处置}〕**", ""]
                    段 += [f"- {q}" for q in u.问题]
                    段 += ["", "```", u.原文 or "（空）", "```", ""]
        else:
            段 += ["## ✅ 定容那几条闸全过了", ""]
        if 一.get("重试了"):
            段 += ["> 🔴 **这一路做过一次【段级重试】**（只把被拒的那几块发回去，"
                   "不是整份重发 —— 段级重试比整份重发便宜一个量级）。**只重试一次。**", ""]
        if 一["提醒"]:
            段 += ["## 说一声（不拒收）", ""] + [f"- {p}" for p in 一["提醒"]] + [""]
        段 += [渲染(一["场景"], 标题=key), ""]
    return "\n".join(段)
