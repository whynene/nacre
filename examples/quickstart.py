#!/usr/bin/env python3
"""最小可跑示例：走一遍 L0 → L1 → 检索 → 钻回原文。

    python examples/quickstart.py

它用一个临时数据库，不碰任何真实数据，跑完自己删掉。
里面的对话内容是编的。

这个例子想说明的是四件事：

  ① 一条对话进账本（L0），逐字保存
  ② 一张记忆卡从它长出来（L1），必须钉着一句真实原话
  ③ 检索能把卡找回来
  ④ 从卡能钻回当时的原文 —— 这是分层不至于变成有损压缩的原因

⚠️ 真实使用中，第 ② 步（写卡）是夜间由模型批量做的，不是手写。
   这里手写是为了让例子跑得起来、不需要模型、不花钱。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# 让这个例子在【没有 pip install】的情况下也跑得起来：
# 把仓库根目录加进搜索路径。装过包的话这一行不起作用，也不碍事。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

临时目录 = Path(tempfile.mkdtemp(prefix="nacre-example-"))
库文件 = 临时目录 / "example.db"
os.environ["NACRE_DB"] = str(库文件)

# 这个例子自带一份最小配置，免得你为了跑它去改 config.json
配置 = {
    "db_path": str(库文件),
    "is_primary": True,
    "local_utc_offset_hours": 8,
    "embedding": {"api_key": ""},        # 空 ⇒ 只走关键词检索，不发任何网络请求
    "recall": {"alpha": 0.5, "default_limit": 5, "max_limit": 20},
}
(临时目录 / "config.json").write_text(json.dumps(配置, ensure_ascii=False),
                                      encoding="utf-8")
from nacre import db, search, store   # noqa: E402  （要先设好环境变量再导入）

os.chdir(临时目录)


def 分节(标题):
    print(f"\n{'─' * 58}\n{标题}\n{'─' * 58}")


conn = db.get_conn(str(库文件))

# ─────────────────────────────────────────────────────────────
分节("L0 · 对话进账本：逐字保存，之后不可改写")

对话 = store.ensure_conversation(conn, "manual")
第一句 = store.append_message(
    conn, 对话, "user",
    "我给我那只猫起名叫年糕，因为它趴着的样子像。", "2026-01-01T21:30:00Z")
第二句 = store.append_message(
    conn, 对话, "assistant",
    "年糕。那它睡醒之后会不会变形。", "2026-01-01T21:31:00Z")
conn.commit()

for r in conn.execute("SELECT id, role, content FROM messages ORDER BY id"):
    print(f"  #{r['id']}  {r['role']:9s} {r['content']}")

print("\n  试着改一句看看：")
try:
    conn.execute("UPDATE messages SET content='改过的' WHERE id=?", (第一句,))
    conn.commit()
    print("  🔴 竟然改成功了 —— 不可变没生效")
except Exception as e:
    print(f"  ✅ 被数据库拒了：{type(e).__name__}")
    conn.rollback()

# ─────────────────────────────────────────────────────────────
分节("L1 · 从对话长出一张卡：必须钉着一句真实原话")

正文 = "她给猫起名叫年糕，理由是它趴着的样子像年糕。"
卡号 = store.add_memory(
    conn, 配置, 正文,
    # 🔴 这句必须【一字不差】地出现在上面那个区间里，否则整张卡被拒
    src_quote="我给我那只猫起名叫年糕，因为它趴着的样子像。",
    src_conversation_id=对话,
    src_msg_start=第一句, src_msg_end=第二句,
    # 🔴 正文每一句来自哪条消息，也要说清楚
    src_sentence_map=[{"sent": 正文, "msg_ids": [第一句]}],
)
conn.commit()
print(f"  ✅ 写成了，卡号 #{卡号}")

print("\n  试着写一张【编原话】的卡：")
try:
    store.add_memory(
        conn, 配置, "她说她最喜欢的动物是猫。",
        src_quote="我最喜欢猫了。",          # ← 这句账本里根本没有
        src_conversation_id=对话,
        src_msg_start=第一句, src_msg_end=第二句,
        src_sentence_map=[{"sent": "她说她最喜欢的动物是猫。", "msg_ids": [第一句]}])
    print("  🔴 竟然写进去了 —— 原文校验没生效")
except Exception as e:
    print(f"  ✅ 被拒了：{str(e).splitlines()[0][:70]}")
    conn.rollback()

# ─────────────────────────────────────────────────────────────
分节("检索 · 把卡找回来")

结果, 提示 = search.recall(conn, 配置, "年糕", limit=3)
for t in 提示:
    print(f"  ⚠️ {t}")
for r in 结果:
    print(f"  [#{r['row']['id']}] {r['row']['content']}")

print("\n  换成单个字试试：")
少, _ = search.recall(conn, 配置, "猫", limit=3)
print(f"  「猫」→ {len(少)} 条　（中文分词后单字不成词，这是正常的）")

# ─────────────────────────────────────────────────────────────
分节("钻回原文 · 分层不至于变成有损压缩的原因")

卡 = conn.execute("SELECT content, src_quote, src_msg_start, src_msg_end "
                  "FROM memories WHERE id=?", (卡号,)).fetchone()
print(f"  卡面（压缩过的）：{卡['content']}")
print(f"  它钉着的那句原话：{卡['src_quote']}")
print(f"  它出自账本的哪一段：#{卡['src_msg_start']} ~ #{卡['src_msg_end']}")
print("\n  照着这个区间回账本取原文：")
for r in conn.execute("SELECT id, role, content FROM messages "
                      "WHERE id BETWEEN ? AND ? ORDER BY id",
                      (卡['src_msg_start'], 卡['src_msg_end'])):
    print(f"    #{r['id']}  {r['role']:9s} {r['content']}")

print("\n  ⭐ 任何一句概括，都能这样两步回到证据。")

conn.close()
import shutil                                   # noqa: E402
shutil.rmtree(临时目录, ignore_errors=True)
print(f"\n跑完了，临时数据库已删除。\n")
