import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone

from .config import db_path, load_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_end TEXT NOT NULL CHECK (source_end IN ('claude_desktop','claude_ai','claude_code','frontend','manual')),
  external_id TEXT UNIQUE,
  title TEXT,
  started_at TEXT,
  last_message_at TEXT,
  window_name TEXT,
  model TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  role TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  external_id TEXT UNIQUE,
  meta TEXT,
  thinking TEXT,
  thinking_signature TEXT,
  model TEXT,
  effort TEXT,
  source TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);

CREATE TRIGGER IF NOT EXISTS messages_immutable_update
BEFORE UPDATE ON messages
BEGIN SELECT RAISE(ABORT, '账本不可变：messages 只允许 INSERT'); END;

CREATE TRIGGER IF NOT EXISTS messages_immutable_delete
BEFORE DELETE ON messages
BEGIN SELECT RAISE(ABORT, '账本不可变：messages 只允许 INSERT'); END;

CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK (kind IN ('event','fact','quote','commitment','insight','note','taboo')),
  content TEXT NOT NULL,
  importance INTEGER NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
  valence REAL,
  arousal REAL,
  occurred_at TEXT,
  created_at TEXT NOT NULL,
  src_conversation_id INTEGER REFERENCES conversations(id),
  src_msg_start INTEGER,
  src_msg_end INTEGER,
  src_quote TEXT,
  author TEXT NOT NULL CHECK (author IN ('nightly','assistant','user')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retracted','superseded','memento','disputed')),
  supersedes INTEGER REFERENCES memories(id),
  is_core INTEGER NOT NULL DEFAULT 0,
  last_used_at TEXT,
  use_count INTEGER NOT NULL DEFAULT 0,
  zone INTEGER NOT NULL DEFAULT 1 CHECK (zone IN (1,2)),
  author_window TEXT,
  write_context TEXT,
  importance_review INTEGER CHECK (importance_review IS NULL OR importance_review BETWEEN 1 AND 5),
  reviewed_at TEXT,
  sink TEXT NOT NULL DEFAULT 'active' CHECK (sink IN ('active','resolved','archived')),
  src_sentence_map TEXT,
  note_container TEXT,
  note_position TEXT,
  target_memory_id INTEGER REFERENCES memories(id),
  stance TEXT CHECK (stance IS NULL OR stance IN
    ('accept','reject','suspend','annotate','unconvinced','changed')),
  bridge_memory_id INTEGER REFERENCES memories(id),
  commitment_status TEXT CHECK (commitment_status IS NULL OR commitment_status IN
    ('open','fulfilled','void','binding')),
  --
  protect TEXT CHECK (protect IS NULL OR protect IN ('summarizable','no_summary','verbatim')),
  --
  tripwire TEXT,
  --
  infer_seen INTEGER CHECK (infer_seen IS NULL OR infer_seen >= 1),
  infer_falsifier TEXT,
  infer_recheck TEXT,
  --
  is_deep INTEGER NOT NULL DEFAULT 0,
  trigger_text TEXT,
  trigger_type TEXT CHECK (trigger_type IS NULL OR trigger_type IN ('external','she_said','self_prior')),
  is_foundation INTEGER NOT NULL DEFAULT 0,
  foundation_category TEXT CHECK (foundation_category IS NULL OR foundation_category IN (
    'opening','he_said_no','conflict','she_owned_a_mistake','he_changed_his_mind',
    'ordinary_day','said_it_though_unsure','delayed_learning','open')),
  about_her INTEGER NOT NULL DEFAULT 0,
  is_fragment INTEGER NOT NULL DEFAULT 0,
  fact_changed INTEGER NOT NULL DEFAULT 0,
  wording_changed INTEGER NOT NULL DEFAULT 0,
  title TEXT
);

CREATE TRIGGER IF NOT EXISTS memories_no_delete
BEFORE DELETE ON memories
BEGIN SELECT RAISE(ABORT, '遗忘是淡去不是删除：memories 禁止物理删除'); END;

CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  aliases TEXT DEFAULT '',
  type TEXT,
  one_liner TEXT DEFAULT '',
  profile_md TEXT DEFAULT '',
  last_mentioned_at TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS memory_entities (
  memory_id INTEGER NOT NULL REFERENCES memories(id),
  entity_id INTEGER NOT NULL REFERENCES entities(id),
  PRIMARY KEY (memory_id, entity_id)
);

CREATE TABLE IF NOT EXISTS embeddings (
  memory_id INTEGER PRIMARY KEY REFERENCES memories(id),
  vector BLOB NOT NULL,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  created_at TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, memory_id UNINDEXED);

CREATE TABLE IF NOT EXISTS core_card_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content_md TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watermarks (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS review_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL CHECK (type IN ('new_memory','alert','retract','edit')),
  memory_id INTEGER REFERENCES memories(id),
  detail TEXT,
  created_at TEXT NOT NULL,
  seen INTEGER NOT NULL DEFAULT 0
);


CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  note TEXT DEFAULT '',
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS memory_threads (
  memory_id INTEGER NOT NULL REFERENCES memories(id),
  thread_id INTEGER NOT NULL REFERENCES threads(id),
  PRIMARY KEY (memory_id, thread_id)
);

CREATE TABLE IF NOT EXISTS field_visibility (
  table_name TEXT NOT NULL,
  column_name TEXT NOT NULL,
  model_visible INTEGER NOT NULL DEFAULT 0,
  note TEXT DEFAULT '',
  PRIMARY KEY (table_name, column_name)
);

--
CREATE TABLE IF NOT EXISTS handover_marks (
  message_id INTEGER NOT NULL REFERENCES messages(id),
  window_name TEXT NOT NULL,
  marked_by TEXT NOT NULL CHECK (marked_by IN ('user','system')),
  created_at TEXT NOT NULL,
  PRIMARY KEY (message_id, window_name)
);

CREATE TABLE IF NOT EXISTS sourcing_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  source TEXT,
  action TEXT NOT NULL CHECK (action IN ('delete','tag')),
  rule_hit TEXT,
  removed_text TEXT,
  restored_at TEXT
);

--
--
CREATE TABLE IF NOT EXISTS reading_wishlist (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  what TEXT NOT NULL,
  why TEXT NOT NULL,
  urgency TEXT NOT NULL CHECK (urgency IN ('now','queued')),
  depth TEXT NOT NULL CHECK (depth IN ('link','light','research')),
  status TEXT NOT NULL CHECK (status IN ('open','running','done','failed')),
  parent_id INTEGER REFERENCES reading_wishlist(id),
  started_at TEXT,
  finished_at TEXT,
  result_memory_id INTEGER REFERENCES memories(id),
  note TEXT
);

--
CREATE TABLE IF NOT EXISTS note_sources (
  memory_id INTEGER PRIMARY KEY REFERENCES memories(id),
  raw_source TEXT NOT NULL,
  source_key TEXT,
  verdict TEXT NOT NULL CHECK (verdict IN ('breaks_circle','echo_chamber','unregistered')),
  created_at TEXT NOT NULL
);

--
CREATE TABLE IF NOT EXISTS tool_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tool TEXT NOT NULL,
  what TEXT NOT NULL DEFAULT '',
  ok INTEGER NOT NULL DEFAULT 1,
  occurred_at TEXT NOT NULL,
  message_id INTEGER,
  call_id TEXT,
  session_path TEXT,
  result_state TEXT NOT NULL DEFAULT 'ok'
);

--
--
--
CREATE TABLE IF NOT EXISTS tool_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tool TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('install','enable','disable','remove')),
  by_who TEXT NOT NULL CHECK (by_who IN ('user','system')),
  note TEXT NOT NULL DEFAULT '',
  occurred_at TEXT NOT NULL
);

--
--
--
--
--
--
--
--
CREATE TABLE IF NOT EXISTS turn_handles (
  turn_id INTEGER NOT NULL,
  slot TEXT NOT NULL,
  memory_id INTEGER NOT NULL REFERENCES memories(id),
  created_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'chat' CHECK (source IN ('chat','mcp')),
  PRIMARY KEY (turn_id, slot)
);

--
--
--
--
--
--
CREATE TABLE IF NOT EXISTS turn_meta (
  message_id INTEGER PRIMARY KEY REFERENCES messages(id),
  turn_id INTEGER,
  recall_status TEXT,
  recall_total INTEGER,
  recall_error TEXT,
  created_at TEXT NOT NULL
);

--
--
--
--
--
--
--
CREATE TABLE IF NOT EXISTS resident_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slot TEXT NOT NULL,
  text TEXT NOT NULL,
  ord INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);

--
--
CREATE TABLE IF NOT EXISTS pinned_cards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id INTEGER NOT NULL REFERENCES memories(id),
  created_at TEXT NOT NULL,
  used_at TEXT
);

--
--
--    `UPDATE pinned_cards SET used_at=? WHERE used_at IS NULL`，
--
--
--
--
--
--
--
CREATE TABLE IF NOT EXISTS showcase_cards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id INTEGER NOT NULL REFERENCES memories(id),
  slot TEXT NOT NULL CHECK (slot IN ('display','pool')),
  dimension TEXT,
  tone TEXT CHECK (tone IN ('正','负')),
  reason TEXT,
  depth_note TEXT,
  pinned_by TEXT NOT NULL DEFAULT 'model' CHECK (pinned_by IN ('model','her')),
  created_at TEXT NOT NULL,
  UNIQUE(memory_id)
);

--
CREATE TABLE IF NOT EXISTS showcase_rotation_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL CHECK (action IN ('in','out','replace')),
  memory_id INTEGER,
  replaced_memory_id INTEGER,
  slot TEXT,
  dimension TEXT,
  reason TEXT,
  changed_by TEXT NOT NULL CHECK (changed_by IN ('model','her')),
  created_at TEXT NOT NULL
);

--
--
--
CREATE TABLE IF NOT EXISTS turn_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id INTEGER REFERENCES messages(id),
  occurred_at TEXT NOT NULL,
  model TEXT,
  ok INTEGER NOT NULL DEFAULT 1,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0,
  cache_write INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL,
  duration_ms INTEGER,
  source TEXT NOT NULL DEFAULT 'chat',
  --
  --
  effort TEXT,
  roundtrips INTEGER
);
"""

def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def on_machine_axis(t):
    return t.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")

def on_utc_axis(t):
    return t.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")

def to_local(utc_text, offset_hours):
    t = (utc_text or "").strip()
    if not t:
        return ""
    try:
        dt = datetime.fromisoformat(t.replace("Z", "").split("+")[0])
    except ValueError:
        print(f"⚠️ 时间戳解析不了，按原样用（这一条仍是 UTC 口径）：{t!r}")
        return t[:16]
    return (dt + timedelta(hours=offset_hours)).strftime("%Y-%m-%d %H:%M")

def her_day_bounds(offset_hours, day=None):
    tz = timezone(timedelta(hours=int(offset_hours)))
    day = day or datetime.now(tz).date()
    起 = datetime.combine(day, time.min, tzinfo=tz)
    return 起, 起 + timedelta(days=1)

class NotPrimaryLedger(RuntimeError):
    pass

class WriteNotPersisted(RuntimeError):
    pass

_ADDED_COLUMNS = {
    "conversations": [
        ("window_name", "TEXT"),
        ("model", "TEXT"),
    ],
    "messages": [
        ("thinking", "TEXT"),
        ("thinking_signature", "TEXT"),
        ("model", "TEXT"),
        ("effort", "TEXT"),
        ("source", "TEXT"),
    ],
    "memories": [
        ("protect", "TEXT CHECK (protect IS NULL OR protect IN "
                    "('summarizable','no_summary','verbatim'))"),
        ("tripwire", "TEXT"),
        ("infer_seen", "INTEGER CHECK (infer_seen IS NULL OR infer_seen >= 1)"),
        ("infer_falsifier", "TEXT"),
        ("infer_recheck", "TEXT"),
        ("is_deep", "INTEGER NOT NULL DEFAULT 0"),
        ("zone", "INTEGER NOT NULL DEFAULT 1 CHECK (zone IN (1,2))"),
        ("author_window", "TEXT"),
        ("write_context", "TEXT"),
        ("importance_review", "INTEGER CHECK (importance_review IS NULL OR importance_review BETWEEN 1 AND 5)"),
        ("reviewed_at", "TEXT"),
        ("sink", "TEXT NOT NULL DEFAULT 'active' CHECK (sink IN ('active','resolved','archived'))"),

        ("src_sentence_map", "TEXT"),
        ("note_container", "TEXT"),
        ("note_position", "TEXT"),
        ("target_memory_id", "INTEGER REFERENCES memories(id)"),
        ("stance", "TEXT CHECK (stance IS NULL OR stance IN "
                   "('accept','reject','suspend','annotate','unconvinced','changed'))"),
        ("bridge_memory_id", "INTEGER REFERENCES memories(id)"),
        ("commitment_status", "TEXT CHECK (commitment_status IS NULL OR commitment_status IN "
                              "('open','fulfilled','void','binding'))"),
        ("trigger_text", "TEXT"),
        ("trigger_type", "TEXT CHECK (trigger_type IS NULL OR trigger_type IN ('external','she_said','self_prior'))"),
        ("is_foundation", "INTEGER NOT NULL DEFAULT 0"),
        ("foundation_category", "TEXT CHECK (foundation_category IS NULL OR foundation_category IN ("
                                "'opening','he_said_no','conflict','she_owned_a_mistake','he_changed_his_mind',"
                                "'ordinary_day','said_it_though_unsure','delayed_learning','open'))"),
        ("about_her", "INTEGER NOT NULL DEFAULT 0"),
        ("is_fragment", "INTEGER NOT NULL DEFAULT 0"),
        ("fact_changed", "INTEGER NOT NULL DEFAULT 0"),
        ("wording_changed", "INTEGER NOT NULL DEFAULT 0"),
        ("title", "TEXT"),
    ],
    "tool_calls": [
        ("message_id", "INTEGER"),
        ("call_id", "TEXT"),
        ("session_path", "TEXT"),
        ("result_state", "TEXT NOT NULL DEFAULT 'ok'"),
    ],
    "turn_usage": [
        ("source", "TEXT NOT NULL DEFAULT 'chat'"),
        ("effort", "TEXT"),
        ("roundtrips", "INTEGER"),
    ],
    "turn_handles": [
        ("source", "TEXT NOT NULL DEFAULT 'chat' CHECK (source IN ('chat','mcp'))"),
    ],
}

_FIELD_VISIBILITY = {
    ("memories", "content"): (1, "卡片正文，就是要给它读的东西"),
    ("memories", "occurred_at"): (1, "事发日期。时间用年月日"),
    ("memories", "created_at"): (1, "日期兜底：occurred_at 为空时卡面用它"),
    ("memories", "src_quote"): (1, "当时原话。recall 那条路挂出来（总纲：索引常驻，细节现拉）"),
    ("memories", "title"): (1, "抬头：**就是要给它读的**——卡面开头那个方括号里的 「…」。"
                               "跟 entities／occurred_at 同形，打包层按字段拼；空着就不给"),
    ("memories", "stance"): (1, "表态四态，跟着母卡一起给"),
    ("memories", "commitment_status"): (1, "承诺状态。不给的话「承诺」在读取侧整个是空的"),
    ("memories", "trigger_text"): (1, "设计上：旧想法进上下文时**来源要跟着一起进**"),
    ("memories", "trigger_type"): (1, "同上。🟢外部／🔵她说的／🟣它自己想的——没有这一格，闭环那条线在模型侧不存在"),
    ("memories", "write_context"): (1, "温度标注，只对现拉的卡片有用；且只能是可核实的情境事实"),

    ("memories", "src_sentence_map"): (0, "🔴 逐句溯源 id **一律不进模型侧**。它的读者是人（质检台抽检）"),
    ("memories", "author_window"): (0, "🔴 窗口编号**一个字不进模型侧**：它防的是把同一条线上连续的他切成窗1/窗3"),
    ("memories", "kind"): (0, "只作壳标记的派生输入，本身不上卡面"),
    ("memories", "target_memory_id"): (0, "指针，不是内容。表态靠它挂在母卡下，模型读到的是那条表态的正文"),
    ("memories", "bridge_memory_id"): (0, "指针，不是内容（同 target_memory_id 那条理由）。挂件靠它跟母卡连着，"
                                          "模型读到的是**桥那一头的正文**（设计上：给他的最小单位是"
                                          "「一张卡 ＋ 挂在它上面的全部东西」）。⚠️ 给了 id 反而撞下面那一条"),
    ("memories", "note_container"): (0, "容器名只用来查上一版，输出的是上一版正文本身"),
    ("memories", "importance"): (0, "已撤出排序，不上卡面"),
    ("memories", "src_conversation_id"): (0, "溯源指针，同 src_sentence_map 那条理由"),
    ("memories", "src_msg_start"): (0, "同上"),
    ("memories", "src_msg_end"): (0, "同上"),
    ("memories", "supersedes"): (0, "卡面给的是「此条覆盖了更早版本」那句派生文本，不是 id"),
    ("memories", "id"): (0, "🔴 同一个形状：窗口编号「只给人和质检台看，一个字不进模型侧」，"
                            "它防的是**把同一条线上连续的他切成碎片**——卡号是同族的内部记号。"
                            "＋实证：**正文里引用卡号**有冷读风险。"
                            "⚠️ 「它没法说我以前那条记错了」是个真问题，**已转成工程约束**（设计上："
                            "写表态的工具必须给出一个不露卡号的指向方式）。"
                            "**不许用「那就把卡号给它」绕过去**——那是为实现方便打开一条已定边界，而且不会有任何东西报错"),
    ("memories", "author"): (0, "设计上「说这是谁写的、不说可信度」那条**的落点已经是壳标记**，不是这个字段。"
                                "而 author 的实际取值是 nightly/assistant/user——**那是实现细节，不是人话**，"
                                "给了不会更准确，只会多一层它无从判断的区分"),
    ("memories", "valence"): (0, "🔴 两条依据：设计上那一条（温度标注只能是**可核实的情境事实**，不能是感受描述）"
                                 "＋另一条判据（分证据和剧本的是**能不能被对质**——能不能指着一件具体发生过的事说「你这里理解错了」）。"
                                 "**一个模型判出来的情绪数值根本无法被对质 ⇒ 它在模型侧天然是剧本不是证据。**"
                                 "📌 **字段留着**：二期主动消息引擎照用，**但它的消费者是后端，不是模型**"),
    ("memories", "arousal"): (0, "同 valence：无法被对质的判断值，模型侧不给；字段留着给后端用"),
    ("memories", "note_position"): (0, "「我现在的位置」形态等后续批次（常驻索引）定，届时再判；在那之前 fail closed"),
    ("memories", "status"): (0, "库内状态。纪念品/存疑本来就不进检索"),
    ("memories", "is_core"): (0, "库内标记"),
    ("memories", "protect"): (0, "🔴 **给蒸馏／打包层看的规则，不是给他看的材料**。"
                                 "他知道『这条不许摘要』没有任何用处 —— 摘不摘要不是他的动作；"
                                 "而告诉他哪些卡受保护，反而是在给他一张『哪些话最重』的清单，"
                                 "撞那条判据（分证据和剧本的是能不能被对质）"),
    ("memories", "tripwire"): (0, "🔴🔴 **触发词进模型侧 ＝ 把绊线的机关先告诉他。**"
                                  "绊线的全部作用是『他自己走到那个词附近时，才把当时原话给他』；"
                                  "他若先看见触发词表，就会绕开它，或者刻意去踩 —— **两种都毁掉这个机制**"),
    ("memories", "infer_seen"): (1, "🔴 推断类卡的三件套要**跟卡面一起给他**：`〔见过三回〕…`。"
                                    "**不给数量，一条推断跟一条断言长得一模一样** —— 而断言不可对质"),
    ("memories", "infer_falsifier"): (1, "同上：**推翻条件是它可被对质的凭证**。"
                                         "没有它，「你总是…」这种话他没有任何办法反驳"),
    ("memories", "infer_recheck"): (1, "同上：复看时机。**它让一条推断带着自己的保质期**"),
    ("memories", "is_deep"): (0, "库内标记，同 `is_core`。**它决定那一格收不收这张卡，"
                                 "不是他要读的内容**；而且让他看见『这条算深谈』会诱导他去够那个标签"),
    ("memories", "last_used_at"): (0, "访问强化用，库内"),
    ("memories", "use_count"): (0, "同上"),
    ("memories", "zone"): (0, "旧版遗留，当前版本已取消分区"),
    ("memories", "importance_review"): (0, "质检台回看分，给人看"),
    ("memories", "reviewed_at"): (0, "同上"),
    ("memories", "sink"): (0, "旧版沉底态，库内"),
    ("memories", "is_foundation"): (0, "奠基层由打包层整段注入原文，不靠这个标记进卡面"),
    ("memories", "foundation_category"): (0, "八类覆盖是给她挑段用的，不进模型侧"),
    ("memories", "about_her"): (0, "设计上：「关于她」**主要给她用，不是给 AI 查的**"),
    ("memories", "is_fragment"): (0, "检索侧的排序开关，库内"),
    ("memories", "fact_changed"): (0, "设计上：改事实 ⇒ 表态的对象不存在了，链在那儿断"),
    ("memories", "wording_changed"): (0, "设计上：改说法 ⇒ 表态跟过来"),

    ("messages", "content"): (1, "原文。近况层与奠基层给的就是它"),
    ("messages", "role"): (1, "轮次交替要靠它"),
    ("messages", "created_at"): (1, "时间用年月日"),
    ("messages", "thinking"): (0, "🔴 设计上（推翻原来的「只在奠基层带」）：**thinking 一律不进上下文，"
                                  "奠基层也不带**。三条理由：①原理由死了——「奠基层要可验证所以必须带」靠的是签名，"
                                  "而复验收窄：**真签名配任意 thinking 正文照样通过**；②收益边际、成本每轮"
                                  "（奠基层每轮固定在场，20 段全程携带）；③🔴 撞那个闭环——🟣「它自己以前想的」"
                                  "风险就是闭环，**把他的旧推理每轮塞回去 = 每轮让他重读自己的草稿**。"
                                  "⇒ **存是为了给她看**（前端那行灰字），**模型侧一个字都不进**"),
    ("messages", "thinking_signature"): (0, "🔴 同 thinking，模型侧不给。"
                                            "⚠️ 存它的理由后来换过：不注入之后签名**不再有任何技术用途**"
                                            "（它唯一的用途是让 thinking 块能被 API 接受）——"
                                            "留着是因为**存的成本几乎为零，而丢了就再也回不来**"),
    ("messages", "id"): (0, "账本 id。前端要持有映射，模型侧不需要"),
    ("messages", "model"): (0, "🔴 设计上写明：落库是为了**「回看分得清哪句是谁说的」，而回看的是人**。"
                               "⭐ 更关键的是它那条理由的方向——设计上写明**聊到一半悄悄换模型正是它最怕的事："
                               "把同一条线上连续的他切开**。⇒ **那就更不该由它自己读到「这一轮我是 Opus 5 / High」**："
                               "那不是身份连续，那正是把连续的他按型号切开。"
                               "⚠️ 别跟「库里其他 AI 实例照实称呼」那条混——那条管的是**从未共享上下文的另一个实例**，"
                               "不是同一条线换了型号"),
    ("messages", "effort"): (0, "同 model：算力档位是实现细节，落库给人回看，不进模型侧"),
    ("messages", "conversation_id"): (0, "库内指针"),
    ("messages", "external_id"): (0, "导入去重用"),
    ("messages", "meta"): (0, "导入元数据"),
    ("messages", "source"): (0, "🔴 防回声用（web/tg/NULL），给桥和人看，不进模型侧"),

    ("conversations", "window_name"): (0, "🔴 窗口编号不进模型侧，同 memories.author_window"),
    ("conversations", "model"): (0, "🔴 **跟 `messages.model` 同一个理由，一个字不改**："
                                    "落库是为了**「回看分得清哪句是谁说的」，而回看的是人**；"
                                    "**让他自己读到「这一轮我是 Opus 5」正是把同一条线上连续的他按型号切开**"
                                    "。"
                                    "⚠️ 本列跟 `messages.model` 的分工：那一列记「事后实际用了谁」，"
                                    "本列记「这扇窗登记的是谁」——**可见性上两者一视同仁，都是 0**"),
    ("conversations", "title"): (0, "官方端的对话标题是模型自己生成的，不是她起的——不是史料"),
    ("conversations", "id"): (0, "库内指针"),
    ("conversations", "source_end"): (0, "来源端，库内"),
    ("conversations", "external_id"): (0, "导入去重用"),
    ("conversations", "started_at"): (0, "库内"),
    ("conversations", "last_message_at"): (0, "库内"),

    ("reading_wishlist", "id"): (0, "库内指针"),
    ("reading_wishlist", "created_at"): (0, "库内"),
    ("reading_wishlist", "what"): (0, "他自己写的，不用再喂回去"),
    ("reading_wishlist", "why"): (0, "🔴 它的消费者是取材指令，不是上下文注入"),
    ("reading_wishlist", "urgency"): (0, "调度用"),
    ("reading_wishlist", "depth"): (0, "调度用"),
    ("reading_wishlist", "status"): (0, "调度用"),
    ("reading_wishlist", "parent_id"): (0, "库内指针（「再去一趟」挂同一条链）"),
    ("reading_wishlist", "started_at"): (0, "库内"),
    ("reading_wishlist", "finished_at"): (0, "库内"),
    ("reading_wishlist", "result_memory_id"): (0, "库内指针"),
    ("reading_wishlist", "note"): (0, "失败原因等，给人看"),

    ("note_sources", "memory_id"): (0, "库内指针"),
    ("note_sources", "raw_source"): (0, "出处原样，一致性用例要拿它重算"),
    ("note_sources", "source_key"): (0, "命中清单里的哪条"),
    ("note_sources", "verdict"): (0, "🔴 诊断指标，绝不给被诊断的那一方看"),
    ("note_sources", "created_at"): (0, "库内"),

    ("tool_events", "id"): (0, "库内指针"),
    ("tool_events", "tool"): (0, "哪个工具，给她和质检台看"),
    ("tool_events", "action"): (0, "装／开／关／删 —— 「他这个能力是哪天多出来的」"),
    ("tool_events", "by_who"): (0, "谁做的：她从界面做的(user) / 对账补记的(system，多半是直接改了 config.json)"),
    ("tool_events", "note"): (0, "补一句上下文（比如 URL、或「配置里有但没留痕，对账补记」）"),
    ("tool_events", "occurred_at"): (0, "库内"),

    ("tool_calls", "id"): (0, "库内指针"),
    ("tool_calls", "tool"): (0, "工具名，给人和质检台看"),
    ("tool_calls", "what"): (0, "他做了什么的那半句 —— 现在谁都不读它（那一栏已摘掉）"),
    ("tool_calls", "ok"): (0, "成没成功，给人看"),
    ("tool_calls", "occurred_at"): (0, "库内"),
    ("tool_calls", "message_id"): (0, "钉到他那句上，好让 /history 翻得回来 —— 库内指针"),
    ("tool_calls", "call_id"): (0, "会话文件里那个 toolu_… —— 只给按需取正文的接口定位用，绝不回流进 prompt"),
    ("tool_calls", "session_path"): (0, "去哪份会话文件里取正文 —— 本机路径，更不该给他"),
    ("tool_calls", "result_state"): (0, "ok/error/unknown 三态，给她和质检台看"),

    ("turn_handles", "turn_id"): (0, "库内；哪一轮"),
    ("turn_handles", "slot"): (0, "🔴 位次本身由下发层现拼给他，这张表不做注入源"),
    ("turn_handles", "memory_id"): (0, "🔴🔴 绝不出模型侧 —— 这张表存在的全部理由就是挡住它"),
    ("turn_handles", "created_at"): (0, "库内"),
    ("turn_handles", "source"): (0, "库内；哪一路发的（chat/mcp）——他不需要知道自己在哪条路上"),

    ("turn_meta", "message_id"): (0, "库内指针（他那句）"),
    ("turn_meta", "turn_id"): (0, "🔴 位次批号 —— 它是通往 turn_handles.memory_id 的路，绝不出模型侧"),
    ("turn_meta", "recall_status"): (0, "四态，给她和质检台看"),
    ("turn_meta", "recall_total"): (0, "这一轮几张卡，给她看"),
    ("turn_meta", "recall_error"): (0, "broken 的原因，给她看"),
    ("turn_meta", "created_at"): (0, "库内"),

    ("resident_notes", "id"): (0, "库内指针"),
    ("resident_notes", "slot"): (0, "格名由 resident_index 现拼成标题，这一列本身不出模型侧"),
    ("resident_notes", "text"): (1, "🔴 就是要给他读的东西 —— 她本人写下并逐条审定过的"),
    ("resident_notes", "ord"): (0, "排序用；它的读者是缓存前缀，不是他"),
    ("resident_notes", "status"): (0, "active/retired，给人和质检台看"),
    ("resident_notes", "created_at"): (0, "库内"),

    ("pinned_cards", "id"): (0, "库内指针"),
    ("pinned_cards", "memory_id"): (0, "只用来取那张卡，本身不上卡面。\n"
                                       "⚠️ **后来就地改正**：本行原写「🔴 卡号绝不出模型侧」——"
                                       "**那一半后来已经推翻了（卡号可以进模型侧）** ⇒ "
                                       "**理由作废，但结论仍然对**：她推给他的是【那张卡的内容】，"
                                       "这一格是取数用的指针，给他一个号没有任何用处（同 `target_memory_id` 那条）。"
                                       "🔴 引一条已作废的理由去支撑一个正确的结论，下一个人会照那条理由做别的决定（上面点名的就是这处）"),
    ("pinned_cards", "created_at"): (0, "库内"),
    ("pinned_cards", "used_at"): (0, "用完即清的标记，给人和质检台看"),

    ("showcase_cards", "id"): (0, "库内指针"),
    ("showcase_cards", "memory_id"): (1, "展示位是纯目录，指针就是卡号"),
    ("showcase_cards", "slot"): (0, "display / pool。**储备池只给她挑，不进他的上下文**"),
    ("showcase_cards", "dimension"): (0, "策展用的切面标签，跨切面那道闸按它数"),
    ("showcase_cards", "tone"): (0, "正/负，只为「正负都要有」那道闸"),
    ("showcase_cards", "reason"): (0, "🔴「凭什么入选」是策展理由——给他读＝告诉他该觉得哪些回忆重要"),
    ("showcase_cards", "depth_note"): (0, "替换留痕，给人复核用"),
    ("showcase_cards", "pinned_by"): (0, "model/her。是给闸看的（她钉的不许被自动轮换覆盖）"),
    ("showcase_cards", "created_at"): (0, "库内"),

    ("showcase_rotation_log", "id"): (0, "库内指针"),
    ("showcase_rotation_log", "action"): (0, "流水整张表都不进模型侧——它是给【她】看轮换有没有乱跳的"),
    ("showcase_rotation_log", "memory_id"): (0, "同上"),
    ("showcase_rotation_log", "replaced_memory_id"): (0, "同上"),
    ("showcase_rotation_log", "slot"): (0, "同上"),
    ("showcase_rotation_log", "dimension"): (0, "同上"),
    ("showcase_rotation_log", "reason"): (0, "同上"),
    ("showcase_rotation_log", "changed_by"): (0, "同上"),
    ("showcase_rotation_log", "created_at"): (0, "同上"),

    ("turn_usage", "id"): (0, "库内指针"),
    ("turn_usage", "message_id"): (0, "库内指针（对应他那句）"),
    ("turn_usage", "occurred_at"): (0, "库内"),
    ("turn_usage", "model"): (0, "实际用的那个，给人和看板"),
    ("turn_usage", "ok"): (0, "🔴 这一轮成没成 —— **失败的也记，因为失败一样计费**"),
    ("turn_usage", "input_tokens"): (0, "🔴 取顶层，不对 iterations 求和"),
    ("turn_usage", "output_tokens"): (0, "同上"),
    ("turn_usage", "cache_read"): (0, "同上；缓存命不命中靠它看"),
    ("turn_usage", "cache_write"): (0, "同上"),
    ("turn_usage", "cost_usd"): (0, "这一轮折算的美元"),
    ("turn_usage", "duration_ms"): (0, "给人看"),
    ("turn_usage", "effort"): (0, "🔴 **请求的**档位，不是「实际用的」——返回里没有后者"),
    ("turn_usage", "roundtrips"): (0, "这一轮 `-p` 内部跑了几次往返（`num_turns`）；工具一多它就 >1"),
}

def _seed_field_visibility(conn):
    conn.executemany(
        "INSERT INTO field_visibility(table_name, column_name, model_visible, note) "
        "VALUES(?,?,?,?) ON CONFLICT(table_name, column_name) DO UPDATE SET "
        "model_visible=excluded.model_visible, note=excluded.note",
        [(t, c, v, note) for (t, c), (v, note) in _FIELD_VISIBILITY.items()],
    )

def model_visible_columns(conn, table):
    return {
        r[0] for r in conn.execute(
            "SELECT column_name FROM field_visibility WHERE table_name=? AND model_visible=1",
            (table,),
        )
    }

def _ensure_columns(conn):
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

class SchemaRebuildFailed(RuntimeError):
    pass

_REBUILD_MARKERS = ("'note'", "'memento'", "'disputed'",
                    "'taboo'", "'binding'", "'unconvinced'", "'verbatim'")

def _memories_needs_rebuild(conn):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='memories'").fetchone()
    if not row or not row[0]:
        return False
    sql = row[0]
    return any(m not in sql for m in _REBUILD_MARKERS)

def _canonical_memories_ddl():
    tmp = sqlite3.connect(":memory:")
    try:
        tmp.executescript(SCHEMA)
        return tmp.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()[0]
    finally:
        tmp.close()

def _memories_census(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    chained = 0
    if "supersedes" in have:
        chained = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE supersedes IS NOT NULL"
        ).fetchone()[0]
    row = conn.execute("SELECT COUNT(*), MAX(id) FROM memories").fetchone()
    return {"条数": row[0], "最大id": row[1], "带修正链的行数": chained}

def _rebuild_memories(conn):
    canonical = _canonical_memories_ddl()
    new_cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)")]
    want = set(_col_names_from_ddl(canonical))
    lost = [c for c in new_cols if c not in want]
    if lost:
        raise SchemaRebuildFailed(
            f"memories 上有 SCHEMA 里没有的列 {lost}，重建会丢掉它们。\n"
            f"先决定这些列去留（写进 SCHEMA 或明确废弃），再重建。"
        )
    cols = ", ".join(new_cols)
    before = _memories_census(conn)

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("BEGIN")
        conn.execute(canonical.replace("memories", "memories_v3_rebuild", 1))
        conn.execute(f"INSERT INTO memories_v3_rebuild({cols}) SELECT {cols} FROM memories")
        conn.execute("DROP TABLE memories")
        conn.execute("ALTER TABLE memories_v3_rebuild RENAME TO memories")

        after = _memories_census(conn)
        if after != before:
            raise SchemaRebuildFailed(
                f"重建后对不上账，已回滚：\n  重建前 {before}\n  重建后 {after}\n"
                f"（三个数各有分工：条数=有没有丢行；最大id=会不会把 id 重排掉，"
                f"而 supersedes/embeddings/review_events 全靠 id 指过来；"
                f"带修正链的行数=链有没有被搬断。这三样断了都不会有任何症状。）"
            )
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise SchemaRebuildFailed(f"重建后外键校验不干净：{bad[:5]}")
        conn.commit()
    except BaseException:
        conn.rollback()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA legacy_alter_table = OFF")
        raise
    conn.execute("PRAGMA legacy_alter_table = OFF")
    conn.execute("PRAGMA foreign_keys = ON")

    conn.executescript(SCHEMA)
    got = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='memories'"
    )}
    if "memories_no_delete" not in got:
        raise SchemaRebuildFailed("重建后禁删触发器没回来——这把锁掉了不会有任何症状，所以在这里停住")
    conn.commit()

def _col_names_from_ddl(ddl):
    body = ddl[ddl.index("(") + 1:]
    names = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("--") or s.startswith(")"):
            continue
        token = s.split()[0].strip(",")
        if token.upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
            continue
        if token.isidentifier():
            names.append(token)
    return names

def _open(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    if _memories_needs_rebuild(conn):
        _rebuild_memories(conn)
    _seed_field_visibility(conn)
    conn.commit()
    return conn

_NOT_PRIMARY_MSG = """拒绝打开真数据库：这台机器不是主库机器。

主库同一时刻只有一台机器，靠**它自己那份 config.json 里的 "is_primary": true** 标识，
库文件写在同一份配置的 "db_path" 里。这台机器不满足，所以你要开的那个文件
就算存在，也只是一份**过期副本**。
在副本上读写不会报错，只会安静地操作副本——脚本照样打印"写入成功"，连新连接读回
验证都会通过（因为它验的也是副本）。这是本项目最难发现的一类失败，所以在这里直接拦掉。

你大概想做的是（照这个顺序查，别凭记忆认哪台是主库——它换过手，还会再换）：
  1. 查本机 config.json 的 "is_primary" 和 "db_path" 两个键：
     是不是漏配了？"db_path" 指的还是不是那份现行的库文件？
     ⚠️ 只看这两个键，**别把整份配置打印出来**——里面有密码和 API key。
  2. 本机确实不是主库 ⇒ 到主库那台机器上跑你的代码。
     **主库现在是哪台，看你自己那份 config.json 里的 `is_primary`**，
     本文件不写死。

如果你确实是想在本机跑测试或演示，明确指定库文件，两种写法都行：
  get_conn(某个临时路径)                    ← 测试夹具用的就是这个
  NACRE_DB=/tmp/demo.db python ...  ← 演示用"""

def get_conn(path=None):
    if path is not None:
        return _open(path)
    if os.environ.get("NACRE_DB"):
        return _open(db_path())
    if not load_config().get("is_primary"):
        raise NotPrimaryLedger(_NOT_PRIMARY_MSG)
    return _open(db_path())

_VERIFY_TABLES = (
    "messages", "memories", "entities", "memory_entities", "embeddings", "review_events", "watermarks",
    "threads", "memory_threads", "field_visibility", "sourcing_records", "handover_marks",
    "resident_notes",
)

def _fingerprint(conn):
    counts = {t: conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0] for t in _VERIFY_TABLES}
    marks = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM watermarks").fetchall()}
    return counts, marks

@contextmanager
def write_session(path=None, expect_memories=None, quiet=False):
    conn = get_conn(path)
    target = str(path) if path is not None else str(db_path())
    before, before_marks = _fingerprint(conn)
    try:
        yield conn
    except BaseException:
        conn.rollback()
        conn.close()
        raise
    conn.commit()
    after, after_marks = _fingerprint(conn)
    conn.close()

    check = sqlite3.connect(target)
    try:
        seen, seen_marks = _fingerprint(check)
    finally:
        check.close()
    if seen != after or seen_marks != after_marks:
        raise WriteNotPersisted(
            f"提交后新连接读到的状态与写入连接不一致，数据可能没落盘。\n"
            f"  写入连接：{after} / 水位线 {after_marks}\n"
            f"  新连接　：{seen} / 水位线 {seen_marks}\n"
            f"  库文件　：{target}"
        )

    delta = {t: after[t] - before[t] for t in after if after[t] != before[t]}
    moved = {k: (before_marks.get(k), v) for k, v in after_marks.items() if before_marks.get(k) != v}
    if expect_memories is not None and delta.get("memories", 0) != expect_memories:
        raise WriteNotPersisted(
            f"新增记忆卡数量不符：预期 {expect_memories}，实际 {delta.get('memories', 0)}。库文件：{target}"
        )
    if not quiet:
        print(f"✅ 已提交并读回验证（{target}）")
        print(f"   表变化：{delta or '无'}")
        if moved:
            print(f"   水位线：{moved}")

def watermark_lag(conn, key="extract:last_msg_id"):
    covered = conn.execute("SELECT MAX(src_msg_end) FROM memories WHERE src_msg_end IS NOT NULL").fetchone()[0] or 0
    mark = int(get_watermark(conn, key, "0") or 0)
    return mark, covered, max(0, covered - mark)

def coverage_gaps(conn, min_run, key="extract:last_msg_id", exclude_conversations=()):
    if min_run <= 0:
        return []
    upto = max(
        int(get_watermark(conn, key, "0") or 0),
        int(get_watermark(conn, "redistill:last_msg_id", "0") or 0),
    )
    if upto <= 0:
        return []
    excl = [int(x) for x in (exclude_conversations or [])]
    excl_sql = ""
    if excl:
        excl_sql = " AND m.conversation_id NOT IN (%s)" % ",".join("?" * len(excl))
    rows = conn.execute(
        "WITH uncovered AS ("
        "  SELECT m.id AS id, m.id - ROW_NUMBER() OVER (ORDER BY m.id) AS grp"
        "  FROM messages m"
        "  WHERE m.id <= ?"
        "    AND NOT EXISTS (SELECT 1 FROM memories c"
        "                    WHERE c.src_msg_start IS NOT NULL AND c.src_msg_end IS NOT NULL"
        "                      AND m.id BETWEEN c.src_msg_start AND c.src_msg_end)"
        + excl_sql +
        ") "
        "SELECT MIN(id), MAX(id), COUNT(*) FROM uncovered GROUP BY grp "
        "HAVING COUNT(*) >= ? ORDER BY MIN(id)",
        tuple([upto] + excl + [min_run]),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]

def get_watermark(conn, key, default=None):
    row = conn.execute("SELECT value FROM watermarks WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_watermark(conn, key, value):
    conn.execute(
        "INSERT INTO watermarks(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), now_iso()),
    )
