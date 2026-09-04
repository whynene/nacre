import json
import os
import secrets
import sys
import webbrowser
from pathlib import Path

from . import chat_api, runner, session_file, store
from .db import get_conn

HOME = Path(os.path.expanduser("~/.nacre-试聊天"))
CWD = HOME / "v3rt"
DB = HOME / "试聊天.db"
STATE = HOME / "入口配置.json"
HISTORY_MD = HOME / "历史.md"

DEMO = [
    ("user", "我给我那只猫起名叫『年糕』，因为它趴着的样子像。"),
    ("assistant", "年糕。那它睡醒之后会不会变形。"),
    ("user", "会，摊开就是一张饼了。"),
    ("assistant", "那就是煎饼阶段。记住了。"),
]

def _state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    s = {"path": secrets.token_urlsafe(16), "password": secrets.token_urlsafe(6),
         "secret_key": secrets.token_hex(32)}
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    return s

def ensure_proxy():
    import socket

    st = _state()
    if st.get("proxy"):
        return None if st["proxy"] == "none" else st["proxy"]

    for port in (7897, 7890, 1087, 8118):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                st["proxy"] = f"http://127.0.0.1:{port}"
                STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
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
            "  ⚠️ 别自己造一个顶上 —— 少一个我们没见过的字段，症状可能是静默的。"
        )
    st["seed"] = str(hit[0])
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
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

def load_history_md(path):
    msgs, role, buf = [], None, []

    def _flush():
        if role and buf:
            text = "\n".join(buf).strip()
            if text:
                msgs.append((role, text))

    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        head = line.strip()
        if head in ("---", "***"):
            continue
        new_role = None
        for mark, r in (("我：", "user"), ("我:", "user"), ("AI：", "assistant"), ("AI:", "assistant")):
            if head.startswith(mark):
                new_role, rest = r, head[len(mark):].strip()
                break
        if new_role:
            _flush()
            role, buf = new_role, ([rest] if rest else [])
        elif role:
            buf.append(line)
    _flush()

    while msgs and msgs[-1][0] != "assistant":
        msgs.pop()
    if not msgs:
        raise RuntimeError(
            f"{path} 里没解析出任何对话。\n"
            "  格式：每段以 `我：` 或 `AI：` 起头，段与段之间可以有 `---`。"
        )
    return msgs

def ensure_db():
    HOME.mkdir(parents=True, exist_ok=True)
    conn = get_conn(DB)
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    if n == 0:
        rows = load_history_md(HISTORY_MD) if HISTORY_MD.exists() else DEMO
        cid = store.ensure_conversation(conn, "frontend")
        for role, text in rows:
            store.append_message(conn, cid, role, text)
        conn.commit()
        n = len(rows)
        print(f"  从 {'历史.md' if HISTORY_MD.exists() else '内置演示'} 导入了 {n} 条")
    conn.close()
    return n

检索需要的配置段 = ("recall", "embedding")

def inherit_sections(names=检索需要的配置段):
    from .config import load_config

    real = load_config()
    out, missing = {}, []
    for k in names:
        seg = real.get(k)
        if not isinstance(seg, dict) or not seg:
            missing.append(k)
            continue
        out[k] = dict(seg)
    if missing:
        raise chat_api.ChatConfigError(
            f"config.json 里这几段读不到：{missing} —— **拒绝启动**。\n"
            "  检索要读它们（`recall.alpha/default_limit/max_limit` · `embedding.api_key`）。\n"
            "  🔴 **不许在这儿造一份默认顶上**：`embedding` 里是 API key，"
            "造假的等于把向量通道永久关掉，而症状只是「他好像不太能联想」，不报错。\n"
            "  怎么办：把这几段补回 `config.json`（默认值在 `nacre/config.py` 的 `DEFAULTS`）。"
        )
    return out

def review_url():
    from .config import load_config

    ra = load_config().get("review_app") or {}
    return f"http://{ra.get('host') or '127.0.0.1'}:{ra.get('port') or 8787}/?tab=today"

def build_cfg(st, seed):
    history_dir = HOME / "会话文件历史"
    return {
        **inherit_sections(),
        "v3": {
            "resident_index_file": str(HOME / "常驻输入层-{date}.txt"),
            "mcp_config_file": str(HOME / "mcp配置.json"),
        },
        "chat": {**st, "cwd": str(CWD), "seed": str(seed),
                     "projects_dir": str(Path(seed).parent),
                     "history_dir": str(history_dir),
                     "page": "chat_v3.html",
                     "review_url": review_url(),
                     "model": "claude-opus-4-6", "default_effort": "high",
                     "proxy": st.get("proxy")},
    }

def lan_ip():
    import subprocess
    for nic in ("en0", "en1"):
        try:
            p = subprocess.run(["ipconfig", "getifaddr", nic],
                               capture_output=True, text=True, timeout=3)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        except Exception:
            continue
    return None

def main():
    print("═" * 52)
    print("  本机试聊天 —— 她说一句，他带着记忆回")
    print("═" * 52)
    print(f"  库文件：{DB}")
    print("  ⚠️ 这是**演示用的临时库**，仓库里那个主库一个字都不会碰\n")

    n = ensure_db()
    ensure_proxy()
    seed, fresh = ensure_seed()
    print(f"  账本里现在 {n} 条历史 · 种子会话 {'刚生成' if fresh else '沿用已有的'}")

    st = _state()
    cfg = build_cfg(st, seed)
    Path(cfg["chat"]["history_dir"]).mkdir(parents=True, exist_ok=True)
    app = chat_api.create_app(cfg=cfg, db_path=DB)

    url = f"http://127.0.0.1:8788/{st['path']}/"
    ip = lan_ip()
    print("\n" + "─" * 52)
    print(f"  这台电脑：{url}")
    print(f"  密码：    {st['password']}")
    print("─" * 52)
    print("  默认只监听本机（127.0.0.1）—— 同一个 WiFi 上的人访问不到。")
    print("  想用手机看：在 config.json 的 `chat.host` 填 `0.0.0.0` 再重开，"
          f"地址是 http://{ip or '<这台电脑的局域网地址>'}:8788/{st['path']}/。")
    print("  ⚠️ 填了之后，同一个 WiFi 上的人都能访问（挡着的只有上面那道密码，没有 HTTPS）。")
    print("     它会真发请求、花你的额度 —— 用完关掉这个窗口就停了。")
    if HISTORY_MD.exists():
        print("\n  账本里是你放进 历史.md 的那份真对话，直接接着聊就行。")
        print("  （想换一份：把 历史.md 换掉，再删掉 试聊天.db 重开）")
    else:
        print("\n  试试问他：我那只猫叫什么名字来着？")
        print("  （他答得出那个只在上文出现过的名字，就说明记忆真的带过去了）")
    print("\n  🔴 关掉的方式：**直接关掉这个窗口**，服务就停了。\n")

    webbrowser.open(url)
    app.run(host=cfg["chat"].get("host", "127.0.0.1"), port=8788, debug=False)

if __name__ == "__main__":
    sys.exit(main())
