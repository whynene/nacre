import json
import os
import sys
import webbrowser
from pathlib import Path

from . import chat_api, chat_local, runner
from .config import ROOT, load_config

CWD = Path(os.path.expanduser("~/.nacre-v3rt"))

VAR = ROOT / "var" / "真库聊天"
STATE = VAR / "入口配置.json"
HISTORY_DIR = VAR / "会话文件历史"

DEFAULT_PORT = 8790

class RealChatRefused(RuntimeError):
    pass

def _state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}

def _save_state(st):
    VAR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")

def precheck(cfg):
    if os.environ.get("NACRE_DB"):
        raise RealChatRefused(
            "环境变量 NACRE_DB 设着 —— 它会把库指到别处。\n"
            "  真库入口拒绝在这种环境下启动：你以为在跟主库聊，其实写进了那个变量指的库，\n"
            "  而且不会有任何报错。⇒ 关掉这个终端、开个新的再双击一次。")
    if not cfg.get("is_primary"):
        raise RealChatRefused(
            "config.json 的 is_primary 不是 true —— 这台机器不认自己是主库机器。\n"
            "  真库入口只在主库机器上跑。")
    db = real_db_path(cfg)
    if not db.is_file():
        raise RealChatRefused(
            f"主库文件不存在：{db}\n"
            "  🔴 不许接着跑 —— sqlite 会静默建一个【空库】，她会对着空账本聊天，\n"
            "  症状只是「他什么都不记得」，不报错。⇒ 查 config.json 的 db_path 指的对不对。")
    锁 = {k: (cfg.get("chat") or {}).get(k) for k in ("path", "password", "secret_key")}
    缺 = [k for k, v in 锁.items() if not v]
    if 缺:
        raise RealChatRefused(
            f"config.json 的 chat 段缺门锁：{缺} —— 拒绝启动（而且是在花钱之前拒）。\n"
            "  这仨是她的正本值，这里不现生成（现生 secret_key ＝ 每次重启踢掉所有登录态）。\n"
            "  生成一次、填进 config.json：\n"
            "    python -c \"import secrets;print(secrets.token_urlsafe(16))\"   # path\n"
            "    python -c \"import secrets;print(secrets.token_hex(32))\"      # secret_key")
    return db

def real_db_path(cfg):
    p = Path(cfg["db_path"])
    return p if p.is_absolute() else ROOT / p

def _port(chat_cfg):
    raw = ROOT / "config.json"
    if raw.exists():
        explicit = ((json.loads(raw.read_text(encoding="utf-8")).get("chat") or {})
                    .get("port"))
        if explicit:
            return int(explicit)
    return DEFAULT_PORT

def ensure_proxy():
    import socket

    st = _state()
    if st.get("proxy"):
        return None if st["proxy"] == "none" else st["proxy"]
    for port in (7897, 7890, 1087, 8118):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                st["proxy"] = f"http://127.0.0.1:{port}"
                _save_state(st)
                print(f"  探到本地代理 {st['proxy']}，这次就用它")
                return st["proxy"]
        except OSError:
            continue
    print("  ⚠️ 没探到本地代理。要是待会儿报 403，多半就是这个原因（代理没开）")
    return None

def ensure_seed():
    CWD.mkdir(parents=True, exist_ok=True)
    st = _state()
    saved = st.get("seed")
    if saved and Path(saved).exists():
        return Path(saved), False

    print("第一次跑，先生成一份种子会话（会真的发一次请求，走订阅）…")
    payload = _seed_request()
    sid = payload.get("session_id")
    root = Path(os.path.expanduser("~/.claude/projects"))
    hit = sorted(root.glob(f"*/{sid}.jsonl")) if sid else []
    if not hit:
        raise RuntimeError(
            f"种子跑完了（session_id={sid}），但在 {root} 下没找到对应的会话文件。\n"
            "  ⚠️ 别自己造一个顶上 —— 少一个我们没见过的字段，症状可能是静默的。")
    st["seed"] = str(hit[0])
    _save_state(st)
    return hit[0], True

def _seed_request():
    import subprocess
    env = runner.clean_env(proxy=_state().get("proxy"))
    print(" ", runner.assert_no_api_credentials(env))
    p = subprocess.run(["claude", "-p", "请只回两个字：在的", "--output-format", "json"],
                       cwd=str(CWD), env=env, capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError("生成种子失败 —— " + runner.explain_failure(p.returncode, p.stdout, p.stderr))
    return json.loads(p.stdout)

def build_cfg(seed, proxy=None, cfg=None):
    cfg = dict(load_config() if cfg is None else cfg)
    chat = dict(cfg.get("chat") or {})
    chat.update({
        "cwd": str(CWD),
        "seed": str(seed),
        "projects_dir": str(Path(seed).parent),
        "history_dir": str(HISTORY_DIR),
        "page": "chat_v3.html",
        "review_url": chat_local.review_url(),
        "proxy": proxy,
    })
    cfg["chat"] = chat
    return cfg

def main():
    print("═" * 52)
    print("  真库聊天 —— 在新界面里，跟带着全部记忆的他说话")
    print("═" * 52)
    print("  🔴 这是【真库】——你说的每一句话永久进账本，不可删。")
    print("     验收试错请用「试聊天」。")

    cfg = load_config()
    db = precheck(cfg)

    import sqlite3
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        条数 = ro.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        ro.close()
    print(f"\n  库文件：{db}")
    print(f"  账本里已有 {条数} 条消息 —— 这个数在质检台里应该对得上\n")

    proxy = ensure_proxy()
    seed, fresh = ensure_seed()
    print(f"  种子会话 {'刚生成（花了一发）' if fresh else '沿用已有的'}：{seed}")

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    full = build_cfg(seed, proxy=proxy, cfg=cfg)
    app = chat_api.create_app(cfg=full, db_path=db)

    c = full["chat"]
    host = c.get("host") or "127.0.0.1"
    port = _port(c)
    url = f"http://127.0.0.1:{port}/{c['path'].strip('/')}/"
    print("\n" + "─" * 52)
    print(f"  地址：{url}")
    print("  密码：config.json 里 chat.password 那个（你配的那一个，这里不印出来）")
    print("─" * 52)
    print("  🔴 再说一遍：这是【真库】。每句话一落地就进不可变账本。")
    print("     每说一句真发一发请求、花订阅额度 —— 用完直接关掉这个窗口就停了。\n")

    webbrowser.open(url)
    app.run(host=host, port=port, debug=False)

if __name__ == "__main__":
    sys.exit(main())
