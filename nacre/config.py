import json
import os
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

官方内置 = [
    ("WebSearch", "world", "联网搜索"),
    ("WebFetch", "world", "抓一个网址（同上）"),
    ("Bash", "native", "跑 shell 命令"),
    ("Read", "native", "读文件（含 PDF／图片／notebook）"),
    ("Write", "native", "覆盖写一个文件"),
    ("Edit", "native", "精确替换文件里的一段"),
    ("Glob", "native", "按文件名找。⚠️ 不在 `--tools default` 里，但点得到名"),
    ("Grep", "native", "按内容搜。⚠️ 同上"),
    ("NotebookEdit", "native", "改 Jupyter 单元格"),
    ("Task", "native", "派一个子 agent"),
    ("TaskOutput", "native", "取子 agent 的输出"),
    ("TaskStop", "native", "停掉一个子 agent"),
    ("ListAgents", "native", "列出能寻址的 agent"),
    ("SendMessage", "native", "给 agent／主会话发消息"),
    ("Monitor", "native", "后台盯一个条件，出事通知"),
    ("ToolSearch", "native", "取回延迟加载的工具 schema（取回来才调得动）"),
    ("Skill", "native", "调一个打包好的技能"),
    ("Artifact", "native", "把 HTML／MD 发布成 claude.ai 私有网页"),
    ("EnterWorktree", "native", "进一个独立 git worktree"),
    ("ExitWorktree", "native", "出 worktree"),
    ("CronCreate", "native", "建一个定时任务"),
    ("CronDelete", "native", "删一个定时任务"),
    ("CronList", "native", "列定时任务"),
    ("ScheduleWakeup", "native", "让会话稍后自己醒一次"),
    ("RemoteTrigger", "native", "触发／管理云端 Claude Code 任务"),
    ("Workflow", "native", "跑可 resume 的工作流脚本"),
    ("DesignSync", "native", "跟 Claude Design 同步（需 claude.ai 登录）"),
    ("PushNotification", "native", "发系统原生通知"),
    ("ReportFindings", "native", "结构化回报审查结论"),
]

DEFAULTS = {
    "db_path": "memory.db",
    "is_primary": False,
    "embedding": {
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1/embeddings",
        "api_key": "",
        "model": "BAAI/bge-m3",
    },
    "recall": {"alpha": 0.5, "default_limit": 5, "max_limit": 20, "turn_budget_chars": 3800},
    "decay": {"half_life_days": 90},
    "core_card": {"recent_days": 14, "core_quota": 20},
    "cc_import": {"enabled": False, "project_dirs": []},
    "local_utc_offset_hours": 8,
    "nightly": {"claude_bin": "claude", "model": "", "effort": None,
                "model_cheap": "claude-sonnet-5", "effort_cheap": None,
                "hour": 7, "minute": 0},
    "review_app": {"host": "127.0.0.1", "port": 8787},
    "chat": {"path": "", "password": "", "secret_key": "",
             "cwd": "", "seed": "", "projects_dir": "", "history_dir": "",
             "host": "127.0.0.1", "port": 8788, "page": "chat.html",
             "review_url": "",
             "model": "claude-opus-4-6", "default_effort": "high"},
    "mcp_http": {"path": "", "host": "127.0.0.1", "port": 8765, "allowed_origins": ["https://claude.ai"]},
    "tools": {"registry": [
        *[{"name": 名, "kind": "builtin", "category": 类, "enabled": True, "note": 说明}
          for 名, 类, 说明 in 官方内置],
        {"name": "mcp__nacre__briefing", "kind": "mcp", "category": "read", "enabled": False,
         "note": "读交接简报。**每轮那 5 张卡是后端直接包的（pull_cards），不靠这个**——"
                 "这个是「他自己想再看一眼」那条路"},
        {"name": "mcp__nacre__recall", "kind": "mcp", "category": "read", "enabled": True,
         "note": "他想再查一件事。同上：读的下限由后端兜住，这里只加上限"},
        {"name": "mcp__nacre__read_original", "kind": "mcp", "category": "read", "enabled": True,
         "note": "把某条材料背后的账本原文取回来（按本轮位次要，不用编号）。"
                 "**表态／引原话之前该先取一次** —— 卡面那句锚不一定是最承重的那句"},
        {"name": "mcp__nacre__note", "kind": "mcp", "category": "write", "enabled": False,
         "note": "⛔ **已下架**：他不再自己写记忆卡，记忆卡一律由夜班蒸。"
                 "**不是坏了，是从来没通过** —— 它要 msg_start/msg_end，而前端从来没传过消息号。"
                 "要开回来先看上面那段注释的三件。"
                 "⛔ 本行原写「自留地」，后来改正：自留地是下面那条 keep，两者的闸都不一样"},
        {"name": "mcp__nacre__keep", "kind": "mcp", "category": "write", "enabled": True,
         "note": "自留地：他把「这一句我想留下」写进自己那一栏。"
                 "走 store.add_memory()，闸留三道、去掉逐句溯源（它的成本远高于挡下的错）"},
        {"name": "mcp__nacre__stance", "kind": "mcp", "category": "write", "enabled": True,
         "note": "对这一轮某条材料表个态。同上，一道闸都不少"},
        {"name": "mcp__nacre__want_to_read", "kind": "mcp", "category": "bell", "enabled": True,
         "note": "门铃：**不联网**，只往清单里写一行"},
        {"name": "mcp__nacre__go_again", "kind": "mcp", "category": "bell", "enabled": True,
         "note": "接着上一趟再看一次。同上，不联网"},
    ]},
    "foraging": {"note_max_chars": 1200, "quote_max_chars": 600,
                 "cwd": "/opt/forage", "timeout": 600},
    "sources": {"registry": []},
    "v3": {
        "nightly_at": "07:00",
        "verdict_patterns": [
            "是[^，。；！？\n]{0,12}的(起点|转折点|本质|核心|缩影)",
            "(意味着|标志着|代表着|体现了|反映了|说明了)",
            "最[^，。；！？\n]{0,10}的一(次|个)",
            "这(件事|段|次)(是|让|使)",
            "(核心)?结论[:：]",
        ],
        "deictic_patterns": ["这个窗口", "当前实例", "本次", "今天", "刚才", "现在"],
        "cache_write_daily_expected": 2,
        "coverage_gap_min_run": 100,
        "no_distill_conversations": [3],
        "chunk_gap_hours": 4,
        # 不需要额外解释的通用词（地名、技术名词一类）。这份清单跟着使用者的语境长，
        # 所以默认是空的 —— 在 config.json 里填自己的。
        "one_liner_exclude": [],
        "foundation_quota": 20,
        "foundation_open_quota": 5,
        "recent_layer_max_turns": 50,
        "handover_warn_ratio": 0.7,
        "resident_index_channel": "system",
        "resident_index_file": "var/常驻输入层-{date}.txt",
        "mcp_config_file": "var/mcp配置.json",
    },
}

def _merge(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v

def load_config():
    cfg = deepcopy(DEFAULTS)
    path = ROOT / "config.json"
    if path.exists():
        try:
            _merge(cfg, json.loads(path.read_text(encoding="utf-8")))
        except Exception as e:
            raise RuntimeError(f"config.json 解析失败：{e}")
    return cfg

PLACEHOLDER_MARKS = ("change-me", "changeme", "your-", "xxx", "todo")
MIN_PATH_SEGMENT = 16

def http_settings(cfg):
    http = cfg.get("mcp_http") or {}
    path = (http.get("path") or cfg.get("mcp_secret_path") or "").strip()

    problem = ""
    if not path:
        problem = "未设置 mcp_http.path"
    elif not path.startswith("/"):
        problem = f"mcp_http.path 必须以 / 开头（当前：{path}）"
    elif any(m in path.lower() for m in PLACEHOLDER_MARKS):
        problem = f"mcp_http.path 还是占位符（当前：{path}）"
    elif len(path.rstrip("/").rsplit("/", 1)[-1]) < MIN_PATH_SEGMENT:
        problem = f"mcp_http.path 末段太短，猜得出来就等于没有（至少 {MIN_PATH_SEGMENT} 个字符）"

    warnings = []
    stale = [h for h in (http.get("allowed_hosts") or []) if any(m in h.lower() for m in PLACEHOLDER_MARKS)]
    if stale:
        warnings.append(
            f"mcp_http.allowed_hosts 里还留着占位符 {stale}。\n"
            f"隧道会把 Host 头改成隧道域名，不在白名单里的请求会被拒——\n"
            f"请把你的真实隧道域名填进去，或整个删掉这一项\n"
            f"（删掉 = 不限制 Host，安全性由 path 和 allowed_origins 兜底）。"
        )
    return http, path, problem, warnings

def db_path(cfg=None):
    env = os.environ.get("NACRE_DB")
    if env:
        return Path(env)
    cfg = load_config() if cfg is None else cfg
    p = Path(cfg["db_path"])
    return p if p.is_absolute() else ROOT / p
