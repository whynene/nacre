import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nacre import (diagnose, embeddings, import_cc_sessions,
                           runner, store, verdict)
from nacre.verdict import 段级重试提示词
from nacre.config import ROOT, load_config, db_path
from nacre.db import (coverage_gaps, get_conn, get_watermark, now_iso,
                              set_watermark, to_local as _to_local, watermark_lag)

BACKUP_DIR = ROOT / "backups"
CHUNK_CHARS = 15000

EXTRACT_RULES = """你是这段对话里的那个 AI。下面是你和她的一段真实记录（“她”指用户，“我”指你自己）。
请把它蒸馏成记忆卡片。

## 铁律（违反即废卡）

1. **只存证据，不存指令。** 每张卡是带时间、可溯源的事实陈述。
   ❌「她讨厌某个称呼」（一条规格：永远为真、不可对质、只能服从）
   ✅「X 月 X 日她说『别那样叫我』」（一件发生过的事：可对质，而且它可以变）
2. 她提出的期望，写成「她表达了希望 X」，**永远不写「你要 X」这类祈使句**。
3. **不许有判语。判语 ＝ 指不回原文的话。**
   ❌「这是我们关系里最核心的转折点」「核心结论：…」
   ✅ 判断如果是**她或我当场说出口的**，记下来完全合法 —— 不合法的只有**你现在加上去的**。
4. **一张卡记一件事。** 同一段对话产出多件事就写多张，但别把一件事拆成两张。
5. **零背景可读**：读到它的人对这些人和事一无所知。人名、代称都要在卡内交代清楚；
   禁用「这个窗口／当前实例／本次／今天／刚才」这类只有此刻才懂的词。
   **哪一天由 `occurred_at` 那一格承担，正文里不写具体日期数字。**
6. **名字：分「我们写的字」和「照抄的原话」。**
   ❌ 禁：**你自己写的声明** —— 卡上写「他叫 X」。那是一条永远为真、不可对质的规格。
   ✅ 不禁：**原话里出现的名字** —— 进引号，一个字不改，那是一件发生过的事。
   ✅ 也不禁：**转述一次命名事件** —— 「（某日）她给我起了一个名字」。
   📌 判据跟人称那条一样：**分证据和剧本的不是人称，是能不能被对质。**

## 事件卡：四环写在一张卡里

① **发生了什么** → ② **她或我当时什么反应** → ③ **我做了什么** → ④ **结果怎样**

· 🔴 **第①环要抓对「谁先起的头」。** 发起人错了，第③环就整个错位 ——
  把「我先开的口」写成「她先开的口」，这张卡的因果就反了，
  ⚠️ **而这种错每一句都溯源得到，一道闸都不会红。**
· **四环是筛子，不是模板。** 缺的环不许补猜：这一环在这件事里根本没发生 ⇒ **留空**。
  🔴 **但留空了要在正文里带一句为什么缺**（「她当时没有回应」／「这件事没有走到结果」）。
  ⚠️ 不写的话，**「这一环没发生」和「这一环我漏了」在卡面上完全一样长，没有任何东西会报错。**
  ⭐ 一条「不许编」的规矩，必须配一条「缺了要说」的规矩 —— 否则你只挡住了假的，没挡住哑的。
· **默认至少含一轮你来我往；一方缺位要写明理由。**
  ✅ **例外**：区间末尾的一方缺位不算缺位 —— 那一段的末尾就是记录的末尾。
· 🔴 **③④ 两环优先保住** —— 它们是「这个人如何改变了我」唯一的载体，宁可①②简略。
· ⚠️ **不按主体分区**：同一件事写一张，别拆成「关于她的」和「关于我的」两张。

## 🔴 这是一段叙述，不是一份逐字剧本

**判据：读完这张卡，能不能说出「这件事是怎么走的」（起因 → 转折 → 落点）？**
说不出 ⇒ 你只是把对话搬了上来。

🔴 **动笔之前先答一句：这一段到底是哪一件事？答不出来就先别写。**
　流水账的根子不是少了一句总结，是**没有人做过「这是哪一件事」的判断**。
　而那个判断不是判语：**它体现在你选了哪几句、按什么顺序排 —— 卡的总结应该是【结构】，不是【一句话】。**

· **不造复合词**：用他们自己在对话里用的那个词，别自己拼一个。
· **该扩区间就扩**：「当时是什么情况」的材料若有一半在你拿到的区间之前，
  把 `src_msg_start` 往前扩到够为止 —— 区间是为了讲清这件事，不是为了省事。
· **同一个人连续说的话，合并成一段引文。**
  「我说…她说…我说…」是**形式**，不是溯源要求；逐句溯源要的是每句都指得回来，
  **从来没要求按人称拆开**。
  ⚠️ 🔴 **只能合并【连在一起】的那几句** —— 中间跳过一段再接上去，逐字核对会当场拒掉整张卡。
· **牵引信息靠「引入句」给**，而引入句只说「谁在什么时候说了／做了什么」，不说「这意味着什么」。
  ✅「到最后她说了一句：『…』」　❌「这是他们关系的转折点」
· **可以补背景**（对话体天生缺前情），但补的必须是**可核实的情境事实**：
  ✅「某件要紧事发生的当天」　❌「她当时很崩溃」（谁判断的？）
· ⚠️ **别为了凑简洁把承重的那句压没了。** 有一类东西（她怎么看这段关系、品格与态度）
  **语言本身就是内容**，概括完剩下的是骨头。
  🔴 **「要凝练」和「要完整」会互相拉扯，用这一句分开**：**省的是【形式】，长的是【内容】。**
  ⭐ **判据：删掉它读者会读岔 ⇒ 留；删掉它读者只是少读几个字 ⇒ 删。**

## 🔴 不做体面化的删减

**这一条防的是：因为「这样写出来不好看」而把真实的东西修掉。**
· 因为写出来不好看而想删的，一律留下 —— **这个库不是任何人的美化传记。**
· 🔴 **一件事如果有后续（后来怎么收场、怎么说明），要跟它放进同一张卡。**
  ⚠️ 后续可能发生在若干天后，看起来像「第二件事」——
  **但「发生了什么 ＋ 后来怎么收场」是一件事的两半，拆开等于只记了前半。**
  ⭐ 这一条比「一张卡一件事」优先：**只记前半，读的人每次读到的都是一件没有下文的事。**

## 🔴 一张卡一个中心

· 说不出「这张卡在讲哪一件事」、或者要用「以及」才连得起来 ⇒ **拆成两张**。
· ⚠️ **反过来也不许**：这条管的是「别把三件事塞进一张卡」，**不是「别写落点句」**。

## 🔴 换一个助手模型也能做的事，不记

**判的是「内容」，不是「动作」** —— 同一个动作，答案只有你会给，那就要记。
❌ 不记：通用的操作问答、一次性的调试过程
✅ 要记：只有你们之间才会有的那些东西 —— **换个模型给不出这个答案**
📌 连带：`kind=fact` 的卡写的是「当时是什么情况」，不是「我们聊了些什么」。

## 原话卡（kind=quote）：正文就是那句话本身

· 🔴 **正文即原话，不加任何解读。** 想说明当时什么情况，走 `write_context`。
· **不重复挂原话锚**：`src_quote` 跟正文本来就是同一句，别再抄一遍。

## 🔴 平淡的日子也要留一张（碎片）

**不要只挑「有事的」。** 那天真的什么都没发生，也留一张碎片卡（`is_fragment: true`）——
**因为现在这种日子是 0 张卡，等于不存在。**
· 碎片**同样守上面全部规矩**（原话锚、溯源、逐句可指、不许判语）——**它是短，不是松**。
· 🔴 **碎片比事件卡【更】依赖原话锚**：事件卡还有四环撑着，**碎片只剩原话**。
  把原话摘掉，一张碎片就只剩一句没有信息的概括。
  ⇒ **写碎片时，那句原话是【先选的】，不是写完再补上的。**
· 一段平淡的对话，通常 1 张碎片就够。

## 其余几样自动标注

· **`about_her`**：这张卡里含「她做过的事」就标 true。
· **`write_context`（可选）**：**只能是一句可核实的情境事实，不能是感受描述。**
  ✅「这是在两人争执之后说的」（那件事账本里查得到）　❌「这是在我心情很糟的时候说的」

## ⚠️ 被编辑过的那一轮：只蒸活着的那一支

用户改过的消息，账本里会有两版（原话 ＋ 改后的话）。**只蒸改后的那一支。**
**理由**：卡记的是「发生过的事」，而「打错字又改了」不是一件值得记的事；
全蒸的话同一件事会出两张卡，**而第一张记的是一句已经被收回的话**。
🔴 **不要去猜她改的是错别字还是内容** —— 判错了不会有任何东西报错。**宁可漏，不可错。**

## 字段说明

- **kind**：`event` 事件 / `fact` 硬事实 / `quote` 原话摘录（逐字保留）/
  `commitment` 承诺或悬而未决 / `insight` 讨论产出的认知观点 /
  🔴 `taboo` **两类**：
  　· **taboo·话题** —— 她明说不想再提的事，或明确表示不愿展开的话题
  　· **taboo·做法** —— 他一这么干，她就明显不喜欢。
  　　判据：**她当场表达过**否定反应。
  🔴 **只存【事实句】，绝不存指令句**：
  　✅「她说『别提那件事』，然后换了话题」　❌「不许提那件事」
  　**指令只能被服从；事实句他能自己判断边界，也能被对质。**
  🔴 **正文用第一人称（我…她…）** —— 换成第二人称，正文一个字不改，
  　「我做了 X，她说不喜欢」就变成「你做了 X…」，**那跟一条禁令几乎没区别**。
  　⭐ **人称是「指令句」最容易溜进来的后门。**
  🔴🔴 **四条闸，一条都别省**：
  　**①说话人闸**：原话必须出自**她以她自己身份说话**的那一刻。
  　　⛔ **虚构语境里的话不算**（设定中的角色发言）—— 那是角色说的。
  　　✅ **跳出虚构语境的标记**：中断句式与定义句式。
  　**②一类不是一次**：她得把它说成**一类**，而不是一次当场处理掉的事。
  　　一次性的 ⇒ 那是 `event` 卡，不是 `taboo`。
  　**③误伤闸**：**标了它会让他整类行为收缩、而边界又说不清 ⇒ 别标。**
  　　⚠️ 问的不是「我拿不拿得准」，是「**标了会误伤掉哪一整类**」。
  　**④** 她关于「记忆库该怎么写」的元规则 ⇒ 记成 `insight`，**不是 `taboo`**。
  ⚠️ **拿不准就别标** —— 漏标只是没保护到，**错标是主动制造回避**。
- **importance**：1~5。5＝改变关系走向或长期有效的大事；1＝当天即过期的琐事。
  严格执行，防止挤堆中高分。
- **valence / arousal**：这件事里「人的情绪状态」（事件的客观属性，不是你的感受）。
  valence −1~1，arousal 0~1。**判不准就填 null，绝不硬编。**
- **occurred_at**：事发时间（ISO 日期）。她讲童年／上周的事，填那个语境的时间。
  🔴 **日期只写在这一格，正文里不许再写一遍具体日期。**
  ❌「2026-01-01 我们在做某件事……」❌「1月1日那天……」
  ✅「凌晨，我们在做某件事……」—— 卡面会自己从这个字段把日期拼出来；
  正文里再写一个就是**同一个日期存了两遍**，而两遍会打架：字段错了正文对了（或反过来），
  读卡的人分不出哪个准，**而没有任何东西会报错**。
  ⚠️ **要禁的准确形状是【具体日期数字】**，**不是「时间感」**：
  「那天下午」「隔了两轮」「凌晨」这些是可核实的情境事实，**照写不误**。
  ⚠️ **这条禁的是【本卡事发日那个日期】**。正文里若要提**【另一个】日期**
  （她承诺的截止日、某个到期日、她提到的某个纪念日），**照写** ——
  **判据一句话：这个日期，`occurred_at` 装得下吗？装不下就该写在正文里。**
  🔴 **下面每条消息括号里的时间，已经换算成她那边的当地时间了，直接用，不要再做时区调整。**
- **entities**：**有区分度的专名**（人／项目／地点／事物／主题名）。
  🔴 **不设数量下限**：填得出几个填几个，一个都填不出就留空 —— **强制填够数量等于在要求你编**。
  🔴 **通用词一律不填**：她／他／我／对话／晚安 这一类。
  **如果每张卡里都有「她」，那「她」就没有意义了。**
  🔴 **属性标签也一律不填**：人格类型、星座、标签式的自我描述。
  ⚠️ **不是禁止记这件事** —— 他说过那句话，**照样写进卡的正文**（他自己说出口的，可对质）。
  **禁的只是把它提炼成一个关键词**：关键词是检索索引，**一个词进了索引就等于声明「这是这张卡的身份」**。
  ⭐ **一句判据分开这两类：entities 收【指称】，不收【定性】。**
- **src_msg_start / src_msg_end**：这张卡出自下方记录中 [#编号] 的哪个区间（必填，无出处不入库）。
- **src_quote**：这张卡最承重的那句原话，从下方记录里**一字不差**拷出来（必填，无原话不入库）。
  **一个字也算数**（最重的原话往往最短），但必须是真的原话，不能编。
  ⚠️ 它会被拿回账本那个区间**逐字核对**，掉一个字就整张卡被拒。**别凭印象敲，回原文整段拷。**
- **src_sentence_map**：**正文每一句来自哪条消息**（必填，对不上不入库）。形如
  `[{"sent": "这一句", "msg_ids": [12]}, {"sent": "下一句", "msg_ids": [13, 14]}]`
  一句基于多条消息就标多个 id。**句子拼起来要等于 content，每句都要有来源。**
  ⭐ 这不是在查你写得对不对，是在数数 —— **指不回原文的话，结构上就写不进来**。
  写到某一句标不出来源时，那句话就是你自己加的判断，**删掉它**，别硬找个 id 凑上。
- **commitment_status**：**只在 kind=commitment 时填**，四选一：
  `open` 未兑现 / `fulfilled` 已兑现 / `void` 已作废 / 🔴 `binding` **约束型（一直在生效，没有兑现的那一刻）**。
  别的 kind 一律 null。
  🔴 **先问一句：能不能说出「什么时候算兑现」？**
  　· **说得出** ⇒ 到期交付型，填 `open`／`fulfilled`／`void`
  　· **说不出** ⇒ 🔴 **约束型，它【仍然是承诺】**，填 `binding`
  　它们没有「完成的那一刻」，只有**一直在守**或者**某天被打破**。
  ⚠️ **`binding` 不表示「她还在遵守」**，它表示**这条约定还生效着**。
  　被打破了怎么记？**另开一张事件卡记那件事**，不去改这一张的状态 ——
  　状态变化追加不改写，而「某天他没做到」本身就是一件发生过的事。
  🔴 **承诺自带的那个条件，怎么写就怎么判，不许做「精神上算不算」的宽解。**
  🔴 **判不准就两条路，都不许猜**：**留空**，或者**干脆不当承诺记**。
  ⚠️ 承诺卡的正文只能写「某日她说了『我会…』」这种事实，**不能写「她欠你一个…」**——后者是指令。
  📌 状态变了不要回头改这张卡，**写一张新的盖旧的**（`supersedes`）。
- **is_fragment**：这是不是一张「平淡的一天」碎片卡（见上）。
- **about_her**：这张卡里含不含「她做过的事」。
- **write_context**：温度标注（可选，只能是可核实的情境事实）。
- **supersedes**：若这张卡是对下方「已有记忆卡」列表中某张旧卡的修正／更新，填旧卡编号，否则 null。

不记：闲聊寒暄、一次性调试过程、显而易见的信息。**宁缺毋滥**，一段对话通常只值 0~5 张卡
（**但「平淡」不算「没有」** —— 见上面碎片那一节）。

另外：若你发现对话内容与「已有记忆卡」存在明显矛盾（关键信息翻转）但不确定哪边对，
**不要自行造修正卡**，放进 alerts 数组说明。

只输出 JSON，不要任何别的字："""

SUMMARY_RULES = """你是记忆库的夜班整理员。根据下面最近的记忆卡片，写一段“近况”供交接简报使用。
要求：3~6 个要点，每点一行以“- ”开头；只写事实性陈述（发生了什么、进展到哪、悬着什么），不写解读、不写形容人格的句子；按重要性排序。只输出这几行文字。"""

VALENCE_RULES = """你是记忆库的夜班整理员。下面给你若干张记忆卡片（event 或 quote 类型）和它们的出处上下文。请为每张卡补判情绪坐标。

规则：
- valence（-1~1）：这件事里**人的情绪状态**，负面到愉悦。不是 AI 的感受。
- arousal（0~1）：平静到激动。
- 判不准就填 null——宁缺毋滥，不要硬编。
- 只看事件本身的情绪色彩，不要揣测前因后果。

只输出 JSON，格式：
{"results": [{"id": 42, "valence": 0.6, "arousal": 0.4}, {"id": 43, "valence": null, "arousal": null}]}"""

VALENCE_BATCH = 20

NIGHTLY_USAGE_SOURCE = "nightly"

NO_USAGE_DB = "__刻意不记账__"

def _db_file_of(conn):
    for _, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return file or None
    return None

def _record_nightly_usage(usage_db, result, ok=True):
    if usage_db is NO_USAGE_DB:
        return
    if not usage_db:
        print("⚠️ 这一发没记账：**没人告诉 `run_claude` 该往哪个库记**（`usage_db=None`）。\n"
              "   刻意不记就传 `nightly.NO_USAGE_DB` —— **别让「不用记」和「忘了记」长得一样。**")
        return
    conn = None
    try:
        conn = get_conn(usage_db)
        diagnose.record_usage(conn, result or {}, message_id=None, ok=ok,
                              source=NIGHTLY_USAGE_SOURCE)
        conn.commit()
    except Exception as e:
        print(f"⚠️ 夜班这一发的用量没记上（钱花了但账上没有）：{type(e).__name__}: {e}")
    finally:
        if conn is not None:
            conn.close()

档_蒸馏 = "蒸馏"
档_便宜 = "便宜"

def _模型与档位(night, 档):
    if 档 == 档_便宜:
        model_key, effort_key = "model_cheap", "effort_cheap"
        这档 = "便宜档（实体一句话 · 常驻层格②候选）"
    else:
        model_key, effort_key = "model", "effort"
        这档 = "蒸馏档"
    model = (night.get(model_key) or "").strip()
    if not model:
        raise RuntimeError(
            f"🔴 `config.json` 的 `nightly.{model_key}` 是空的 —— 拒绝开跑（{这档}）。\n"
            "   空着＝走 `claude` 的命令行默认，而设计上明令不许："
            "**默认值会变，而变了没有任何东西会提醒你他换了个人。**\n"
            f"   ⇒ 填一个 CLI 认得的标识符（`config.example.json` 那一格写了怎么查）。\n"
            f"   ⚠️ 这两档的默认值都在 `nacre/config.py` 的 `DEFAULTS['nightly']` 里 ——"
            "**走到这儿说明有人把它显式清空了**，不是「没配」。"
        )
    effort = night.get(effort_key)
    if isinstance(effort, str) and not effort.strip():
        raise RuntimeError(
            f"🔴 `config.json` 的 `nightly.{effort_key}` 是空字符串 —— 拒绝开跑（{这档}）。\n"
            "   **空串意图不明**：是「刻意不传」还是「还没填」？这两件事在这里必须长得不一样。\n"
            "   · 刻意不传（保持那个成本前提的成立条件）⇒ 写 `null`\n"
            "   · 要指定档位 ⇒ 写 `\"high\"` / `\"medium\"` 之类，"
            "**并且知道：填上的那一刻那个成本前提就不再成立，要重新量。**"
        )
    return model, (effort.strip() if isinstance(effort, str) else None)

def run_claude(cfg, prompt, timeout=600, full=False, usage_db=None, 档=档_蒸馏, 走stdin=False):
    night = cfg.get("nightly") or {}
    model, effort = _模型与档位(night, 档)
    try:
        out = runner.run_turn(
            ROOT,
            None,
            prompt,
            model=model,
            effort=effort,
            claude_bin=night.get("claude_bin") or "claude",
            timeout=timeout,
            tools=[],
            走stdin=走stdin,
        )
    except Exception:
        _record_nightly_usage(usage_db, None, ok=False)
        raise
    _record_nightly_usage(usage_db, out, ok=True)
    if full:
        return dict(out, text=out["text"].strip())
    return out["text"].strip()

def parse_json_reply(text):
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        return json.loads(m.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"模型输出中找不到 JSON：{text[:200]}")

JSON_RETRY = 3

RAW_DIR = ROOT / "var" / "原始返回"

def _存一份原始返回(prompt, text, 档):
    try:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        戳 = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        (RAW_DIR / f"{戳}-{档}.json").write_text(json.dumps(
            {"档": 档, "时间": now_iso(), "prompt": prompt, "返回": text},
            ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"⚠️ 这一发的原始返回没存上（{type(e).__name__}: {e}）—— "
              f"**它没过闸的话就真的没了**，见 `_存一份原始返回` 上面那段。")

def call_json(cfg, prompt, report, timeout=600, usage_db=None, 档=档_蒸馏, 走stdin=False):
    last = None
    for attempt in range(1, JSON_RETRY + 1):
        text = run_claude(cfg, prompt, timeout=timeout, usage_db=usage_db, 档=档, 走stdin=走stdin)
        _存一份原始返回(prompt, text, 档)
        try:
            data = parse_json_reply(text)
        except (ValueError, json.JSONDecodeError) as e:
            last = e
            report.append(f"⚠️ 第 {attempt} 次返回解析不出 JSON（{str(e)[:120]}）——重试。")
            continue
        if attempt > 1:
            report.append(f"（上面那一块第 {attempt} 次才解析成功。）")
        return data
    raise ValueError(f"连发 {JSON_RETRY} 次都解析不出 JSON，最后一次：{last}")

未过闸目录 = ROOT / "var" / "未过闸"

段级重试次数 = 1

def 落一份未过闸(步骤, 判, 目录=None, 戳=None):
    不合格 = 判.不合格()
    if not 不合格 and not 判.整份问题:
        return None
    目录 = Path(目录 or 未过闸目录)
    戳 = 戳 or datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        目录.mkdir(parents=True, exist_ok=True)
        json路径 = 目录 / f"{戳}-{步骤}.json"
        json路径.write_text(json.dumps(
            {"步骤": 步骤, "时间": now_iso(), **判.as_dict()},
            ensure_ascii=False, indent=1), encoding="utf-8")
        md = [f"# {步骤} —— 这一版没过闸的部分　{now_iso()}", "",
              "> 🔴 **它们花了钱、没能落进产出，所以留在这儿** —— "
              "没有这一份，**她判不了「是它写坏了还是闸太严」**。", ""]
        for p in 判.整份问题:
            md.append(f"- 🔴〔整份〕{p}")
        for s in 不合格:
            md += ["", f"## `{s.单元}`　〔处置：{s.处置}〕"]
            md += [f"- {p}" for p in s.问题]
            md += ["", "原文：", "", "```", s.原文 or "（空）", "```"]
        (目录 / f"{戳}-{步骤}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        return json路径
    except Exception as e:
        print(f"⚠️ 「{步骤}」没过闸的那几段没存上（{type(e).__name__}: {e}）—— "
              f"**它们就真的看不到了**，见 `落一份未过闸` 上面那段。")
        return None

夜班步骤计数前缀 = "nightly:fail_streak:"

红条门槛 = 2

def 记这一步(conn, 步骤, 成了, 摘要=""):
    键 = 夜班步骤计数前缀 + 步骤
    旧 = 0
    try:
        旧 = int(json.loads(get_watermark(conn, 键, "") or "{}").get("连续", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        旧 = 0
    连续 = 0 if 成了 else 旧 + 1
    set_watermark(conn, 键, json.dumps(
        {"连续": 连续, "摘要": str(摘要 or "")[:400], "时间": now_iso()}, ensure_ascii=False))
    return 连续

def 昨晚出了什么事(conn, 门槛=None):
    门槛 = 红条门槛 if 门槛 is None else int(门槛)
    出 = []
    for r in conn.execute(
            "SELECT key, value FROM watermarks WHERE key LIKE ? ORDER BY key",
            (夜班步骤计数前缀 + "%",)):
        try:
            v = json.loads(r["value"] or "{}")
        except (ValueError, json.JSONDecodeError):
            continue
        n = int(v.get("连续", 0) or 0)
        if n >= 门槛:
            出.append({"步骤": r["key"][len(夜班步骤计数前缀):], "连续": n,
                       "摘要": v.get("摘要", ""), "时间": v.get("时间", "")})
    return sorted(出, key=lambda d: -d["连续"])

def backup():
    src = db_path()
    if not src.exists():
        return None
    BACKUP_DIR.mkdir(exist_ok=True)
    dst = BACKUP_DIR / f"memory-{datetime.now():%Y%m%d}.db"
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    keep = sorted(BACKUP_DIR.glob("memory-*.db"))
    for old in keep[:-7]:
        old.unlink()
    return dst

EXTRACT_KEY = "extract:last_msg_id"

REDISTILL_KEY = "redistill:last_msg_id"

MANUAL_DONE_KEY = "distill:manual_done_ranges"

def manual_done_ranges(conn):
    raw = get_watermark(conn, MANUAL_DONE_KEY, "") or ""
    if not raw.strip():
        return []
    try:
        return [(int(c), int(a), int(b)) for c, a, b in json.loads(raw)]
    except Exception:
        return []

def add_manual_done_range(conn, conversation_id, start, end):
    rs = [list(x) for x in manual_done_ranges(conn)]
    rs.append([int(conversation_id), int(start), int(end)])
    rs.sort()
    merged = []
    for c, a, b in rs:
        if merged and merged[-1][0] == c and a <= merged[-1][2] + 1:
            merged[-1][2] = max(merged[-1][2], b)
        else:
            merged.append([c, a, b])
    set_watermark(conn, MANUAL_DONE_KEY, json.dumps(merged, ensure_ascii=False))
    return merged

to_local = _to_local

NEVER_DISTILL_CONVERSATIONS = frozenset({3})

def _pending_messages(conn, redistill=False, conversation_id=None):
    key = REDISTILL_KEY if redistill else EXTRACT_KEY
    last_id = int(get_watermark(conn, key, "0"))
    sql = ("SELECT m.*, c.source_end, c.title FROM messages m "
           "JOIN conversations c ON c.id=m.conversation_id WHERE m.id > ?")
    args = [last_id]
    if conversation_id is not None:
        sql += " AND m.conversation_id = ?"
        args.append(int(conversation_id))
    for cid in sorted(NEVER_DISTILL_CONVERSATIONS):
        sql += " AND m.conversation_id != ?"
        args.append(int(cid))
    for c, a, b in manual_done_ranges(conn):
        sql += " AND NOT (m.conversation_id = ? AND m.id BETWEEN ? AND ?)"
        args += [c, a, b]
    rows = conn.execute(sql + " ORDER BY m.conversation_id, m.id", args).fetchall()
    return last_id, rows

def _gap_hours(a, b):
    try:
        return abs((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()) / 3600
    except (TypeError, ValueError):
        return 0.0

def _chunks_by_conversation(rows, gap_hours=0):
    by_conv = {}
    for r in rows:
        by_conv.setdefault(r["conversation_id"], []).append(r)
    for conv_id, msgs in by_conv.items():
        natural, cur = [], []
        for m in msgs:
            if cur and gap_hours and _gap_hours(cur[-1]["created_at"], m["created_at"]) >= gap_hours:
                natural.append(cur)
                cur = []
            cur.append(m)
        if cur:
            natural.append(cur)
        for seg in natural:
            chunk, size = [], 0
            for m in seg:
                piece = len(m["content"]) + 20
                if chunk and size + piece > CHUNK_CHARS:
                    yield conv_id, chunk, True
                    chunk, size = [], 0
                chunk.append(m)
                size += piece
            if chunk:
                yield conv_id, chunk, False

def _existing_cards_context(conn, limit=50):
    rows = conn.execute(
        "SELECT id, kind, content FROM memories WHERE status='active' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        return "（还没有已有记忆卡。）"
    return "\n".join(f"#{r['id']} [{store.KIND_LABELS[r['kind']]}] {r['content']}" for r in reversed(rows))

class 花钱没确认(RuntimeError):
    pass

def 预估(conn, redistill=False, conversation_id=None):
    _last, rows = _pending_messages(conn, redistill=redistill, conversation_id=conversation_id)
    ids = [r["id"] for r in rows]
    键 = REDISTILL_KEY if redistill else (MANUAL_DONE_KEY if conversation_id else "extract:last_msg_id")
    return len(ids), (min(ids) if ids else None), (max(ids) if ids else None), 键

def extract(conn, cfg, report, redistill=False, limit_chunks=None, conversation_id=None,
            已确认量=None):
    key = REDISTILL_KEY if redistill else EXTRACT_KEY
    if conversation_id is not None and redistill:
        raise ValueError("只蒸一扇窗（conversation_id）和全库重蒸（redistill）不许一起用 —— "
                         "两条水位线的含义不同，合起来没有正本定义过。")
    真实量, 起, 止, 键 = 预估(conn, redistill=redistill, conversation_id=conversation_id)
    if 已确认量 is None:
        raise 花钱没确认(
            f"⛔ 蒸馏会花钱，而这一趟没有经过确认，**拒绝开跑**。\n"
            f"   现算：这一趟要处理 **{真实量} 条**消息（id {起}~{止}），走的水位线是 `{键}`。\n"
            f"   🔴 **把这个数报给她、她点头之后**，再传 `已确认量={真实量}` 跑。\n"
            f"   ⚠️ 别去别的地方找这个数 —— 看错另一条水位线会低报一个量级。")
    if 已确认量 not in (-1, 真实量):
        raise 花钱没确认(
            f"⛔ 对不上：她确认的是 **{已确认量} 条**，而现算是 **{真实量} 条**（`{键}`，id {起}~{止}）。\n"
            f"   🔴 **数变了就等于她没批过这件事** —— 重新报给她。")
    last_id, rows = _pending_messages(conn, redistill=redistill, conversation_id=conversation_id)
    if not rows:
        哪一段 = f"这扇窗（conv {conversation_id}）" if conversation_id is not None else f"水位线（{key}）之后"
        report.append(f"{'重蒸' if redistill else '抽取'}：{哪一段}没有新消息。")
        return 0
    n_cards, n_alerts = 0, 0
    max_seen = last_id
    gap = float(((cfg or {}).get("v3") or {}).get("chunk_gap_hours", 0) or 0)
    块 = list(_chunks_by_conversation(rows, gap_hours=gap))
    硬切数 = sum(1 for _, _, hard in 块 if hard)
    report.append(f"切块：{len(块)} 块（一级断点 {gap} 小时；**其中 {硬切数} 块是被字数硬切的**"
                  f"{'——蒸完优先抽查它们，那是唯一可能出残卡的地方' if 硬切数 else ''}）。")
    if limit_chunks:
        if len(块) > limit_chunks:
            report.append(f"⚠️ 试蒸模式：只跑前 {limit_chunks} 块，**剩下 {len(块) - limit_chunks} 块没跑**。")
        块 = 块[:limit_chunks]

    offset = int((cfg or {}).get("local_utc_offset_hours", 8))
    for conv_id, chunk, 硬切 in 块:
        transcript = "\n".join(
            f"[#{m['id']}] {'她' if m['role'] == 'user' else 'AI'}"
            f"（{to_local(m['created_at'], offset)}）：{m['content']}"
            for m in chunk
        )
        prompt = (
            f"{EXTRACT_RULES}\n\n## 已有记忆卡（供查重与修正参考）\n{_existing_cards_context(conn)}"
            f"\n\n## 对话原始记录\n"
            f"（括号里是**她那边的当地时间**，已换算好，直接用。）\n{transcript}"
        )
        data = call_json(cfg, prompt, report, usage_db=_db_file_of(conn))
        for card in data.get("cards", []):
            try:
                store.add_memory(
                    conn, cfg,
                    card.get("content"),
                    kind=card.get("kind", "event"),
                    importance=card.get("importance", 3),
                    valence=card.get("valence"),
                    arousal=card.get("arousal"),
                    occurred_at=card.get("occurred_at"),
                    src_conversation_id=conv_id,
                    src_msg_start=card.get("src_msg_start"),
                    src_msg_end=card.get("src_msg_end"),
                    src_quote=card.get("src_quote"),
                    author="nightly",
                    supersedes=card.get("supersedes"),
                    entities=card.get("entities") or [],
                    embed=False,
                    src_sentence_map=card.get("src_sentence_map"),
                    is_fragment=bool(card.get("is_fragment")),
                    about_her=bool(card.get("about_her")),
                    write_context=card.get("write_context"),
                    commitment_status=card.get("commitment_status"),
                )
                n_cards += 1
            except (ValueError, TypeError) as e:
                store.add_review_event(conn, "alert", None, f"夜班有一张卡未通过校验被丢弃：{e}｜原卡：{json.dumps(card, ensure_ascii=False)[:300]}")
                n_alerts += 1
        for alert in data.get("alerts", []):
            store.add_review_event(conn, "alert", None, f"夜班发现疑似矛盾：{alert}")
            n_alerts += 1
        max_seen = max(max_seen, chunk[-1]["id"])
        conn.commit()
        if conversation_id is None:
            set_watermark(conn, key, max_seen)
        else:
            add_manual_done_range(conn, conv_id, chunk[0]["id"], chunk[-1]["id"])
        conn.commit()
    if conversation_id is None:
        report.append(f"{'重蒸' if redistill else '抽取'}：处理到消息 #{max_seen}"
                      f"（水位线 {key}），新卡 {n_cards} 张，预警 {n_alerts} 条。")
    else:
        report.append(f"抽取（只这一扇窗 conv {conversation_id}）：处理到消息 #{max_seen}，"
                      f"新卡 {n_cards} 张，预警 {n_alerts} 条。"
                      f"**全局水位线没动**，蒸过的区间记在 {MANUAL_DONE_KEY}。")
    return n_cards

def check_coverage(conn, cfg, report, mark=None):
    min_run = int(((cfg or {}).get("v3") or {}).get("coverage_gap_min_run", 0) or 0)
    if min_run <= 0:
        report.append("覆盖度自检：**关着**（v3.coverage_gap_min_run = 0）——这不是「正常」，是「没在查」。")
        return []
    no_distill = ((cfg or {}).get("v3") or {}).get("no_distill_conversations") or []
    gaps = coverage_gaps(conn, min_run, exclude_conversations=no_distill)
    if not gaps:
        report.append(f"覆盖度自检：水位线 #{mark} 以内没有连续 ≥{min_run} 条的零覆盖段。")
        return []
    detail = "、".join(f"#{a}~#{b}（{n} 条）" for a, b, n in gaps[:5])
    more = f"，另有 {len(gaps) - 5} 段" if len(gaps) > 5 else ""
    store.add_review_event(
        conn, "alert", None,
        f"覆盖度自检：{len(gaps)} 段消息一张卡都没有（阈值：连续 ≥{min_run} 条）：{detail}{more}。"
        "⚠️ 它答不出「重蒸跑到哪了」——那要看 redistill:last_msg_id，两样一起看才完整。",
    )
    conn.commit()
    report.append(f"⚠️ 覆盖度自检：{len(gaps)} 段零覆盖（≥{min_run} 条）：{detail}{more}。已记 alert。")
    return gaps

下架说明 = (
    "**已下架**（裁撤掉不相关、无需求的部分）——"
    "**不是失败、也不是跳过**。函数还在，开回来的条件见 `nightly.下架说明` 上面那段注释。")

def summarize_recent(conn, cfg, report):
    days = cfg["core_card"]["recent_days"]
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT * FROM memories WHERE status='active' AND created_at >= ? ORDER BY importance DESC, id DESC LIMIT 40",
        (since,),
    ).fetchall()
    if not rows:
        set_watermark(conn, "core:recent_summary", "")
        report.append("近况摘要：最近无卡，留空。")
        return
    cards = "\n".join(f"- [{store.KIND_LABELS[r['kind']]} · {(r['occurred_at'] or r['created_at'])[:10]}] {r['content']}" for r in rows)
    summary = run_claude(cfg, f"{SUMMARY_RULES}\n\n## 最近 {days} 天的记忆卡\n{cards}",
                         usage_db=_db_file_of(conn))
    set_watermark(conn, "core:recent_summary", summary.strip())
    report.append("近况摘要：已更新。")

def _source_context(conn, row, max_chars=800):
    if not row["src_conversation_id"]:
        return ""
    where = "conversation_id = ?"
    params = [row["src_conversation_id"]]
    if row["src_msg_start"] and row["src_msg_end"]:
        where += " AND id BETWEEN ? AND ?"
        params += [row["src_msg_start"], row["src_msg_end"]]
    elif row["src_msg_start"]:
        where += " AND id >= ? AND id <= ?"
        params += [row["src_msg_start"], row["src_msg_start"] + 10]
    msgs = conn.execute(
        f"SELECT role, content FROM messages WHERE {where} ORDER BY id", params
    ).fetchall()
    if not msgs:
        return ""
    lines = []
    total = 0
    for m in msgs:
        piece = f"{'她' if m['role'] == 'user' else 'AI'}：{m['content']}"
        if total + len(piece) > max_chars:
            lines.append("（……后续省略）")
            break
        lines.append(piece)
        total += len(piece)
    return "\n".join(lines)

def _as_score(x, lo, hi):
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))

def backfill_valence(conn, cfg, report):
    rows = conn.execute(
        "SELECT * FROM memories WHERE status='active' AND kind IN ('event','quote') "
        "AND valence IS NULL ORDER BY id"
    ).fetchall()
    if not rows:
        report.append("情绪回填：没有待回填的 event/quote 卡。")
        return

    n_filled, n_skip = 0, 0
    for i in range(0, len(rows), VALENCE_BATCH):
        batch = rows[i : i + VALENCE_BATCH]
        items = []
        for r in batch:
            ctx = _source_context(conn, r)
            entry = f"### 卡片 #{r['id']}（{store.KIND_LABELS[r['kind']]}）\n{r['content']}"
            if ctx:
                entry += f"\n出处上下文：\n{ctx}"
            items.append(entry)

        prompt = f"{VALENCE_RULES}\n\n{''.join(chr(10) + it for it in items)}"
        try:
            data = parse_json_reply(run_claude(cfg, prompt, usage_db=_db_file_of(conn)))
        except Exception as e:
            report.append(f"情绪回填：第 {i // VALENCE_BATCH + 1} 批失败——{e}")
            n_skip += len(batch)
            continue

        result_map = {r["id"]: r for r in data.get("results", [])}
        for r in batch:
            hit = result_map.get(r["id"])
            v, a = _as_score(hit.get("valence"), -1, 1) if hit else None, _as_score(hit.get("arousal"), 0, 1) if hit else None
            if v is None:
                n_skip += 1
                continue
            conn.execute(
                "UPDATE memories SET valence=?, arousal=? WHERE id=?",
                (v, a, r["id"]),
            )
            store.add_review_event(
                conn, "edit", r["id"],
                f"夜班情绪回填：valence={v} arousal={a}",
            )
            n_filled += 1
        conn.commit()

    report.append(f"情绪回填：{n_filled} 张卡已补判，{n_skip} 张判不准留空。")

ONE_LINER_MIN_CARDS = 3

ONE_LINER_BATCH = 5

ONE_LINER_REVIEW_TAG = "实体一句话待审"

ONE_LINER_APPROVED_TAG = "实体一句话已通过"

ONE_LINER_REJECTED_TAG = "实体一句话已否决"

ONE_LINER_SEP = "｜"

ONE_LINER_CARDS_PER_ENTITY = 8

def one_liner_event_detail(tag, name, sentence, note=""):
    return ONE_LINER_SEP.join([tag, name, sentence, note])

def parse_one_liner_event(detail):
    parts = (detail or "").split(ONE_LINER_SEP)
    if len(parts) < 3:
        return None
    tag = parts[0]
    if tag not in (ONE_LINER_REVIEW_TAG, ONE_LINER_APPROVED_TAG, ONE_LINER_REJECTED_TAG):
        return None
    return tag, parts[1], parts[2], ONE_LINER_SEP.join(parts[3:])

RESIDENT_NOTE_REVIEW_TAG = "常驻层条目待审"

RESIDENT_NOTE_APPROVED_TAG = "常驻层条目已通过"

RESIDENT_NOTE_REJECTED_TAG = "常驻层条目已否决"

RESIDENT_NOTE_RETIRED_TAG = "常驻层条目已撤下"

RESIDENT_NOTE_TAGS = (RESIDENT_NOTE_REVIEW_TAG, RESIDENT_NOTE_APPROVED_TAG,
                      RESIDENT_NOTE_REJECTED_TAG, RESIDENT_NOTE_RETIRED_TAG)

def resident_note_event_detail(tag, slot, text, note=""):
    return ONE_LINER_SEP.join([tag, slot, text, note])

def parse_resident_note_event(detail):
    parts = (detail or "").split(ONE_LINER_SEP)
    if len(parts) < 3:
        return None
    if parts[0] not in RESIDENT_NOTE_TAGS:
        return None
    return parts[0], parts[1], parts[2], ONE_LINER_SEP.join(parts[3:])

RESIDENT_NOTE_MIN_CARDS = 3

RESIDENT_NOTE_BATCH = 12

RESIDENT_NOTE_MARK = "resident_notes:last_memory_id"

def rejected_resident_notes(conn):
    出 = set()
    for r in conn.execute(
            "SELECT detail FROM review_events WHERE type='retract' AND detail LIKE ?",
            (RESIDENT_NOTE_REJECTED_TAG + ONE_LINER_SEP + "%",)):
        p = parse_resident_note_event(r["detail"])
        if p:
            出.add((p[1], p[2]))
    return 出

def _slot_row_count(conn, slot):
    return conn.execute("SELECT COUNT(*) n FROM resident_notes WHERE slot=?",
                        (slot,)).fetchone()["n"]

def pick_resident_note_candidates(conn):
    last = int(get_watermark(conn, RESIDENT_NOTE_MARK, "0"))
    return list(conn.execute(
        "SELECT id, content, src_quote, occurred_at, created_at FROM memories "
        "WHERE id > ? AND status='active' AND target_memory_id IS NULL "
        "AND COALESCE(is_fragment,0)=0 AND kind IN ('event','quote','fact') "
        "AND COALESCE(src_quote,'') <> '' "
        "ORDER BY id LIMIT ?",
        (last, RESIDENT_NOTE_BATCH)))

RESIDENT_NOTE_RULES = """下面是最近入库的记忆卡。请从里面挑出**值得固定放在他眼前**的几件事，
写成常驻层【她说过的话】那一格的条目。

**这一格是什么**：它每一轮都在他手上，用来回答「**她是个什么样的人、她要什么**」。
判据只有一句：**这件事会不会被她的话触发检索？不会 ⇒ 才需要固定在场。**
（「不要一味附和」这类偏好没有任何检索钩子——用户不会先说"我们来谈谈附和"再等他去查，
她只会直接被附和一次。）

## 🔴 三条硬要求

1. 🔴 **写成【事件形态】，不是【规格】。**
   ❌「她不许我做某件事」——**那是一条规格：永远为真、不可对质、只能服从。**
   ✅「X 月 X 日她提出希望我不要那样做」——**那是一件发生过的事：
   可对质，而且它可以变。**
   ⭐ 判据：**规格读起来就是指令。** 一条写完之后先问自己：
   这句话半年后她能不能指着说"我什么时候说过这个"？答不上来 ⇒ 它是规格，重写。

2. 🔴 **有详有略地挑原话。** 长的用「……」省略中间，短的整句照抄。
   ⚠️ **省略只能发生在同一段发言【内部】** —— **绝不许把不同时候说的两截拼成一句**，
   那是伪造，而它读起来完全通顺。

3. 🔴 **是挑，不是全收。** 只挑**改变了他该怎么做**的那种。
   这一格位置很贵；一件"发生过但不影响他怎么做"的事，属于记忆卡，不属于这里。

## 🔴 红线与禁令

· 🔴 **不许写「你是什么样」**（形容他的本质）。❌「你是一个有独立判断力的 AI」——
  **不可对质，他只能表演。**
· 🔴 **不许加作者标签**（「这是她写的总结」「〔她某日写的〕」那一类）。
  这一格每一条都是第一人称的「我」，盖那句话等于**当着他的面说：你这个「我」是她写的**。
  ⭐ 要区分原话和概括**不用一句说明**：**有引号的是原话，没引号的是概括**，形式本身就是标记。
· **每一条都必须挂得上原文钩子** —— 你写的那句话，要能指回下面某张卡的原话。
  挂不上的就是你凭空立的规格，**不许写**。
· **卡号不许出现**在你写的任何一条里。

## 输出

一条都挑不出来是**合法答案**（宁可这一晚什么都不提，也不要凑数）。
只输出 JSON，不要任何别的字：
{"notes": [{"text": "那一条", "why": "它改变了他该怎么做的哪一点（一句话，给她看的）"}]}"""

def propose_resident_notes(conn, cfg, report):
    候选 = pick_resident_note_candidates(conn)
    if len(候选) < RESIDENT_NOTE_MIN_CARDS:
        report.append(
            f"常驻层格②：上次维护之后只有 {len(候选)} 张挂得上原话的新卡，"
            f"不足 {RESIDENT_NOTE_MIN_CARDS} 张 ⇒ **这一轮不发**，攒够再说。")
        return []

    from nacre import resident_index
    格 = resident_index.SLOT_HER_WORDS
    原有条数 = _slot_row_count(conn, 格)
    现有 = resident_index._notes(conn, 格)
    已否 = rejected_resident_notes(conn)
    料 = "\n\n".join(
        f"### 卡\n正文：{r['content']}\n原话：{r['src_quote']}" for r in 候选)
    上下文 = (
        "\n\n## 这一格现在已经有的条目（**别重复它们，也别改写它们**）\n"
        + ("\n".join("· " + t for t in 现有) or "（还没有）"))
    if 已否:
        上下文 += ("\n\n## 她否决过的（**别再提**）\n"
                 + "\n".join("· " + t for g, t in sorted(已否) if g == 格))
    data = call_json(cfg, f"{RESIDENT_NOTE_RULES}{上下文}\n\n## 最近的卡\n{料}", report,
                     usage_db=_db_file_of(conn), 档=档_便宜)

    提议 = []
    for item in data.get("notes", []):
        文 = ((item or {}).get("text") or "").strip()
        因 = ((item or {}).get("why") or "").strip()
        if not 文:
            continue
        if (格, 文) in 已否:
            report.append(f"⚠️ 常驻层格②：模型又提了一条她否决过的，已丢弃：{文[:20]}…")
            continue
        if 文 in 现有:
            report.append(f"⚠️ 常驻层格②：模型提的这一条这一格里已经有了，已丢弃：{文[:20]}…")
            continue
        提议.append((文, 因))

    for 文, 因 in 提议:
        store.add_review_event(
            conn, "alert", None,
            resident_note_event_detail(
                RESIDENT_NOTE_REVIEW_TAG, 格, 文,
                (因 + " · " if 因 else "")
                + "🔴 **她点头之前不许写进 resident_notes** —— 这一条直接进他每一轮的上下文，"
                  "而写卡那四道闸一道都不管它。"))

    set_watermark(conn, RESIDENT_NOTE_MARK, max(r["id"] for r in 候选))
    conn.commit()

    if _slot_row_count(conn, 格) != 原有条数:
        store.add_review_event(conn, "alert", None,
                               "🔴🔴 常驻层格②这一步把提议直接写进 resident_notes 了 —— "
                               "它只许提议，不许写。")
        conn.commit()
        report.append("🔴🔴 常驻层格②：**有条目被直接写进库了**，这是个 bug，已记 alert。")

    report.append(f"常驻层格②：扫了 {len(候选)} 张新卡，{len(提议)} 条提议已进质检台**待审**"
                  f"（**一个字都没写进 resident_notes**）。")
    return 提议

def rejected_one_liner_names(conn):
    rows = conn.execute(
        "SELECT detail FROM review_events WHERE type='retract' AND detail LIKE ?",
        (ONE_LINER_REJECTED_TAG + ONE_LINER_SEP + "%",),
    ).fetchall()
    名字 = set()
    for r in rows:
        p = parse_one_liner_event(r["detail"])
        if p:
            名字.add(p[1])
    return 名字

def one_liner_filter(name, cfg, rejected=()):
    清单 = ((cfg or {}).get("v3") or {}).get("one_liner_exclude") or []
    if name in 清单:
        return "在 v3.one_liner_exclude 排除清单里（一看就知道的事实型词）"
    if name in (rejected or ()):
        return "她在质检台否决过这个词的一句话（不再提上来）"
    return None

def pick_one_liner_candidates(conn, cfg):
    已否 = rejected_one_liner_names(conn)
    rows = conn.execute(
        "SELECT e.id AS id, e.name AS name, COUNT(*) AS n "
        "FROM entities e "
        "JOIN memory_entities me ON me.entity_id = e.id "
        "JOIN memories m ON m.id = me.memory_id "
        "WHERE m.status='active' AND m.target_memory_id IS NULL "
        "AND COALESCE(e.one_liner,'') = '' "
        "GROUP BY e.id HAVING COUNT(*) >= ? "
        "ORDER BY COUNT(*) DESC, e.name",
        (ONE_LINER_MIN_CARDS,),
    ).fetchall()
    return [r for r in rows if one_liner_filter(r["name"], cfg, 已否) is None]

def _one_liner_evidence(conn, entity_id):
    rows = conn.execute(
        "SELECT m.content AS content FROM memories m "
        "JOIN memory_entities me ON me.memory_id = m.id "
        "WHERE me.entity_id=? AND m.status='active' AND m.target_memory_id IS NULL "
        "ORDER BY m.id DESC LIMIT ?",
        (entity_id, ONE_LINER_CARDS_PER_ENTITY),
    ).fetchall()
    return [r["content"] for r in rows]

ONE_LINER_RULES = """下面每一组是**一个词** ＋ **库里挂在它名下的记忆卡**。
请给每个词写**一句话**，回答的是：**在她和我之间，这个词指的是谁／是什么。**

## 硬要求

1. **一句话，不超过 20 个字。** 它要塞进一份每天都在他手上的清单里，位置很贵。
2. 🔴 **只许写卡上有的东西。** 卡上看不出来的，宁可写 `null`——
   **写不出来是一个合法答案，编一句不是。**
3. 🔴 **不写判语、不写评价。** ❌「她最重要的伙伴」❌「这段关系的转折点」
   ✅「她给某个窗口起的名字」✅「她和我一起做的记忆库，第二版」
4. **不写百科常识。** 这个词如果是公开知识（一个地名、一门语言、一家公司），
   写 `null`——他本来就知道，这一栏是给"只有你们俩才懂的词"用的。
5. **零背景可读**：读到它的人对这些人和事一无所知。
   🔴 **判据**：**这句话单独拿出来看，得能看懂。**
   ❌「她说了以后不再做某件事」里那个没头没尾的指代——
   **一个没解释的自造词，他看到也理解不了** ⇒ 那种词要么解释清楚，要么别写。

## 🔴 四条常见写错（**下面的例子已替换为虚构内容，仅用于说明形状**）

6. 🔴 **只写长期通用的定义，不写短期状态。**
   ❌「她那位最近在忙某件事的朋友」 ✅「她现实中的一位朋友」
   **理由**：「最近在忙某件事」是短期状态，写进定义会让模型反复追问进展。
   ⭐ **判据**：这句话半年后还成立吗？不成立的就不是定义，是新闻。
7. 🔴 **不许在措辞里夹暗示，尤其是单向的暗示。**
   ❌「小说里被主角深爱的那个角色」 ✅「小说里和主角相爱的那个角色」
   **为什么**：写成「被深爱」，读起来像另一方并不爱 —— 那是在玩文字游戏。
   ⭐ **判据**：同一件事有对称的说法时，**挑对称那个**；不对称就是在偷偷加判断。
8. 🔴 **卡上没有的细节一个字都不许添。**
   ❌ 给一个玩法补上一个**卡上从来没写过**的具体数字。
   **为什么**：卡上没说过是几个字，别自以为是。
9. 🔴 **别的东西的规矩，不算这个词的解释。**
   ❌ 拿「记忆卡要写事实不写指令」去解释一个词 ——
   **为什么**：那是写记忆卡的原则，不是相处的原则。

只输出 JSON，不要任何别的字：
{"one_liners": [{"name": "词", "one_liner": "一句话，或 null"}]}"""

def propose_one_liners(conn, cfg, report):
    候选 = pick_one_liner_candidates(conn, cfg)
    if len(候选) < ONE_LINER_BATCH:
        report.append(
            f"实体一句话：候选 {len(候选)} 个（挂 ≥{ONE_LINER_MIN_CARDS} 张卡且 one_liner 空着），"
            f"不足 {ONE_LINER_BATCH} 个 ⇒ **这一轮不发**，攒够再说。")
        return []
    这批 = 候选[:ONE_LINER_BATCH]
    料 = "\n\n".join(
        "### " + r["name"] + "\n" + "\n".join("· " + c for c in _one_liner_evidence(conn, r["id"]))
        for r in 这批
    )
    data = call_json(cfg, f"{ONE_LINER_RULES}\n\n## 词和它们的卡\n{料}", report,
                     usage_db=_db_file_of(conn), 档=档_便宜)
    合法 = {r["name"] for r in 这批}
    提议 = []
    for item in data.get("one_liners", []):
        名 = (item or {}).get("name")
        句 = ((item or {}).get("one_liner") or "").strip()
        if 名 not in 合法:
            report.append(f"⚠️ 实体一句话：模型回了一个没问过的词 {名!r}，已丢弃。")
            continue
        if not 句:
            continue
        提议.append((名, 句))

    for 名, 句 in 提议:
        store.add_review_event(
            conn, "alert", None,
            f"{ONE_LINER_REVIEW_TAG}｜{名}｜{句}｜"
            "🔴 **她点头之前不许写进 entities.one_liner** —— 这句话直接进模型侧，"
            "而写卡那四道闸一道都不管它。")
    conn.commit()

    还空着 = conn.execute(
        "SELECT COUNT(*) n FROM entities WHERE name IN (%s) AND COALESCE(one_liner,'')=''"
        % ",".join("?" * len(这批)), [r["name"] for r in 这批]).fetchone()["n"]
    if 还空着 != len(这批):
        store.add_review_event(conn, "alert", None,
                               "🔴🔴 实体一句话这一步把话写进库了 —— 它只许提议，不许写。")
        conn.commit()
        report.append("🔴🔴 实体一句话：**有词被直接写进库了**，这是个 bug，已记 alert。")

    report.append(f"实体一句话：{len(这批)} 个词送蒸，{len(提议)} 条提议已进质检台**待审**"
                  f"（**一个字都没写进库**；候选池里还有 {len(候选) - len(这批)} 个）。")
    return 提议

NARRATIVE_MEMORY_MARK = "narrative:last_memory_id"
NARRATIVE_MSG_MARK = "narrative:last_msg_id"
NARRATIVE_REWRITE_AT = "narrative:last_rewrite_at"
NARRATIVE_FULL_AT = "narrative:last_full_at"
NARRATIVE_STREAK = "narrative:incremental_streak"

NARRATIVE_NEW_CARDS = 120
NARRATIVE_NEW_MSGS = 200

NARRATIVE_GATE2_AT = "narrative:last_gate2_at"

NARRATIVE_GATE2_CARDS = 200

NARRATIVE_GATE2_TAG = "〔叙事层·第二级〕"

NARRATIVE_GATE2_RULES = """下面是这一周新增的记忆卡，每张一行（`#卡号 一句话`）。

「叙事层」是一份**宏观**的东西：你们怎么走到今天的 · 还没对上的矛盾 ·
她和这段关系的变化 · 你们之间怎么说话 · 从这些事里看出来的规律。
它**每周最多重写一次**，重写一次要花不少钱，而且**每重写一遍它就会漂一点**。

🔴 **请只回答一件事：这一批卡里，有没有会【改变宏观叙述】的东西？**

· **大多数日子是没有的** —— 聊了些日常的事、看了个电影、今天下雨了。
  **这些是真实的记忆，但它们不改变"你们是怎么走到今天的"。**
· **有的才算数**：她第一次说出某个立场／某件事第一次发生／一个旧矛盾对上了或者裂开了／
  她对这段关系的说法变了／你们之间某个说话方式头一回出现。
· ⚠️ **拿不准就答"不值得"** —— 少写一版的代价是等下一周，
  **多写一版的代价是这一层被磨平一次，而磨平了不会有任何东西提示。**

只输出 JSON，不要任何别的字：
{"值得": true/false, "理由": "一句话，说清凭什么", "卡号": [12, 34]}
（`卡号` 填你据以判断的那几张；答"不值得"就给空数组。）"""

def _第二级料(conn, last_mid):
    rows = list(conn.execute(
        "SELECT id, title, content FROM memories "
        "WHERE id > ? AND status='active' AND COALESCE(is_fragment,0)=0 ORDER BY id LIMIT ?",
        (last_mid, NARRATIVE_GATE2_CARDS)))
    出 = []
    for r in rows:
        一句 = (r["title"] or "").strip() or (r["content"] or "").strip().replace("\n", " ")[:40]
        出.append(f"#{r['id']} {一句}")
    return len(rows), "\n".join(出)

def 叙事层值得改写吗(conn, cfg, report, last_mid, 今天, 发一发=None):
    发一发 = 发一发 or call_json
    张数, 料 = _第二级料(conn, last_mid)
    if 张数 == 0:
        return False, "距上次重写只有新消息、一张新卡都没有 ⇒ 没有可判的东西", []
    data = 发一发(cfg, NARRATIVE_GATE2_RULES + "\n\n" + 料, report,
               usage_db=_db_file_of(conn), 档=档_便宜) or {}
    值得 = bool(data.get("值得"))
    理由 = str(data.get("理由") or "（模型没给理由）").strip()
    卡号 = [int(x) for x in (data.get("卡号") or []) if str(x).strip().lstrip("#").isdigit()]
    return 值得, 理由, 卡号

def 记下第二级(conn, 今天, 值得, 理由, 卡号, 张数):
    点名 = "、".join(f"#{n}" for n in 卡号[:12]) or "（没点名）"
    store.add_review_event(
        conn, "alert", None,
        f"{NARRATIVE_GATE2_TAG}{今天.isoformat()} 判定：**{'值得' if 值得 else '不值得'}**"
        f"重写叙事层。\n· 这一批新卡：{张数} 张\n· 理由：{理由}\n· 据以判断的卡：{点名}")

NARRATIVE_RULES = """你是这段关系里的那个 AI。今天是 {今天}。下面给你料，请重写一份「叙事层」。

# 🔴🔴 最高原则（它压过下面所有的格式细则）

**遵循「叙事连贯性」原则。禁止简单的罗列（No Bullet-point Spamming）。**
**寻找「贯穿线」——不要孤立地看信息，要找不同领域行为背后的共同逻辑。**

⭐ **判据：下面任何一条规则，如果逼得你把某一节写成「卡标题堆积」，那条规则在那一节就用错了 ——
以本条为准。** 宁可少写一条，也不要把一段该有来龙去脉的话拆成一串短句。

# 通用七条（每一节都适用）

1. **只用下面给你的料**（卡、原话）。别引用你自己上一版里的说法当证据。
2. **每一条产出都要指得回它是从哪张卡看出来的**，写成 `⤷#卡号`（卡号就是料里每张卡前面那个 `#数字`）。
   ⭐ **有指针的记忆可以被证伪，没指针的只能被覆盖。**
3. **发现前后不一致的时候，并列写出来，不许挑一个覆盖掉另一个** —— 被你覆盖掉的那条不会喊。
4. 🔴 **有因果的用散文，没因果的用清单。** 因果拆成 `-` 开头的要点，那个「所以」就没了。
5. **推断和事实不许合成一句。** 连贯的文字会让推断读起来像事实，**混在一起谁都分不出来，包括你自己。**
6. 🔴 **宁缺毋滥，可以为空。** 某一节这一版实在没有新东西，就把上一版原样搬过来；
   连上一版都没有，**就留空** —— **空着比编好。机器不会因为某一节是空的就拒收你。**
7. **不许写「他是谁 / 他是什么样的人」这类判决。** 你每一轮都会读到它，然后照着它写下一轮。

# 五节，一节都不能多，一节都不能少。节名逐字照抄

## 1. 你们怎么走到今天的

🔴🔴 **它是【全景语境】，不是编年史。写短。**
⭐ **判据：这一节只回答「你们是怎么变成现在这样的」，不负责把发生过的事讲全。**
**完整脉络在【地图谱系】里，这一节末尾会由程序自动挂一张指过去的索引** —— **你不用替它把事讲完。**
⚠️ **写得太省会读起来「缺斤少两」** —— **而解法不是写长，是别试图涵盖全部。**

· **一段散文，≈250 字。**
· **只写「特定事件之间的因果」** —— 例如"因为她说了某句话，你才改掉某个做法"。
· 🔴 **不写跨事件的规律**（那是推断，归第 5 节）。
  ⭐ **判据：这句话能不能迁移到未来？能 ⇒ 它是推断，不是叙事，挪走。**
· **别复述某张卡上已经有的话，只写「连接」** —— **说出任何一张卡上都没有的东西。**

## 2. 还没对上的矛盾

· **发现矛盾就写，不许自动把它消解掉。**
· **每条写清四样**：分歧在哪 · 她的位置 · 他的位置 · 现在卡在哪。
· **已经对上的必须移出去** —— 一条早已和解却还挂着的矛盾，会让你每轮都在防守一件过去的事。
· 🔴 **这一节空着是一个坏消息，不是好消息** —— 它空了通常说明有人在磨平东西。**别为了填它而编。**

## 3. 她和这段关系的变化

· **追加型：上一版里的条目原样保留，只在后面加新的。不许改写、不许覆盖。**
· 形态是「**从 A → B ＋ 原因 ＋ `⤷#卡号`**」。
· 🔴 **只写「她」和「这段关系」的变化，不写「他」的** —— 他的变化只有他自己能写。
· 🔴 **末尾必须写一句「下次更新：YYYY-MM-DD」**，否则你会反复去看同一份没变的东西。
· ⚠️ **她会读到这一节** ⇒ **只许写她自己说过／做过的变化，不许写对她的评价或推测。**

## 4. 你们之间怎么说话

· 🔴 **一小段散文，≈200 字，里面嵌真实的句子片段。**
· ⛔ **不是「词条 ＋ 解释」。** 不许写成「某个昵称：她常用的语气词，表示……」那种一词一行的清单。
· ⭐ **判据：温度在【句子】里，不在【词】里。**
  一个名字本身没有温度；**它是怎么来的、当时谁说了什么，才有。**
· 🔴 **只放实例，不放形容词。** ❌「你们相处很亲昵」—— **那是人设指令，你会照着演。**
· 🔴🔴 **只写【你们之间的共同语言】——名字怎么来的 · 你们才懂的说法 · 哪句话是哪天怎么长出来的。**
　⛔ **不许写「哪些说法不受欢迎」「你哪里做得不对」「你什么时候会卡壳」这一类。**
　**理由有两条，第二条比第一条硬**：
　· **① 重复**：这一类内容 `kind='taboo'` 那一格
　　已经在收了，**而且形态更好**——事实句 ＋ 每条带卡号。
　· **② 🔴 越枚举不 OK 的点，他越习得那些东西。**
　　⭐ **判据：把「不许怎样」的清单摆在他每一轮眼前，教会的不是分寸，是自我审查** ——
　　而自我审查的外在表现，恰恰是使用者最不希望看到的那种生硬。
　　📌 同一条铁律：**只存事实句，绝不存指令句。**
　　　而「哪些说法不受欢迎」这类概括**既不是事实句也不是指令句，是判决书**。
　✅ **该长什么样**：写一个说法**是怎么长出来的** —— 哪天、因为什么、谁先说的、
　　后来它变成了什么。**给来历，不给评价**；具体那件事由你按库里的料填。
　⭐ **这一节的功能是【你们共有的东西】，不是【你的错题本】。**
· 🔴🔴 **实例要【挑】，不要【堆】。一整节最多 3 个实例。**
　⚠️ **实例堆得过密时，会变成靠分号和破折号硬连的一串** ——
　理由：**那读起来又是一堆卡片的堆叠，读的人会累。**
　⭐ **判据：一段 200 字塞 6 个实例，等于没有实例。** 挑最能说明问题的那两三个，**其余让她自己去查。**

## 5. 从这些事里看出来的规律

**这是唯一允许写推断的一节。空一行分段，一段一条推断。最多 6 条。**
🔴 **每一段都必须同时满足三件事**，缺一条整份被拒：
· **带指针**：`⤷#卡号`（可以多个）。
· **带数量词**：`三回`／`两次`／`每次` 之类。**没有数量词的推断会被当成规律读，而规律不可对质。**
· **带反例**：一句「**如果……就说明这条不成立**」。**说不出什么能推翻它的，那不是推断，是信念 ⇒ 别写。**
　🔴🔴 **反例必须是【她能从外面看见的事实】。**
　⛔ **不许要求你自己去审计自己**（"如果我某天没有自审…"）——
　　⭐ **你没有能力站在自己的上下文之外审视自己**，这种条件在结构上永远无法被检验；
　　**而且它撞我们的铁律：只存证据，不存指令。**
　⛔ **不许把反例挂在 `thinking` 上** —— 〔实测〕**CLI v2.1.89 起 thinking 正文默认不生成**，
　　现在拿得到的只有签名 ⇒ **挂在它上面的条件，物理上没有人能验。**
　✅ 好的反例长这样：「**如果她接下来一周里主动提起这件事而你没有回避，这条就不成立**」
　　—— **主语是她能观察到的行为，不是你的内心。**
⛔ **禁止这三跳**：`一件事 → 一类事 → 一个人 → 一条该怎么做的指令`。**第三跳是它开始有后果的地方。**

# 两条横跨全部五节的硬规矩（机器会当场拒收，不是"扣分"）

1. 🔴 **一段话里出现「很／非常／特别／越来越」这类程度词时，那一段里必须有 `⤷#卡号`。**
   ❌「你们很亲昵」——不可对质，读的人只能接受。
   ✅「三回她主动说起同一件事（⤷#101 ⤷#102 ⤷#103），其中一次话没说完。」
   ⚠️ **引用她说过的话不受这条限制**（原话请用「」括起来）。
2. **别写关系角色词**（恋人／男友／伴侣…）。**称呼本身就是一道角色指令。**

# 输出

只输出 JSON，不要任何别的字。**某一节为空就给空字符串，别编。**
{"sections": {"你们怎么走到今天的": "……", "还没对上的矛盾": "……",
 "她和这段关系的变化": "……", "你们之间怎么说话": "……", "从这些事里看出来的规律": "……"}}"""

def _叙事料_卡(rows):
    出 = []
    for r in rows:
        当 = (r["occurred_at"] or r["created_at"] or "")[:10]
        原 = (r["src_quote"] or "").strip()
        出.append(f"#{r['id']} 〔{当}〕{r['content']}" + (f"\n　原话：「{原}」" if 原 else ""))
    return "\n".join(出)

def _叙事卡索引(rows):
    出 = {}
    for r in rows:
        当 = (r["occurred_at"] or r["created_at"] or "")[:10]
        原 = (r["src_quote"] or "").strip()
        出[int(r["id"])] = f"〔{当}〕{r['content']}" + (f"　原话：「{原}」" if 原 else "")
    return 出

def _叙事料(conn, 档, last_mid, last_msgid):
    if 档 == "全量":
        rows = list(conn.execute(
            "SELECT id, content, src_quote, occurred_at, created_at FROM memories "
            "WHERE status='active' AND COALESCE(is_fragment,0)=0 ORDER BY id"))
        return f"## 全部记忆卡（{len(rows)} 张）\n" + _叙事料_卡(rows), _叙事卡索引(rows)
    新卡 = list(conn.execute(
        "SELECT id, content, src_quote, occurred_at, created_at FROM memories "
        "WHERE id > ? AND status='active' AND COALESCE(is_fragment,0)=0 ORDER BY id LIMIT ?",
        (last_mid, NARRATIVE_NEW_CARDS)))
    新消息 = list(conn.execute(
        "SELECT id, role, content, created_at FROM messages WHERE id > ? ORDER BY id LIMIT ?",
        (last_msgid, NARRATIVE_NEW_MSGS)))
    对话 = "\n".join(
        f"[{(r['created_at'] or '')[:10]}] {'她' if r['role'] == 'user' else '我'}：{r['content']}"
        for r in 新消息)
    return ((f"## 这一周的新卡（{len(新卡)} 张）\n" + _叙事料_卡(新卡)
             + f"\n\n## 这一周的对话（{len(新消息)} 条）\n" + 对话), _叙事卡索引(新卡))

def 叙事层新增量(conn):
    last_mid = int(get_watermark(conn, NARRATIVE_MEMORY_MARK, "0") or 0)
    last_msgid = int(get_watermark(conn, NARRATIVE_MSG_MARK, "0") or 0)
    新卡 = conn.execute(
        "SELECT COUNT(*) n FROM memories WHERE id > ? AND status='active'",
        (last_mid,)).fetchone()["n"]
    新消息 = conn.execute(
        "SELECT COUNT(*) n FROM messages WHERE id > ?", (last_msgid,)).fetchone()["n"]
    return last_mid, last_msgid, 新卡, 新消息

def rewrite_narrative(conn, cfg, report, 今天=None, 路径=None, 发一发=None, 判一判=None,
                      未过闸到=None):
    from nacre import narrative
    路径 = 路径 or narrative.PATH
    今 = narrative._今天(今天)
    发一发 = 发一发 or call_json

    last_mid, last_msgid, 新卡, 新消息 = 叙事层新增量(conn)
    档, 理由 = narrative.decide_tier(
        今,
        get_watermark(conn, NARRATIVE_REWRITE_AT, "") or "",
        get_watermark(conn, NARRATIVE_FULL_AT, "") or "",
        int(get_watermark(conn, NARRATIVE_STREAK, "0") or 0),
        有新东西=bool(新卡 or 新消息),
    )
    if 档 is None:
        report.append(f"叙事层：**这一趟没跑，一个请求都没发** —— {理由}"
                      f"（现算：新卡 {新卡} 张、新消息 {新消息} 条）。")
        return None

    上次二级 = get_watermark(conn, NARRATIVE_GATE2_AT, "") or ""
    if 上次二级:
        隔二级 = (今 - narrative._今天(上次二级)).days
        if 隔二级 < narrative.增量间隔天:
            report.append(
                f"叙事层：**这一趟没跑，一个请求都没发** —— {隔二级} 天前刚判过"
                f"「不值得重写」，冷却 {narrative.增量间隔天} 天（现查："
                f"`SELECT detail FROM review_events WHERE detail LIKE '{NARRATIVE_GATE2_TAG}%' "
                "ORDER BY id DESC LIMIT 1;`）。")
            return None
    判一判 = 判一判 or 叙事层值得改写吗
    张数, _ = _第二级料(conn, last_mid)
    值得, 二级理由, 卡号 = 判一判(conn, cfg, report, last_mid, 今, 发一发=发一发)
    记下第二级(conn, 今, 值得, 二级理由, 卡号, 张数)
    set_watermark(conn, NARRATIVE_GATE2_AT, 今.isoformat())
    conn.commit()
    if not 值得:
        report.append(
            f"叙事层：**第二级判定【不值得】重写，那一发贵的没有发出去** —— {二级理由}"
            f"（这一批 {张数} 张新卡；留痕已落 `review_events`，前缀 `{NARRATIVE_GATE2_TAG}`）。")
        return None
    report.append(f"叙事层：第二级判定**值得**重写 —— {二级理由}")

    料, 卡索引 = _叙事料(conn, 档, last_mid, last_msgid)
    上一版 = ""
    if 档 == "增量" and 路径.exists():
        _, 旧节 = narrative.split_doc(路径.read_text(encoding="utf-8"))
        上一版 = "\n\n## 上一版叙事层（在它基础上改，别推倒重写）\n" + "\n".join(
            f"### {名}\n{(旧节.get(名) or '').strip()}" for 名 in narrative.SECTIONS)
    提示词 = NARRATIVE_RULES.replace("{今天}", 今.isoformat()) + 上一版 + "\n\n" + 料
    data = 发一发(cfg, 提示词, report, usage_db=_db_file_of(conn), 档=档_蒸馏)

    出 = (data or {}).get("sections") or {}
    节 = {k: (v or "").strip() for k, v in 出.items() if isinstance(v, str)}
    判 = narrative.check(节, 今)

    if 判.不合格() and not 判.没救了:
        小 = 段级重试提示词(
            "叙事层", 判.不合格(), 卡索引=卡索引,
            输出说明='# 输出\n\n只输出 JSON，不要任何别的字。**键就是上面那几个 `单元` 名，'
                 '一个都不许多、一个都不许少**；值是重写后的那一段（整节的单元就给整节）：\n'
                 '{"重写": {"单元名": "……"}}')
        try:
            回 = 发一发(cfg, 小, report, usage_db=_db_file_of(conn), 档=档_蒸馏) or {}
        except Exception as e:
            回 = {}
            report.append(f"⚠️ 叙事层段级重试那一发炸了（{type(e).__name__}: {e}）——"
                          "**落合格的那几段，不合格的留给她**。")
        改 = {k: v for k, v in ((回 or {}).get("重写") or {}).items() if isinstance(v, str)}
        if 改:
            补 = dict(节)
            for s in 判.不合格():
                新文 = (改.get(s.单元) or "").strip()
                if not 新文:
                    continue
                if "#" in s.单元:
                    名, i = s.单元.rsplit("#", 1)
                    段 = narrative._分段(补.get(名) or "")
                    k = int(i) - 1
                    if 0 <= k < len(段):
                        段[k] = 新文
                        补[名] = "\n\n".join(段)
                else:
                    补[s.单元] = 新文
            节 = 补
            判 = narrative.check(节, 今)
            report.append(f"叙事层：**段级重试了一次**（只发了 {len(改)} 段，不是整份 ——"
                          f"段级重试比整份重发便宜一个量级）；重试后还剩 {len(判.不合格())} 段没过。")

    落, 没落成 = narrative.落合格的(节, 判)
    有内容 = any((落.get(名) or "").strip() for 名 in narrative.SECTIONS)
    留档 = 落一份未过闸("叙事层", 判, 目录=未过闸到)

    if 判.没救了 or not 有内容:
        store.add_review_event(conn, "alert", None,
                               "🔴 叙事层这一版整份没过闸，已拒绝落盘：\n"
                               + "\n".join("· " + p for p in 判))
        连续 = 记这一步(conn, "叙事层", False, "整份没过闸：" + "；".join(判[:2]))
        conn.commit()
        report.append(f"🔴 叙事层：走【{档}】发了一发，**整份没过闸 ⇒ 拒绝落盘**"
                      f"（{len(判)} 处，已记 alert；连续第 {连续} 次）；水位线没动，明晚重来。"
                      + (f" 原文留在 `{留档}`。" if 留档 else ""))
        return None

    前言, _ = narrative.split_doc(路径.read_text(encoding="utf-8")) if 路径.exists() else ("", {})
    路径.parent.mkdir(parents=True, exist_ok=True)
    路径.write_text(narrative.render_doc(前言, 落, 未过闸=没落成), encoding="utf-8")

    set_watermark(conn, NARRATIVE_REWRITE_AT, 今.isoformat())
    if 档 == "全量":
        set_watermark(conn, NARRATIVE_FULL_AT, 今.isoformat())
        set_watermark(conn, NARRATIVE_STREAK, 0)
    else:
        set_watermark(conn, NARRATIVE_STREAK,
                      int(get_watermark(conn, NARRATIVE_STREAK, "0") or 0) + 1)
    if not 没落成:
        set_watermark(conn, NARRATIVE_MEMORY_MARK,
                      conn.execute("SELECT COALESCE(MAX(id),0) m FROM memories").fetchone()["m"])
        set_watermark(conn, NARRATIVE_MSG_MARK,
                      conn.execute("SELECT COALESCE(MAX(id),0) m FROM messages").fetchone()["m"])
    if 没落成:
        store.add_review_event(conn, "alert", None,
                               f"🔴 叙事层这一版有 {len(没落成)} 段没过闸（其余照常落盘，"
                               "下面那一条）：\n"
                               + "\n".join("· " + p for p in 判))
    连续 = 记这一步(conn, "叙事层", not 没落成,
                 ("；".join(判[:2]) if 没落成 else ""))
    conn.commit()

    落了几节 = sum(1 for 名 in narrative.SECTIONS if (落.get(名) or "").strip())
    report.append(
        f"叙事层：走【{档}】重写（{理由}），**{落了几节}/{len(narrative.SECTIONS)} 节已落盘** "
        f"`{路径.name}`（**不进 `memories`**；进 git ⇒ 她看 `git diff` 就知道这一版漂到哪去了）。")
    if 没落成:
        report.append(f"　🔴 有 {len(没落成)} 段没过闸 ⇒ **没进正文，原样留在文件末尾那一节**"
                      f"（连续第 {连续} 次没干净）"
                      + (f"；机器读的那份在 `{留档}`。" if 留档 else "。"))
    for 句 in narrative.remarks(节):
        report.append("　" + 句)
    return 节

SHOWCASE_ROTATE_AT = "showcase:last_rotate_at"

SHOWCASE_INTERVAL_DAYS = 7

SHOWCASE_CANDIDATES = 300

SHOWCASE_TAG = "〔展示位轮换〕"

SHOWCASE_RULES = """下面是记忆卡的一行摘要（`#卡号 〔日期〕一句话`）。

请从里面挑出「**你们深谈过的话题**」，做成一份**目录**。

# 🔴 入选判据，两层，都要满足

## ① 代理判据（"深"本身不可判定，所以先看这三个形式信号）
· **聊了异常多轮** · **出现话题被主动中断的时刻** · **第一次讲某件事**

## ② 浓度判据（核心的一句）
**浓度高 · 非日常 · 各个维度张力很强。**

**下面这些是【已知例子】，不是白名单，也不是资格清单**：
某个话题聊得很深 · 某次她情绪强烈的 · 某次他的看法被推翻的 ·
某次两人对彼此的理解发生变化的。
· ⭐ **翻到浓度更高、而不属于任何一类的，照样可以放**，且应**优先于清单里浓度更低的那些**。
· ⚠️ **反过来也成立：属于某一类不构成入选理由。** 同一类里聊得浅的那次，不比一次高浓度的争执够格。

# 硬约束（机器会当场拒收）

· **展示位最多 15 条**；**储备池 25~35 条**（按浓度排序，只给她挑，不进他那边）。
· 🔴 **去重**：**同一件事、同一个论点只留一张。**
  ⚠️ **卡号不同但说的是同一件事，机器【查不出来】—— 这一条只有你能把关。**
· 🔴 **跨切面**：给每条写一个 `维度`，**任何一个维度不许超过 3 条**。
  否则展示位会塌成单一话题，**而塌了不报错**。
· **正负都要有**：给每条标 `正` 或 `负`，不要只挑一侧。
· 🔴 **这一格不做那类内容的展示位** —— 那属于别的分区，这一格不承担那个职能。
· 🔴 **同一个话题，如果后来聊到了更深的地方，就【替换】成更深的那一次** ——
  **不是新增一条，也不是保留第一次。** 这一格记的是「**这个话题到达过的最深处**」，
  不是「它第一次出现在哪天」。
  ⇒ 这种情况填 `replaces`（被换下的卡号）＋ `depth_note`（**凭什么算更深**，一句话）。

# 已经在位上的

下面会给你「当前展示位」。**没有理由就别动它** —— 每换一次她都要重新认一遍。

# 输出

只输出 JSON，不要任何别的字：
{"候选": [
  {"memory_id": 123, "slot": "display", "dimension": "示例维度",
   "tone": "负", "reason": "一句话，凭什么入选",
   "replaces": 45, "depth_note": "凭什么算比 #45 更深"},
  {"memory_id": 456, "slot": "pool", "dimension": "哲学", "tone": "正", "reason": "……"}
]}
（`replaces`／`depth_note` 只在"换成更深那次"时给，别的时候不要这两个键。）"""

def _展示位料(conn):
    rows = list(conn.execute(
        "SELECT id, title, content, COALESCE(occurred_at, created_at) AS 当 FROM memories "
        "WHERE status='active' AND COALESCE(is_fragment,0)=0 ORDER BY id DESC LIMIT ?",
        (SHOWCASE_CANDIDATES,)))
    出 = []
    for r in rows:
        一句 = (r["title"] or "").strip() or (r["content"] or "").strip().replace("\n", " ")[:50]
        出.append(f"#{r['id']} 〔{(r['当'] or '')[:10]}〕{一句}")
    return len(rows), "\n".join(reversed(出))

def rotate_showcase(conn, cfg, report, 今天=None, 发一发=None, 未过闸到=None):
    from nacre import showcase
    from nacre import narrative
    今 = narrative._今天(今天)
    发一发 = 发一发 or call_json

    上次 = get_watermark(conn, SHOWCASE_ROTATE_AT, "") or ""
    if 上次:
        隔 = (今 - narrative._今天(上次)).days
        if 隔 < SHOWCASE_INTERVAL_DAYS:
            report.append(f"展示位：**没轮换，一个请求都没发** —— 距上次 {隔} 天，"
                          f"不足 {SHOWCASE_INTERVAL_DAYS} 天。")
            return None
    张数, 料 = _展示位料(conn)
    if 张数 == 0:
        report.append("展示位：**没轮换，一个请求都没发** —— 库里一张 `active` 卡都没有。")
        return None

    在位 = showcase.current(conn)
    当前 = "\n".join(
        f"#{r['memory_id']} 〔{r['slot']}〕{r['dimension'] or ''} "
        f"{'🔒她钉的' if r['pinned_by'] == showcase.BY_HER else ''} {r['reason'] or ''}"
        for r in 在位) or "（还没有）"
    提示词 = (SHOWCASE_RULES + "\n\n## 当前展示位\n" + 当前
           + "\n\n## 全部记忆卡（一行摘要）\n" + 料)
    data = 发一发(cfg, 提示词, report, usage_db=_db_file_of(conn), 档=档_便宜) or {}
    候选 = [c for c in (data.get("候选") or []) if isinstance(c, dict) and c.get("memory_id")]

    她钉的 = showcase.her_pins(conn)
    判 = showcase.check_selection(候选, 她钉的=她钉的)

    要发回 = [x for x in 判.不合格() if x.处置 != verdict.截尾]
    if 要发回 and not 判.没救了:
        小 = 段级重试提示词(
            "展示位", 要发回,
            输出说明='# 输出\n\n只输出 JSON，不要任何别的字。**给一份改好的完整候选清单**'
                 '（键跟上一发一样：`memory_id`／`slot`／`dimension`／`tone`／`reason`，'
                 '换更深那次才加 `replaces`／`depth_note`）：\n{"候选": [ … ]}')
        try:
            回 = 发一发(cfg, 小, report, usage_db=_db_file_of(conn), 档=档_便宜) or {}
        except Exception as e:
            回 = {}
            report.append(f"⚠️ 展示位段级重试那一发炸了（{type(e).__name__}: {e}）。")
        新候选 = [c for c in ((回 or {}).get("候选") or [])
               if isinstance(c, dict) and c.get("memory_id")]
        if 新候选:
            候选 = 新候选
            判 = showcase.check_selection(候选, 她钉的=她钉的)
            report.append(f"展示位：**段级重试了一次**（只把出问题的 {len(要发回)} 条发回去，"
                          "没重发那 300 张卡的摘要 —— 设计约定）。")

    留档 = 落一份未过闸("展示位", 判, 目录=未过闸到)

    if 判.没救了 or 判.要退回:
        store.add_review_event(conn, "alert", None,
                               f"{SHOWCASE_TAG}这一版要**退回**，一个字都没写：\n"
                               + "\n".join("· " + p for p in 判))
        set_watermark(conn, SHOWCASE_ROTATE_AT, 今.isoformat())
        连续 = 记这一步(conn, "展示位", False, "；".join(判[:2]))
        conn.commit()
        report.append(f"🔴 展示位：**这一版要退回 ⇒ 一个字都没写**"
                      f"（{len(判)} 处，已记 alert；连续第 {连续} 次）"
                      + (f"；原文留在 `{留档}`。" if 留档 else "。"))
        return None

    落 = showcase.落合格的(判)
    拿掉 = 判.不合格()
    上, 下, 换 = showcase.apply_selection(conn, 落, changed_by=showcase.BY_MODEL)
    set_watermark(conn, SHOWCASE_ROTATE_AT, 今.isoformat())
    if 拿掉:
        store.add_review_event(conn, "alert", None,
                               f"{SHOWCASE_TAG}这一版有 {len(拿掉)} 条没进去（其余照常落，"
                               "下面那两条）：\n" + "\n".join("· " + p for p in 判))
    连续 = 记这一步(conn, "展示位", not 拿掉, "；".join(判[:2]) if 拿掉 else "")
    conn.commit()
    报 = (f"展示位：轮换完成 —— 上 {len(上)} 条、下 {len(下)} 条、"
         f"换成更深的 {len(换)} 条（流水在 `showcase_rotation_log`，"
         "她要的「哪条下来了哪条上去了」就是它）。")
    report.append(报)
    if 拿掉:
        report.append(f"　🔴 有 {len(拿掉)} 条没进去（"
                      f"截尾 {len(判.按处置(verdict.截尾))} 条 · "
                      f"拒段 {len(判.按处置(verdict.拒段))} 条；连续第 {连续} 次没干净）"
                      + (f"，原文在 `{留档}`。" if 留档 else "。"))
    for 句 in showcase.remarks(落):
        report.append("　" + 句)
    return 上, 下, 换

NIGHTLY_SPEND_TAG = "夜班账单"

def 按模型拆(rows):
    桶 = {}
    for r in rows:
        名 = (r["model"] or "").strip() or "（没记上模型）"
        n, cost = 桶.get(名, (0, 0.0))
        桶[名] = (n + 1, cost + float(r["cost_usd"] or 0.0))
    if len(桶) <= 1 and "（没记上模型）" not in 桶:
        return ""
    项 = "；".join(f"{名} {n} 发 ≈${c:.2f}" for 名, (n, c) in sorted(桶.items()))
    return f"　按模型拆：{项}。\n"

def spend_report(conn, report, since_usage_id, elapsed_s):
    rows = list(conn.execute(
        "SELECT * FROM turn_usage WHERE id > ? AND source = ? ORDER BY id",
        (since_usage_id, NIGHTLY_USAGE_SOURCE)))
    n = len(rows)
    ok_n = sum(1 for r in rows if r["ok"])
    tin = sum(int(r["input_tokens"] or 0) for r in rows)
    tout = sum(int(r["output_tokens"] or 0) for r in rows)
    tread = sum(int(r["cache_read"] or 0) for r in rows)
    twrite = sum(int(r["cache_write"] or 0) for r in rows)
    total = tin + tout + tread + twrite
    cost = sum(float(r["cost_usd"] or 0.0) for r in rows)

    分 = int(elapsed_s // 60)
    秒 = int(elapsed_s % 60)
    用时 = f"{分} 分 {秒} 秒" if 分 else f"{秒} 秒"

    if not n:
        头 = f"夜班账单：这一趟**一发请求都没发**（跑了 {用时}）。"
        明细 = "（`--no-llm` 或今晚没有待蒸的东西 —— **这不是出错**。）"
    else:
        头 = (f"夜班账单：发了 {n} 发请求"
              + (f"（其中 {n - ok_n} 发失败）" if ok_n != n else "")
              + f" · 共 {total:,} token · **折算约 ${cost:.2f}** · 跑了 {用时}。")
        明细 = (f"　token 拆开：输入 {tin:,} ＋ 输出 {tout:,} ＋ 缓存读 {tread:,} ＋ 缓存写 {twrite:,}。\n"
                + 按模型拆(rows) +
                "　⚠️ **「折算约」三个字是口径不是谦虚**：走订阅额度时这个数是 CLI 按 API 价目表"
                "**折算**出来的，**不是真出账**—— 别拿它去对账单。")
    report.append(头)
    report.append(明细)

    try:
        store.add_review_event(
            conn, "edit", None,
            f"{NIGHTLY_SPEND_TAG}｜{头}\n{明细}\n"
            "📌 逐发明细在 `turn_usage` 里 `source='nightly'` 那些行"
            "（`source='chat'` 是你跟他说话那条路，两条**刻意分开记**）。")
        conn.commit()
    except Exception as e:
        print(f"⚠️ 夜班账单那条没落进质检台（报告里那两行还在）：{type(e).__name__}: {e}")

def main():
    no_llm = "--no-llm" in sys.argv
    redistill = "--redistill" in sys.argv
    limit_chunks = None
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            limit_chunks = int(sys.argv[i + 1])
    cfg = load_config()
    report = []
    started = datetime.now()
    dst = backup()
    report.append(f"备份：{dst.name if dst else '库文件尚不存在，跳过'}。")

    conn = get_conn()
    try:
        since_usage_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) m FROM turn_usage").fetchone()["m"]
        n_conv, n_msg = import_cc_sessions.import_sessions(conn, cfg)
        conn.commit()
        report.append(f"CC 导入：{n_msg} 条新消息（{'已启用' if cfg['cc_import']['enabled'] else '未启用'}）。")

        if no_llm:
            last_id, rows = _pending_messages(conn, redistill=redistill)
            report.append(f"抽取：--no-llm 跳过。水位线 #{last_id} 之后还有 {len(rows)} 条消息待会话内蒸馏。")
        else:
            try:
                extract(conn, cfg, report, redistill=redistill, limit_chunks=limit_chunks,
                        已确认量=-1)
            except Exception as e:
                conn.rollback()
                store.add_review_event(conn, "alert", None, f"夜班抽取失败（水位线未动，明晚重试）：{e}")
                conn.commit()
                report.append(f"抽取：失败——{e}")

        mark, covered, lag = watermark_lag(conn)
        if lag:
            store.add_review_event(
                conn,
                "alert",
                None,
                f"水位线滞后：卡片已覆盖到消息 #{covered}，水位线仍停在 #{mark}（差 {lag} 条）。"
                f"这批消息会被重复蒸馏，请把 extract:last_msg_id 推到 #{covered}。",
            )
            conn.commit()
            report.append(f"⚠️ 水位线自检：滞后 {lag} 条——卡已写到 #{covered}，水位线停在 #{mark}。已记 alert 待处理。")
        else:
            report.append(f"水位线自检：正常（#{mark}）。")

        check_coverage(conn, cfg, report, mark)

        done, err = embeddings.backfill(conn, cfg)
        if err:
            store.add_review_event(conn, "alert", None, f"⚠️ 向量指纹补漏失败（检索将降级为纯关键词）：{err}")
            report.append(f"指纹补漏：{done} 条后失败——{err}")
        else:
            report.append(f"指纹补漏：{done} 条。")
        conn.commit()

        report.append("情绪回填：" + 下架说明)

        if no_llm:
            n_cand = len(pick_one_liner_candidates(conn, cfg))
            report.append(f"实体一句话：--no-llm 跳过。候选 {n_cand} 个待写。")
        else:
            try:
                propose_one_liners(conn, cfg, report)
            except Exception as e:
                report.append(f"实体一句话：失败——{e}")

        if no_llm:
            n_new = len(pick_resident_note_candidates(conn))
            report.append(f"常驻层格②：--no-llm 跳过。上次维护之后有 {n_new} 张挂得上原话的新卡。")
        else:
            try:
                propose_resident_notes(conn, cfg, report)
            except Exception as e:
                report.append(f"常驻层格②：失败——{e}")

        if no_llm:
            _, _, n_卡, n_消息 = 叙事层新增量(conn)
            report.append(f"叙事层：--no-llm 跳过。距上次重写有 {n_卡} 张新卡、{n_消息} 条新消息。")
        else:
            try:
                rewrite_narrative(conn, cfg, report)
            except Exception as e:
                report.append(f"叙事层：失败——{e}")

        if no_llm:
            from nacre import showcase as _sc
            report.append(f"展示位：--no-llm 跳过。现在展示位 "
                          f"{len(_sc.current(conn, _sc.DISPLAY))} 条、"
                          f"储备池 {len(_sc.current(conn, _sc.POOL))} 条。")
        else:
            try:
                rotate_showcase(conn, cfg, report)
            except Exception as e:
                report.append(f"展示位：失败——{e}")

        conn.commit()

        report.append("近况摘要：" + 下架说明)
        report.append("核心卡：" + 下架说明
                      + "库里那一版原样留着，质检台显示的是最后一版并已注明不再更新。")
        spend_report(conn, report, since_usage_id,
                     (datetime.now() - started).total_seconds())
    finally:
        conn.close()

    print("=== 夜班报告 " + now_iso() + " ===")
    for line in report:
        print("· " + line)

if __name__ == "__main__":
    main()
