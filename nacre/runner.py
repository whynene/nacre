import json
import os
import subprocess
from pathlib import Path

_ENV_PASSTHROUGH = ("HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "LANG",
                    "HTTPS_PROXY", "HTTP_PROXY")

_FORBIDDEN = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

class RunnerError(Exception):
    pass

def clean_env(base=None, proxy=None):
    src = os.environ if base is None else base
    env = {k: src[k] for k in _ENV_PASSTHROUGH if src.get(k)}
    env.setdefault("TMPDIR", "/tmp/")
    env.setdefault("LANG", "en_US.UTF-8")
    if proxy:
        env["HTTPS_PROXY"] = env["HTTP_PROXY"] = proxy
    return env

def assert_no_api_credentials(env):
    hit = [k for k in _FORBIDDEN if env.get(k)]
    if hit:
        raise RunnerError(
            f"🔴 凭证闸失败：子进程环境里有 {hit}\n"
            "   本项目走**订阅**（authMethod: claude.ai）。这两个变量会**无条件压过订阅登录、"
            "静默切到 API 计费、真花钱**，而且不会有任何提示。"
        )
    return f"凭证闸通过：子进程环境 {len(env)} 个变量，{list(_FORBIDDEN)} 均不存在"

def build_argv(session_id, prompt, model=None, effort=None, claude_bin="claude",
               tools=None, allowed_tools=None, mcp_config=None, strict_mcp=False,
               new_session=False, system_prompt=None, append_system_prompt=None,
               append_system_prompt_file=None, stream_input=False):
    出格式 = ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"] \
        if stream_input else ["--output-format", "json"]
    前置 = [] if stream_input else [prompt]
    if session_id is None:
        if new_session:
            raise RunnerError(
                "`new_session=True` 却没给 session_id —— 这两件事互相矛盾，"
                "**不替调用方猜一个会话号**（猜出来的号会静默落一份没人管的会话文件）。"
            )
        argv = [claude_bin, "-p"] + 前置 + 出格式
    else:
        flag = "--session-id" if new_session else "--resume"
        argv = [claude_bin, "-p"] + 前置 + [flag, session_id] + 出格式
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    argv += ["--thinking-display", "summarized"]
    if tools is not None:
        argv += ["--tools", ",".join(tools)]
    if allowed_tools:
        argv += ["--allowedTools", ",".join(allowed_tools)]
    if mcp_config:
        argv += ["--mcp-config", str(mcp_config)]
    if system_prompt:
        argv += ["--system-prompt", system_prompt]
    if append_system_prompt and append_system_prompt_file:
        raise RunnerError(
            "`append_system_prompt` 和 `append_system_prompt_file` 只能给一个 —— "
            "两个一起传时 CLI 怎么处理**没有实测过**，而猜错的症状是"
            "「他少读了半份常驻输入层」，且不会有任何东西报错。"
        )
    if append_system_prompt:
        argv += ["--append-system-prompt", append_system_prompt]
    if append_system_prompt_file:
        argv += ["--append-system-prompt-file", str(append_system_prompt_file)]
    if strict_mcp:
        argv += ["--strict-mcp-config"]
    return argv

def explain_failure(returncode, stdout, stderr, gate=""):
    detail, hint = "", ""
    try:
        j = json.loads(stdout or "")
    except (json.JSONDecodeError, TypeError):
        j = None
    if isinstance(j, dict):
        detail = str(j.get("result") or "")[:400]
        status = j.get("api_error_status")
        if status == 403 or "authenticate" in detail.lower():
            hint = ("\n   👉 **多半是网络没通到 Anthropic**（不是密码问题、也不是记忆库坏了）：\n"
                    "      · 代理开着吗？开着的话把它的地址填进 `chat.proxy`\n"
                    "      · 终端里手动跑一次 `claude -p 你好` 能不能成——不成就是环境的事，跟本项目无关")
    if not detail:
        detail = (stderr or "").strip()[:400] or "（stdout 和 stderr 都是空的）"
    return f"`claude -p` 退出码 {returncode}：{detail}{hint}\n   （{gate}）"

def run_turn(cwd, session_id, prompt, model=None, effort=None, claude_bin="claude", timeout=600,
             proxy=None, tools=None, allowed_tools=None, mcp_config=None, strict_mcp=False,
             new_session=False, system_prompt=None, append_system_prompt=None,
             append_system_prompt_file=None, content_blocks=None, 走stdin=False,
             run_as=None):
    env = clean_env(proxy=proxy)
    gate = assert_no_api_credentials(env)

    if append_system_prompt_file and not Path(append_system_prompt_file).exists():
        raise RunnerError(
            f"`--append-system-prompt-file` 指的文件不在：{append_system_prompt_file}\n"
            "· 夜间维护窗口没跑？· 还是 `v3.resident_index_file` 配错了？\n"
            "🔴 **这里刻意不降级成「不带常驻输入层跑一轮」** —— 那一轮他会失忆，"
            "而她只会觉得「他今天怪怪的」，没有任何东西会报错。"
        )

    stream_input = bool(content_blocks) or bool(走stdin)
    argv = build_argv(session_id, prompt, model=model, effort=effort, claude_bin=claude_bin,
                      tools=tools, allowed_tools=allowed_tools, mcp_config=mcp_config,
                      strict_mcp=strict_mcp, new_session=new_session,
                      system_prompt=system_prompt, append_system_prompt=append_system_prompt,
                      append_system_prompt_file=append_system_prompt_file,
                      stream_input=stream_input)
    stdin_data = None
    if stream_input:
        块 = [{"type": "text", "text": prompt}] + list(content_blocks or [])
        stdin_data = json.dumps(
            {"type": "user", "message": {"role": "user", "content": 块}},
            ensure_ascii=False) + "\n"
    try:
        跑 = dict(cwd=str(cwd), env=env, capture_output=True, input=stdin_data,
                 text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if run_as:
            import pwd
            信息 = pwd.getpwnam(run_as)
            跑["user"] = 信息.pw_uid
            跑["group"] = 信息.pw_gid
            跑["env"] = {**env, "HOME": 信息.pw_dir, "USER": run_as, "LOGNAME": run_as}
        proc = subprocess.run(argv, **跑)
    except FileNotFoundError:
        raise RunnerError(f"找不到 {claude_bin} —— 非交互 shell 的 PATH 里没有 `~/.local/bin`")
    except subprocess.TimeoutExpired:
        raise RunnerError(f"`claude -p` 超过 {timeout} 秒没返回 —— 当作失败，**不许当成空回复往下走**")

    if proc.returncode != 0:
        raise RunnerError(explain_failure(proc.returncode, proc.stdout, proc.stderr, gate))
    if stream_input:
        payload = None
        for 行 in (proc.stdout or "").splitlines():
            try:
                ev = json.loads(行)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict) and ev.get("type") == "result":
                payload = ev
        if payload is None:
            raise RunnerError(
                "`claude -p --output-format stream-json` 退出码是 0，但事件流里**没有 result 事件**"
                " —— **进程活着不等于请求成功**。\n"
                f"   前 800 字：{(proc.stdout or '').strip()[:800]}"
            )
    else:
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RunnerError(
                "`claude -p` 退出码是 0，但输出不是 JSON —— **进程活着不等于请求成功**。\n"
                f"   前 800 字：{(proc.stdout or '').strip()[:800]}"
            )

    text = payload.get("result")
    if not isinstance(text, str) or not text.strip():
        raise RunnerError(
            "返回里没有结果文本 —— 按验收状态机这一轮不算成功。\n"
            "   ⚠️ **绝不许把它当成「他没话说」往账本里写一条空消息**：账本不可变，写错了删不掉。\n"
            f"   返回的键：{sorted(payload)[:20]}"
        )
    if payload.get("is_error"):
        raise RunnerError(f"返回里 is_error=true：{str(payload.get('result'))[:400]}")

    return {
        "text": text,
        "usage": payload.get("usage") or {},
        "effort": effort,
        "num_turns": payload.get("num_turns"),
        "total_cost_usd": payload.get("total_cost_usd"),
        "model": _model_of(payload, requested=model) or model,
        "session_id": payload.get("session_id") or session_id,
        "raw": payload,
    }

def _model_of(payload, requested=None):
    mu = payload.get("modelUsage")
    if not isinstance(mu, dict) or not mu:
        return None
    if requested and requested in mu:
        return requested
    if len(mu) == 1:
        return next(iter(mu))

    def _out(name):
        v = mu.get(name)
        return (v or {}).get("outputTokens") or (v or {}).get("output_tokens") or 0

    return max(sorted(mu), key=_out)
