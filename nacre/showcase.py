from . import verdict
from .db import now_iso

DISPLAY = "display"
POOL = "pool"
BY_MODEL = "model"
BY_HER = "her"

DISPLAY_LIMIT = 15

POOL_MIN = 25
POOL_MAX = 35

PER_DIMENSION_MAX = 3

色调最少查几条 = 4

_合法色调 = ("正", "负")

def current(conn, slot=None):
    sql = ("SELECT id, memory_id, slot, dimension, tone, reason, depth_note, "
           "pinned_by, created_at FROM showcase_cards")
    args = ()
    if slot:
        sql += " WHERE slot=?"
        args = (slot,)
    return list(conn.execute(sql + " ORDER BY id", args))

def for_model(conn):
    return list(conn.execute(
        "SELECT s.memory_id, m.title, m.content, "
        "       COALESCE(m.occurred_at, m.created_at) AS 当 "
        "  FROM showcase_cards s JOIN memories m ON m.id = s.memory_id "
        " WHERE s.slot=? AND m.status='active' ORDER BY 当 DESC, s.memory_id",
        (DISPLAY,)))

def _键(c, k, 默认=None):
    return (c.get(k) if isinstance(c, dict) else c[k]) or 默认

def _一行(c):
    return (f"#{int(_键(c, 'memory_id', 0))} 〔{_键(c, 'slot', DISPLAY)}〕"
            f"{_键(c, 'dimension', '') or '（没写维度）'} "
            f"{_键(c, 'tone', '') or '（没写色调）'}　{_键(c, 'reason', '') or ''}"
            + (f"　换掉 #{int(_键(c, 'replaces'))}：{_键(c, 'depth_note', '') or '（没写）'}"
               if _键(c, "replaces") else ""))

def check_selection(候选, 她钉的=()):
    整份问题 = []
    展示 = [c for c in 候选 if _键(c, "slot", DISPLAY) == DISPLAY]
    池 = [c for c in 候选 if _键(c, "slot", DISPLAY) == POOL]

    见过 = {}
    单元 = []
    索引 = {}
    for c in 候选:
        mid = int(_键(c, "memory_id", 0))
        见过[mid] = 见过.get(mid, 0) + 1
        名 = f"#{mid}" if 见过[mid] == 1 else f"#{mid}（第{见过[mid]}次出现）"
        s = verdict.段判决(名, 过=True, 原文=_一行(c), 载荷=c, 引用卡号=(mid,))
        单元.append(s)
        索引[id(c)] = s

    def _拒(c, 处置, 话):
        s = 索引[id(c)]
        s.过 = False
        s.处置 = 处置
        s.问题 = s.问题 + (话,)

    def _整格(名, 处置, 话, 料):
        s = verdict.段判决(名, 过=False, 问题=(话,), 处置=处置, 原文=料)
        单元.append(s)

    if len(展示) > DISPLAY_LIMIT:
        _整格("展示位·数量", verdict.退回,
             f"🔴 ① 展示位 {len(展示)} 条，上限 {DISPLAY_LIMIT} 条。\n"
             "   🔴 **这 15 条并列无序 ⇒ 程序不许砍**，退回让它自己合并取舍"
             "。",
             "\n".join(_一行(c) for c in 展示))
    if len(池) > POOL_MAX:
        for n, c in enumerate(池[POOL_MAX:], POOL_MAX + 1):
            _拒(c, verdict.截尾,
                f"🔴 ① 储备池 {len(池)} 条，上限 {POOL_MAX} 条——"
                f"这是第 {n} 条，**按浓度排序的尾巴，程序截掉**"
                "。")

    重 = sorted(m for m, n in 见过.items() if n > 1)
    if 重:
        整份问题.append(f"🔴 ② 这几张卡在候选里出现了不止一次：{['#%d' % m for m in 重]}"
                    "")
        见 = set()
        for c in 候选:
            mid = int(_键(c, "memory_id", 0))
            if mid in 见:
                _拒(c, verdict.拒段,
                    f"🔴 ② #{mid} 在候选里出现了不止一次 —— **重复的这一条不落**，"
                    "第一次出现的那条照落")
            见.add(mid)

    桶 = {}
    for c in 展示:
        d = (_键(c, "dimension", "") or "（没写维度）").strip()
        桶.setdefault(d, []).append(c)
    for d, cs in sorted(桶.items()):
        if len(cs) > PER_DIMENSION_MAX:
            _整格(f"展示位·维度「{d}」", verdict.退回,
                 f"🔴 ③ 维度「{d}」占了 {len(cs)} 条，上限 {PER_DIMENSION_MAX} 条 ——"
                 f"**展示位会塌成单一话题，而塌了不报错**："
                 f"{['#%d' % int(_键(c, 'memory_id', 0)) for c in cs]}\n"
                 "   🔴 **同一维度里这几条并列无序 ⇒ 程序不许砍**，退回让它自己挑。",
                 "\n".join(_一行(c) for c in cs))

    色 = [(_键(c, "tone", "") or "").strip() for c in 展示]
    for c in 展示:
        t = (_键(c, "tone", "") or "").strip()
        if t and t not in _合法色调:
            _拒(c, verdict.拒段,
                f"🔴 ④ 不认识的色调 {t!r} —— 只有 {list(_合法色调)}")
    if len(展示) >= 色调最少查几条:
        缺 = [t for t in _合法色调 if t not in 色]
        if 缺:
            _整格("展示位·色调", verdict.退回,
                 f"🔴 ④ 展示位 {len(展示)} 条里一条「{缺[0]}」都没有 ——"
                 "**不许全是甜的，也不许全是痛的**。\n"
                 "   ⚠️ 这是**整格的属性**，删掉哪一条都补不出来 ⇒ 退回让它换几条。",
                 "\n".join(_一行(c) for c in 展示))

    留下的 = {int(_键(c, "memory_id", 0)) for c in 候选}
    没了的 = [m for m in 她钉的 if int(m) not in 留下的]
    if 没了的:
        _整格("她钉的", verdict.退回,
             f"🔴 ⑤ 这几条是**她手动钉的**，自动轮换不许把它们换下去："
             f"{['#%d' % int(m) for m in 没了的]}"
             "（三条硬约束之三 —— 否则「我可以手动调整」是假的）",
             "\n".join(f"#{int(m)}" for m in 没了的))

    for c in 候选:
        旧 = _键(c, "replaces")
        if 旧 and not (_键(c, "depth_note", "") or "").strip():
            _拒(c, verdict.拒段,
                f"🔴 ⑥ #{int(_键(c, 'memory_id', 0))} 说它要换掉 #{int(旧)}，"
                "却没写凭什么算更深 —— **替换必须留痕，否则「更深」是一个无法复核的说法**"
                "")
    return verdict.判决书(单元, 整份问题)

def 落合格的(判):
    return [s.载荷 for s in 判.段 if s.过 and s.载荷 is not None]

def remarks(候选):
    出 = []
    展示 = [c for c in 候选 if _键(c, "slot", DISPLAY) == DISPLAY]
    池 = [c for c in 候选 if _键(c, "slot", DISPLAY) == POOL]
    if len(展示) < DISPLAY_LIMIT:
        出.append(f"⚪ 展示位只有 {len(展示)} 条——"
                  "**不拒收**：逼它凑满唯一能被满足的方式就是编。")
    if len(池) < POOL_MIN:
        出.append(f"⚪ 储备池只有 {len(池)} 条。")
    return 出

def her_pins(conn):
    return [r["memory_id"] for r in conn.execute(
        "SELECT memory_id FROM showcase_cards WHERE pinned_by=? ORDER BY id", (BY_HER,))]

def _记流水(conn, action, memory_id, changed_by, slot=None, dimension=None,
          reason=None, replaced=None):
    conn.execute(
        "INSERT INTO showcase_rotation_log(action, memory_id, replaced_memory_id, slot, "
        "dimension, reason, changed_by, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (action, memory_id, replaced, slot, dimension, reason, changed_by, now_iso()))

def apply_selection(conn, 候选, changed_by=BY_MODEL):
    她的 = set(her_pins(conn))
    问题 = check_selection(候选, 她钉的=她的 if changed_by == BY_MODEL else ())
    if 问题:
        raise ValueError("展示位这一版没过闸，一个字都没写：\n" + "\n".join("· " + p for p in 问题))

    旧 = {r["memory_id"]: r for r in current(conn)}
    新 = {int(_键(c, "memory_id", 0)): c for c in 候选}
    上, 下, 换 = [], [], []

    被换掉的 = {int(_键(c, "replaces")): int(_键(c, "memory_id", 0))
             for c in 候选 if _键(c, "replaces")}
    for mid, r in 旧.items():
        if mid in 新:
            continue
        if changed_by == BY_MODEL and r["pinned_by"] == BY_HER:
            raise ValueError(f"#{mid} 是她钉的，自动轮换不许把它换下去")
        conn.execute("DELETE FROM showcase_cards WHERE memory_id=?", (mid,))
        if mid in 被换掉的:
            继任 = 被换掉的[mid]
            _记流水(conn, "replace", 继任, changed_by, slot=DISPLAY,
                  dimension=_键(新.get(继任, {}), "dimension"),
                  reason=(_键(新.get(继任, {}), "depth_note") or ""), replaced=mid)
            换.append(mid)
        else:
            _记流水(conn, "out", mid, changed_by, slot=r["slot"],
                  dimension=r["dimension"], reason=r["reason"])
            下.append(mid)

    for mid, c in 新.items():
        slot = _键(c, "slot", DISPLAY)
        dim = _键(c, "dimension")
        tone = _键(c, "tone")
        reason = _键(c, "reason")
        深 = _键(c, "depth_note")
        if mid in 旧:
            r = 旧[mid]
            by = r["pinned_by"] if changed_by == BY_MODEL else changed_by
            if r["slot"] != slot:
                _记流水(conn, "in" if slot == DISPLAY else "out", mid, changed_by,
                      slot=slot, dimension=dim, reason=reason)
                (上 if slot == DISPLAY else 下).append(mid)
            conn.execute(
                "UPDATE showcase_cards SET slot=?, dimension=?, tone=?, reason=?, "
                "depth_note=?, pinned_by=? WHERE memory_id=?",
                (slot, dim, tone, reason, 深, by, mid))
            continue
        conn.execute(
            "INSERT INTO showcase_cards(memory_id, slot, dimension, tone, reason, "
            "depth_note, pinned_by, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (mid, slot, dim, tone, reason, 深, changed_by, now_iso()))
        if _键(c, "replaces"):
            pass
        else:
            _记流水(conn, "in", mid, changed_by, slot=slot, dimension=dim, reason=reason)
            上.append(mid)
    return 上, 下, 换

def pin_by_her(conn, memory_id, dimension=None, tone=None, reason=None, slot=DISPLAY):
    mid = int(memory_id)
    if not conn.execute("SELECT 1 FROM memories WHERE id=? AND status='active'",
                        (mid,)).fetchone():
        raise ValueError(f"要钉的这张卡不在（或已撤回）：#{mid}")
    有 = conn.execute("SELECT 1 FROM showcase_cards WHERE memory_id=?", (mid,)).fetchone()
    if 有:
        conn.execute("UPDATE showcase_cards SET slot=?, pinned_by=?, dimension=COALESCE(?,dimension), "
                     "tone=COALESCE(?,tone), reason=COALESCE(?,reason) WHERE memory_id=?",
                     (slot, BY_HER, dimension, tone, reason, mid))
    else:
        conn.execute(
            "INSERT INTO showcase_cards(memory_id, slot, dimension, tone, reason, "
            "pinned_by, created_at) VALUES(?,?,?,?,?,?,?)",
            (mid, slot, dimension, tone, reason, BY_HER, now_iso()))
    _记流水(conn, "in", mid, BY_HER, slot=slot, dimension=dimension,
          reason=reason or "她手动钉的")
    return mid

def unpin_by_her(conn, memory_id, 放开=False):
    mid = int(memory_id)
    r = conn.execute("SELECT slot, dimension, reason FROM showcase_cards WHERE memory_id=?",
                     (mid,)).fetchone()
    if not r:
        return False
    if 放开:
        conn.execute("UPDATE showcase_cards SET pinned_by=? WHERE memory_id=?", (BY_MODEL, mid))
        _记流水(conn, "in", mid, BY_HER, slot=r["slot"], dimension=r["dimension"],
              reason="她放开了这一条，交回自动轮换")
        return True
    conn.execute("DELETE FROM showcase_cards WHERE memory_id=?", (mid,))
    _记流水(conn, "out", mid, BY_HER, slot=r["slot"], dimension=r["dimension"],
          reason="她手动摘下来的")
    return True

def rotation_log(conn, limit=50):
    return list(conn.execute(
        "SELECT action, memory_id, replaced_memory_id, slot, dimension, reason, "
        "changed_by, created_at FROM showcase_rotation_log ORDER BY id DESC LIMIT ?",
        (int(limit),)))
