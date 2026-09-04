import re
from datetime import date, datetime

from nacre import verdict
from nacre.config import ROOT

PATH = ROOT / "docs" / "叙事层.md"

SECTIONS = (
    "你们怎么走到今天的",
    "还没对上的矛盾",
    "她和这段关系的变化",
    "你们之间怎么说话",
    "从这些事里看出来的规律",
)

推断节 = SECTIONS[-1]
事实节 = SECTIONS[:-1]

截至行 = re.compile(r"^〔截至 (\d{4}-\d{2}-\d{2})〕")

指针 = re.compile(r"#\d+")

数量词 = re.compile(
    r"(每次|每回|每天|每周|每月|每年|[一二两三四五六七八九十百千零几多0-9]+\s*(?:次|回|遍|条|张|句|天|周|月|年))")

_反例前件 = ("如果", "要是", "哪天")
_反例后件 = ("不成立", "就说明", "推翻", "作废")

程度词 = ("很", "非常", "特别", "十分", "极其", "相当", "越来越", "更加", "格外", "尤其", "极为")

推断词 = ("总是", "一贯", "往往", "通常", "每次都", "每回都", "从来都", "说明",
        "意味着", "可见", "看得出", "看出来", "倾向于", "本质上", "其实是",
        "大概率", "显然", "一直都")

关系角色词 = ("恋人", "男友", "女友", "男朋友", "女朋友", "伴侣", "情侣", "老公", "老婆", "对象")

列表行 = re.compile(r"^\s*(?:[-*•·+]|\d+[.、)]|[（(]\d+[）)]|[①-⑳])\s*")

散文节 = ("你们怎么走到今天的", "你们之间怎么说话")

散文节列表行上限 = 1

推断段上限 = 6

_原话 = re.compile(r"「[^」]*」|“[^”]*”|\"[^\"]*\"")

def _今天(d=None):
    if d is None:
        return date.today()
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()

def split_doc(text):
    前言 = []
    节 = {}
    当前 = None
    buf = []
    for line in text.splitlines():
        if line.startswith("## "):
            if 当前 is not None:
                节[当前] = "\n".join(buf).strip("\n")
            当前 = line[3:].strip()
            buf = []
        elif 当前 is None:
            前言.append(line)
        else:
            buf.append(line)
    if 当前 is not None:
        节[当前] = "\n".join(buf).strip("\n")
    return "\n".join(前言).rstrip("\n"), 节

def render_doc(前言, 节, 未过闸=()):
    out = [前言.rstrip("\n"), ""]
    for 名 in SECTIONS:
        out.append("## " + 名)
        out.append("")
        out.append((节.get(名) or "").strip("\n"))
        out.append("")
    块 = 未过闸段落(未过闸)
    if 块:
        out += ["---", "", 块]
    return "\n".join(out).rstrip("\n") + "\n"

def _剥原话(s):
    return _原话.sub("", s)

def _分段(正文):
    行 = 正文.splitlines()
    if 行 and 截至行.match(行[0].strip()):
        行 = 行[1:]
    段 = []
    buf = []
    for line in 行:
        if line.strip():
            buf.append(line.strip())
        elif buf:
            段.append(" ".join(buf))
            buf = []
    if buf:
        段.append(" ".join(buf))
    return 段

def 引用的卡号(文本):
    见 = []
    for m in 指针.finditer(文本 or ""):
        n = int(m.group(0)[1:])
        if n not in 见:
            见.append(n)
    return tuple(见)

def check(节, 今天=None):
    _今天(今天)
    整份问题 = []
    单元 = {}

    def _记(名, 原文, 处置=None, 问题=None, 载荷=None):
        s = 单元.get(名)
        if s is None:
            s = verdict.段判决(名, 过=True, 原文=原文, 载荷=载荷,
                            引用卡号=引用的卡号(原文))
            单元[名] = s
        if 问题:
            s.过 = False
            s.处置 = 处置
            s.问题 = s.问题 + (问题,)
        return s

    if not any((节.get(名) or "").strip() for 名 in SECTIONS):
        整份问题.append("🔴 五节【全是空的】—— 这一发白花了，不是「宁缺毋滥」"
                    "")

    for 名 in SECTIONS:
        for 词 in 关系角色词:
            if 词 in 名:
                整份问题.append(f"🔴 命名闸：节标题里出现了关系角色词「{词}」——"
                            "标题本身就是一道角色指令")

    for 名 in [k for k in 节 if k not in SECTIONS]:
        _记(名, (节.get(名) or "").strip(), verdict.拒段,
            f"🔴 多出了不该有的节「{名}」—— 叙事层只有 {len(SECTIONS)} 节"
            "（「你们深谈过的话题」是刻意不生成的，见设计说明）")

    for 名 in SECTIONS:
        正文 = (节.get(名) or "").strip()
        if not 正文:
            continue

        if 名 in 散文节:
            列 = [l for l in 正文.splitlines() if 列表行.match(l)]
            if len(列) > 散文节列表行上限:
                _记(名, 正文, verdict.拒段,
                    f"🔴 ⑧ 罗列闸：「{名}」有 {len(列)} 行是列表行，它必须是**一段散文**"
                    f"：{列[0].strip()[:30]}…")

        for i, p in enumerate(_分段(正文), 1):
            键 = f"{名}#{i}"
            _记(键, p)
            if not 指针.search(p):
                用了 = [w for w in 程度词 if w in _剥原话(p)]
                if 用了:
                    _记(键, p, verdict.拒段,
                        f"🔴 滑坡闸：「{名}」有一段带程度词「{用了[0]}」又不带指针 ⇒ "
                        f"形容词式断言：{p[:30]}…")
            if 名 in 事实节:
                用了 = [w for w in 推断词 if w in _剥原话(p)]
                if 用了:
                    _记(键, p, verdict.拒段,
                        f"🔴 ⑤「{名}」是事实节，有一段出现了推断词「{用了[0]}」——"
                        f"推断只许出现在「{推断节}」那一节：{p[:30]}…")

    段 = _分段(节.get(推断节) or "")
    for i, p in enumerate(段, 1):
        键 = f"{推断节}#{i}"
        裸 = _剥原话(p)
        if not 指针.search(p):
            _记(键, p, verdict.拒段,
                f"🔴 ①「{推断节}」第 {i} 段没有 `#卡号` 指针：{p[:30]}…")
        if not 数量词.search(裸):
            _记(键, p, verdict.拒段,
                f"🔴 ③「{推断节}」第 {i} 段没有数量词 ⇒ 它会被当成规律读"
                f"：{p[:30]}…")
        if not (any(a in p for a in _反例前件) and any(b in p for b in _反例后件)):
            _记(键, p, verdict.拒段,
                f"🔴 ④「{推断节}」第 {i} 段没有「如果……就说明这条不成立」式的反例句"
                f"：{p[:30]}…")

    if len(段) > 推断段上限:
        _记(推断节, (节.get(推断节) or "").strip(), verdict.退回,
            f"🔴 ⑨「{推断节}」写了 {len(段)} 条，上限是 {推断段上限} 条 ——"
            "**模型极爱生成推断，问题不是没有，是太多**。\n"
            "   ⚠️ **这几条并列无序 ⇒ 程序不许砍尾巴**，退回让它自己挑要留哪几条"
            "。")

    return verdict.判决书(单元.values(), 整份问题)

未过闸节名 = "⚠️ 这一版没过闸的（没有进上面五节）"

def 落合格的(节, 判):
    按单元 = {s.单元: s for s in 判.段}
    落 = {}
    for 名 in SECTIONS:
        整节 = 按单元.get(名)
        if 整节 is not None and not 整节.过:
            落[名] = ""
            continue
        段 = [s for k, s in 按单元.items() if k.startswith(名 + "#")]
        段.sort(key=lambda s: int(s.单元.rsplit("#", 1)[1]))
        if 段:
            落[名] = "\n\n".join(s.原文 for s in 段 if s.过)
        else:
            落[名] = (节.get(名) or "").strip()
    return 落, 判.不合格()

def 未过闸段落(不合格):
    if not 不合格:
        return ""
    出 = [f"## {未过闸节名}", "",
         "> 🔴 **它们花了钱、没能落进正文，所以留在这儿** —— "
         "没有这一段，**她判不了「是它写坏了还是闸太严」**。", ""]
    for s in 不合格:
        出.append(f"### `{s.单元}`　〔处置：{s.处置}〕")
        for p in s.问题:
            出.append(f"- {p}")
        出 += ["", "原文：", "", "> " + (s.原文 or "（空）").replace("\n", "\n> "), ""]
    return "\n".join(出)

def remarks(节):
    出 = []
    空 = [名 for 名 in SECTIONS if not (节.get(名) or "").strip()]
    for 名 in 空:
        if 名 == "还没对上的矛盾":
            出.append(f"⚠️ 「{名}」这一版是空的 —— **设计上：这一格空了，"
                      "说明蒸馏在磨平东西，不是说明没矛盾。**")
        else:
            出.append(f"⚪ 「{名}」这一版是空的。")
    return 出

增量间隔天 = 7
全量间隔天 = 30
最多连续增量 = 3

def decide_tier(今天, 上次重写, 上次全量, 连续增量次数, 有新东西):
    今 = _今天(今天)
    if not 有新东西:
        return None, "距上次重写没有新卡、也没有新消息 ⇒ **这一趟根本不发**"
    if not 上次重写:
        return "全量", "从来没写过 ⇒ 第一次就是全量"
    上 = _今天(上次重写)
    隔 = (今 - 上).days
    if 隔 < 增量间隔天:
        return None, f"距上次重写只有 {隔} 天，不足 {增量间隔天} 天 ⇒ 这一趟不跑"
    if not 上次全量:
        return "全量", "还没有过一次全量 ⇒ 这一次走全量"
    隔全 = (今 - _今天(上次全量)).days
    if 隔全 >= 全量间隔天:
        return "全量", f"距上次全量 {隔全} 天 ≥ {全量间隔天} 天"
    if int(连续增量次数 or 0) >= 最多连续增量:
        return "全量", (f"连续增量已满 {最多连续增量} 次 ⇒ 强制全量"
                      "")
    return "增量", f"距上次重写 {隔} 天 ≥ {增量间隔天} 天"
