import json
import re
import sys
from pathlib import Path

from .config import DEFAULTS, ROOT, load_config
from .config import 官方内置 as _官方内置

NATIVE_TOOL_NAMES = frozenset(名 for 名, _类, _说明 in _官方内置)

CATEGORIES = {
    "read":  "读库（`recall`/`briefing` 那种）——主会话要，隔离会话绝不给",
    "write": "往库里写（自留地 · 表态）——**走四道闸，不许旁路**",
    "bell":  "门铃：不联网的「我想读 X，因为 Y」，只往清单里写一行",
    "world": "他去看世界（WebSearch / WebFetch）——**只在隔离会话里存在**",
    "sense": "🔴 感知类（环境与状态输入）——**全新的一类**，"
             "产出的不是「用户说了什么」而是「当时的状态」，**存不存怎么存未定**",
    "native": "🔴 **官方内置的其余 27 个**（碰文件 · 派子 agent · 排程 · 发布…，已登记）。"
              "**上面那五类是为这个记忆库设计的**（读库／写库／门铃／看世界／感知她），"
              "而这 27 个的真实分类轴是别的 ⇒ **没硬塞，单给一类**"
              "（实测：29 个官方内置里只有 2 个落得进去）。"
              "⚠️ **它不进 `MAIN_SESSION_CATEGORIES`，是故意的**：内置工具走的是 `--tools` 那条路"
              "（`enabled_builtins()`），跟 `--allowedTools` 那条**互不覆盖**。",
    "external": "🔴 **她自己从界面上装的 MCP server** ——「前端能加·关·删 MCP」"
                "落地时长出来的那一类。它**去主会话**，因为她装它的全部目的就是"
                "（目标是：像在官方端一样操作就能连上）——"
                "装完他还是拿不到，那就是一个空开关。",
}

MAIN_SESSION_CATEGORIES = ("read", "write", "bell", "external")
ISOLATED_CATEGORIES = ("world",)

DEFAULT_REGISTRY = DEFAULTS["tools"]["registry"]

_KINDS = ("builtin", "mcp", "mcp_server")

_SERVER_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

BUILTIN_SERVER = "nacre"

class ToolRegistryError(ValueError):
    pass

def registry(cfg=None):
    cfg = load_config() if cfg is None else cfg
    block = cfg.get("tools")
    if block is None:
        items = DEFAULT_REGISTRY
    elif not isinstance(block, dict):
        raise ToolRegistryError(f"config 里的 tools 必须是一个对象，收到 {type(block).__name__}")
    else:
        items = block.get("registry", DEFAULT_REGISTRY)
    if not isinstance(items, list):
        raise ToolRegistryError(
            f"tools.registry 必须是数组，收到 {type(items).__name__}。\n"
            '每一项形如 {"name": "WebSearch", "kind": "builtin", "category": "world", "enabled": true}'
        )

    out, seen = [], set()
    for i, raw in enumerate(items, 1):
        if not isinstance(raw, dict):
            raise ToolRegistryError(f"tools.registry 第 {i} 项不是对象：{raw!r}")
        name = (raw.get("name") or "").strip()
        kind = (raw.get("kind") or "").strip()
        category = (raw.get("category") or "").strip()
        if not name:
            raise ToolRegistryError(f"tools.registry 第 {i} 项没有 name")
        if kind not in _KINDS:
            raise ToolRegistryError(
                f"工具 {name} 的 kind 只能是 {' / '.join(_KINDS)}，收到 {kind!r}。\n"
                "🔴 这两者不是同一个开关：builtin 走 --tools（**能做到看不见**），"
                "mcp 走 --allowedTools（**只做得到调不着**）。混了的话，"
                "一个本该消失的工具会照样躺在他每一轮的上下文里，而且不报错。"
            )
        if category not in CATEGORIES:
            raise ToolRegistryError(
                f"工具 {name} 的 category 只能是 {' / '.join(CATEGORIES)}，收到 {category!r}。\n"
                + "\n".join(f"  {k} = {v}" for k, v in CATEGORIES.items())
            )
        if kind == "mcp" and len(name.split("__")) < 3:
            raise ToolRegistryError(
                f"MCP 工具名要写成 `mcp__server__tool` 那种全名，收到 {name!r}。\n"
                "🔴 实测：白名单**能**点名到单个 MCP 工具（对照组：只给 `mcp__probe` "
                "则整组放行）。写不全名 = 悄悄把整组打开。"
            )
        if kind == "builtin" and name not in NATIVE_TOOL_NAMES:
            raise ToolRegistryError(
                f"`{name}` 不在官方那 29 个内置工具里。\n"
                "🔴 `--tools` 收到一个不存在的名字是**静默忽略**（实测：不报错、退出码正常）"
                "⇒ 打错一个字母 ＝ 那一路安安静静少一个工具，而没有任何东西会说一句。\n"
                f"⇒ 正本是 `config.官方内置`（{len(NATIVE_TOOL_NAMES)} 个）。"
                "官方要是又加了新工具，**去改那张表，别在这儿绕过闸**。"
            )
        spec = None
        if kind == "mcp_server":
            _check_server_name(name)
            spec = check_spec(raw.get("spec"), name)
        if name in seen:
            raise ToolRegistryError(f"工具 {name} 在注册表里出现了两次——两份清单必然分头漂移")
        seen.add(name)
        out.append({"name": name, "kind": kind, "category": category,
                    "enabled": bool(raw.get("enabled", True)),
                    "server": raw.get("server") or (name if kind == "mcp_server"
                                                    else _server_of(name)),
                    "spec": spec,
                    "label": (raw.get("label") or "").strip() or name,
                    "irreversible": bool(raw.get("irreversible")),
                    "note": raw.get("note") or ""})
    return out

def _check_server_name(name):
    if not _SERVER_NAME_OK.match(name) or "__" in name:
        raise ToolRegistryError(
            f"「{name}」不能直接当 MCP 的 server 名字。\n"
            "🔴 这个名字会被拼进 `mcp__<server>__<工具>` 那个前缀 ⇒ "
            "**只能用英文字母、数字、`-`、`_`、`.`，而且中间不能出现两个连着的下划线。**\n"
            "⇒ 你想叫它什么中文名字都行（那个另存一格），"
            "但这里要一个短英文名 —— 你粘的那段配置里如果带了名字，直接用那个就好。"
        )
    if name == BUILTIN_SERVER:
        raise ToolRegistryError(
            f"`{BUILTIN_SERVER}` 这个名字是记忆库自己那一支占着的，不能被顶掉。\n"
            "🔴 **它的启动配置里钉着 `NACRE_DB`** —— 那是"
            "「他写卡别写错库」的唯一一道闸（设计上：拿演示库聊天，"
            "他一调 `keep`／`stance` 就写进主库，而且什么都不会红）。\n"
            "⇒ 换一个名字。"
        )

def check_spec(spec, who=""):
    谁 = f"「{who}」" if who else "这个工具"
    if not isinstance(spec, dict) or not spec:
        raise ToolRegistryError(
            f"{谁}没有启动配置 —— 光有名字它装不上。\n"
            "⇒ 把那段配置粘进来，长这样（跟你在官方端加 MCP 时粘的是同一段）：\n"
            '   {"command": "npx", "args": ["-y", "某个包名"]}\n'
            '   或者：{"url": "https://某个地址/mcp"}')
    command = (spec.get("command") or "").strip() if isinstance(spec.get("command"), str) else ""
    url = (spec.get("url") or "").strip() if isinstance(spec.get("url"), str) else ""
    if not command and not url:
        raise ToolRegistryError(
            f"{谁}这段配置里既没有 `command`（要跑的那条命令），也没有 `url`（要连的那个地址）。\n"
            "🔴 两样至少要有一样，否则它**起不来** —— 而起不来的时候 `claude` "
            "**不报错，只是安安静静少几个工具**：你会以为它装上了。\n"
            "⇒ 再去复制一次完整的那一段，别只复制里面一行。")
    if command and url:
        raise ToolRegistryError(
            f"{谁}这段配置里 `command` 和 `url` 都写了 —— 只能要一样。\n"
            "`command` ＝ 在这台机器上起一个进程；`url` ＝ 连一个已经在跑的地址。\n"
            "⇒ 留下你真正要的那一个。")
    out = {}
    if command:
        out["command"] = command
        args = spec.get("args", [])
        if args in (None, ""):
            args = []
        if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            raise ToolRegistryError(
                f"{谁}的 `args` 要是一串文本（数组），比如 `[\"-y\", \"某个包名\"]`。\n"
                "⇒ 照抄你复制来的那一段，别自己改它的形状。")
        out["args"] = list(args)
    else:
        out["url"] = url
        if isinstance(spec.get("type"), str) and spec["type"].strip():
            out["type"] = spec["type"].strip()
    env = spec.get("env")
    if env not in (None, "", {}):
        if not isinstance(env, dict) or any(not isinstance(v, str) for v in env.values()):
            raise ToolRegistryError(
                f"{谁}的 `env` 要是一组「名字: 值」，值都得是文本。\n"
                "⇒ 照抄你复制来的那一段。")
        out["env"] = dict(env)
    if isinstance(spec.get("headers"), dict):
        out["headers"] = {k: v for k, v in spec["headers"].items() if isinstance(v, str)}
    return out

def parse_paste(text, name_hint=""):
    text = (text or "").strip()
    if not text:
        raise ToolRegistryError(
            "还没粘配置。\n"
            "⇒ 在那个工具的说明页上找到「加到 Claude」那一段（一段大括号包着的文字），"
            "整段复制过来粘进这个框。")
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except Exception as e:
        raise ToolRegistryError(
            "这段配置读不懂 —— 多半是没复制全（少了开头或结尾的大括号），"
            "或者中间掺了别的字。\n"
            "⇒ 从 `{` 开始到最后一个 `}` 结束，整段一起复制。\n"
            f"（排查用的原文：{type(e).__name__}: {e}）") from None
    if not isinstance(data, dict):
        raise ToolRegistryError(
            "这段配置的形状不对：它该是一段大括号 `{…}` 包着的东西。\n"
            "⇒ 再复制一次完整的那一段。")

    hint = (name_hint or "").strip()
    if isinstance(data.get("mcpServers"), dict):
        servers = data["mcpServers"]
        if not servers:
            raise ToolRegistryError(
                "这段配置里的 `mcpServers` 是空的 —— 里面一个工具都没有。\n"
                "⇒ 再复制一次完整的那一段。")
        if len(servers) > 1:
            raise ToolRegistryError(
                f"这段配置里有 {len(servers)} 个工具（{'、'.join(list(servers)[:4])}…）—— "
                "**一次只装一个**。\n"
                "🔴 一次装一堆的话，那个勾（会不会花钱／发给别人／控制设备）"
                "就说不清是在说哪一个了。\n"
                "⇒ 把你要的那一个单独复制出来，装完再来装下一个。")
        server, spec = next(iter(servers.items()))
        server = (server or "").strip()
    else:
        server, spec = hint, data
        if not server:
            raise ToolRegistryError(
                "这段配置里没带名字，所以得你给它起一个（上面那个格子）。\n"
                "⇒ 一个短英文名就行，比如 `weather`、`my-notes`。\n"
                "（带名字的配置长这样：`{\"mcpServers\": {\"weather\": {…}}}`）")
    _check_server_name(server)
    return server, check_spec(spec, server)

def _server_of(name):
    if not name.startswith("mcp__"):
        return None
    parts = name.split("__")
    return parts[1] if len(parts) >= 3 else None

def _select(cfg, categories, who):
    reg = registry(cfg)
    picked = [t for t in reg if t["enabled"] and t["category"] in categories]

    if any(t["category"] == "sense" for t in reg if t["enabled"]):
        raise ToolRegistryError(
            "注册表里有 category=sense 的工具（感知类：环境与状态输入）。\n"
            "🔴 **这一类产出的不是一条消息**——不是「用户说了什么」，是「**当时她的状态是什么**」。\n"
            "   设计上明写着：**不是消息，别塞进 `messages`**，存不存、怎么存要单独定"
            "（形状同那张轻表）。\n"
            "\n"
            "**需要维护者拍的是三句，照这三句去问**：\n"
            "  ① 这类数据要不要入库？\n"
            "  ② 要的话入哪张表？（`messages` 是不行的——账本不可变，而状态是连续采样的）\n"
            "  ③ 模型侧要不要可见？\n"
            "\n"
            "🔴 **在使用者确认之前，这一类只能登记、不能处理——这是设计，不是没做完。**\n"
            "   （别顺手把它实现掉：那三处未定是要单独讨论的。）\n"
            "⇒ 暂时先把那几条 `enabled` 设成 false，它们照样登记在册。"
        )

    banned = [t["name"] for t in reg
              if t["enabled"] and t["category"] == "world" and "world" not in categories]
    if who == "main" and banned:
        assert not [t for t in picked if t["category"] == "world"], \
            f"主会话里出现了联网工具 {banned}——这条设计决定就是为了消灭它"
    return picked

def main_session_argv_opts(cfg=None):
    picked = _select(cfg, MAIN_SESSION_CATEGORIES, who="main")
    allowed = [t["name"] for t in picked if t["kind"] == "mcp"]
    allowed += [f"mcp__{t['server']}" for t in picked if t["kind"] == "mcp_server" and t["server"]]
    return {
        "tools": [t["name"] for t in picked if t["kind"] == "builtin"],
        "allowed_tools": allowed,
        "mcp_servers": sorted({t["server"] for t in picked
                               if t["kind"] in ("mcp", "mcp_server") and t["server"]}),
    }

def enabled_builtins(cfg=None):
    return [t["name"] for t in registry(cfg) if t["kind"] == "builtin" and t["enabled"]]

def isolated_session_argv_opts(cfg=None):
    picked = _select(cfg, ISOLATED_CATEGORIES, who="isolated")
    mcp = [t["name"] for t in picked if t["kind"] in ("mcp", "mcp_server")]
    if mcp:
        raise ToolRegistryError(
            f"隔离会话要用的联网工具里有 MCP 的：{mcp}。\n"
            "🔴 隔离会话**一个 MCP server 都不接**——那是唯一能让写库工具从他视野里消失的粒度"
            "（MCP 工具只关得掉调用、关不掉可见性〔实测 · CLI 2.1.138〕）。\n"
            "⇒ 联网能力请用内置的 `WebSearch` / `WebFetch`；"
            "**真要用 MCP 取材，得先想清楚怎么在接上它的同时不让写库工具也一起可见**——"
            "那是一个还没解的设计问题，不是这里能兜的。"
        )
    return {
        "tools": [t["name"] for t in picked if t["kind"] == "builtin"],
        "allowed_tools": [],
        "mcp_servers": [],
    }

MCP_SERVER_PY = Path(__file__).resolve().parent / "mcp_server.py"

def mcp_server_spec(server, db_path, cfg=None, source=None):
    if server == BUILTIN_SERVER:
        return {
            "command": sys.executable,
            "args": [str(MCP_SERVER_PY)],
            "env": {"NACRE_DB": str(db_path),
                    **({"NACRE_MCP_SOURCE": source} if source else {})},
        }
    for t in registry(cfg):
        if t["kind"] == "mcp_server" and t["server"] == server and t["spec"]:
            return dict(t["spec"])
    raise ToolRegistryError(
        f"注册表里有 `mcp__{server}__…` 这样的工具，但没人知道 `{server}` 这个 server 怎么启动。\n"
        "🔴 **不静默跳过**：跳过的话，她在界面上打开的是一个空开关——\n"
        "   工具在清单里亮着，而他那一侧根本没有这个东西，**没有任何提示**。\n"
        f"⇒ 要么在工具页上把 `{server}` 重新装一次（这次把那段启动配置粘进去），\n"
        "  要么把它那几条工具的 `enabled` 关掉（关掉照样登记在册）。"
    )

def mcp_config_path(cfg=None):
    cfg = load_config() if cfg is None else cfg
    raw = ((cfg.get("v3") or {}).get("mcp_config_file") or "").strip()
    if not raw:
        raise ToolRegistryError(
            "`v3.mcp_config_file` 是空的 —— 主会话要挂 MCP server 就得有一份 "
            "`--mcp-config` 文件，**它的落点必须由配置给**。\n"
            "🔴 **这里刻意不回退到一个算出来的默认路径**：回退过一版，"
            "结果是每个临时库都在仓库 `var/` 里落一个文件，**跑得通、不报错、没人回来删**。\n"
            "⇒ 拼配置的地方补上它（生产走 `config.DEFAULTS`，演示走 "
            "`chat_local.build_cfg()`，用例指到 tmp）。"
        )
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p

def write_mcp_config(servers, db_path, cfg=None, out_path=None, source=None):
    if not servers:
        return None
    if not db_path:
        raise ToolRegistryError(
            "要挂 MCP server 却没给 `db_path`。\n"
            "🔴 `mcp_server.py` 不传路径就自己去读 `config.json` 的 `db_path` ⇒ **主库**。\n"
            "   拿演示库聊天时，他一调 `note`／`stance` 就写进主库，而且**不会报错**。\n"
            "⇒ 这里不给默认值：**不知道该写哪个库，就不生成这份配置。**"
        )
    body = {"mcpServers": {s: mcp_server_spec(s, db_path, cfg=cfg, source=source)
                           for s in servers}}
    text = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
    out = Path(out_path) if out_path else mcp_config_path(cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists() or out.read_text(encoding="utf-8") != text:
        out.write_text(text, encoding="utf-8")
    return out

ALLOWLISTED_BUILTINS = frozenset({"WebSearch", "WebFetch"})

def main_session_run_opts(cfg=None, db_path=None, out_path=None):
    opts = main_session_argv_opts(cfg)
    builtins_on = enabled_builtins(cfg)
    return {
        "tools": builtins_on,
        "allowed_tools": opts["allowed_tools"]
                         + [n for n in builtins_on if n in ALLOWLISTED_BUILTINS],
        "mcp_config": write_mcp_config(opts["mcp_servers"], db_path, cfg=cfg,
                                       out_path=out_path, source="chat"),
        "strict_mcp": True,
    }
