# Nacre

**给长期 AI 对话用的分层记忆系统。**

> Four layers of evidence, one continuous relationship,
> and a place where the agent writes its own margin.

- **分层提炼**：原文 → 记忆卡 → 摘要 → 常驻上下文，四层，越往上越薄
- **可回溯**：任意一层的内容都能追回它下一层的依据，直至逐字原文
- **底层不可变**：数据库触发器拒绝 `UPDATE` 与 `DELETE`，修正以追加方式记录

---

## 概述 / Overview

模型每开启一个新会话就会丢失全部上下文。完整历史无法全部放入上下文窗口，
而只做向量检索存在两个局限：

1. **部分信息不存在于任何单条记录中。** 长期话题的当前进展、双方尚未达成一致的分歧、
   关系的演变过程，是全部记录聚合后的结果，检索无法返回。
2. **部分信息可被检索，但模型不会主动检索。** 仍然生效的承诺、需要回避的话题，
   通常在意识到需要查询之前就已经产生了影响。

因此 Nacre 采用的判据是：**是否常驻，取决于该信息能否通过检索获得。**
可检索的不必常驻；不可检索的必须常驻。

---

## 分层结构 / Architecture

```
L3  常驻上下文    每轮请求携带的固定文本            每日生成一次
      ↑
L2  提炼摘要      关系摘要 · 长线追踪 · 实体索引     夜间批量
      ↑           模型笔记 · 承诺 · 敏感话题
L1  记忆卡        一事一卡，附原文锚点               夜间批量
      ↑
L0  对话账本      逐字原文，不可变                   实时写入
```

低层保证可查证，高层保证可理解。检索不属于这四层：四层内容每轮都会随请求发出，
检索仅在模型主动调用时发生。

L2 由两类来源组成：关系摘要与长线追踪由夜间任务写入文件，
其余各项在组装时从数据库实时查询。

### 回溯路径 / Traceability

```
常驻上下文中的一行    「⤷#499」
        ↓  recall("#499")
记忆卡 #499           事实陈述 + 原文锚点
        ↓  read_original(499)
对话账本              该卡对应的逐字原文
```

压缩通常以牺牲可追溯性为代价。Nacre 通过强制锚点避免这一点：
每张卡必须包含一句逐字原话，每条摘要携带来源卡号。

---

## 快速开始 / Quick Start

要求 Python 3.12 及以上（开发与测试在 3.14）。

```bash
git clone https://github.com/whynene/nacre.git && cd nacre
python3 -m venv .venv && .venv/bin/pip install -e .
cp config.example.json config.json
```

编辑 `config.json`，至少填写两项：

```jsonc
{
  "is_primary": true,          // 本机是否为主库所在
  "local_utc_offset_hours": 8  // 使用者时区偏移
}
```

启动：

```bash
.venv/bin/nacre-review     # 审核台，127.0.0.1:8787
.venv/bin/nacre-mcp        # MCP server（stdio）
```

运行示例，了解完整链路：

```bash
.venv/bin/python examples/quickstart.py
```

示例使用临时数据库，演示一条对话写入账本、生成记忆卡、检索、回溯原文的全过程，
并展示写入校验如何拒绝不合规的输入。

---

## 需要自行准备 / Prerequisites

以下三项不包含在仓库中，需使用者自行准备：

| 项目 | 说明 | 缺失后果 |
|---|---|---|
| `config.json` | 由 `config.example.json` 复制并填写 | 无法启动 |
| 系统提示词 | 模型的身份与说话方式定义。骨架见 `examples/system_prompt.example.md`，复制到 `他的运行时/系统提示词.md` | `chat_api` 拒绝启动 |
| 前端页面 | 本仓库不含前端。需自行实现并对接 `chat_api` | 自建对话入口不可用 |

---

## 运行状态 / Component Status

在全新环境中逐项验证的结果：

| 组件 | 状态 | 依赖 |
|---|---|---|
| 审核台 `nacre-review` | 可直接运行 | 无 |
| MCP server（stdio） | 可直接运行 | 无 |
| MCP server（`--http`） | 可运行 | 需完整填写 `mcp_http` |
| 历史对话导入 | 可运行 | 需 Claude 导出文件 |
| 夜间提炼 `nightly` | 需额外安装 | Claude Code CLI 与可用订阅 |
| 自建对话前端 | 不可用 | 见上节 |

两项预期行为，非故障：账本为空时审核台内容为空；中文单字检索通常无结果
（分词后不构成词，应使用 `search_ledger` 检索原文）。

---

## 接入方式 / Integration

**stdio（Claude Desktop / Claude Code）**

```jsonc
{
  "mcpServers": {
    "nacre": {
      "command": "/abs/path/nacre/.venv/bin/python",
      "args": ["-m", "nacre.mcp_server"],
      "cwd": "/abs/path/nacre"
    }
  }
}
```

**HTTP（远程客户端）**

```bash
.venv/bin/nacre-mcp --http
```

HTTP 模式是唯一将数据库暴露至本机之外的入口。

```jsonc
"mcp_http": {
  "path": "/mcp/<随机串>",              // 必填，访问凭据，末段不少于 16 字符
  "allowed_hosts": ["your.domain", "127.0.0.1:8765"],  // 必填，Host 白名单
  "host": "127.0.0.1",                 // 可选，默认仅监听回环
  "port": 8765,                        // 可选
  "allowed_origins": ["https://claude.ai"]             // 可选，默认仅允许 claude.ai
}
```

`path` 与 `allowed_hosts` 缺失时拒绝启动；其余三项缺失时取上述默认值，不作提示。
`allowed_hosts` 之所以强制填写，是因为空值会使 Host 白名单退化为通配，
抵消 DNS rebinding 防护。如确需放开，应显式写入 `["*"]`。

凭据位于 URL 中，会被沿途各层记入日志（应用框架、反向代理、浏览器历史），
部署时需逐层确认访问日志已关闭。

---

## 工具 / Tools

MCP server 向模型提供以下工具：

| 工具 | 说明 |
|---|---|
| `recall(query, limit)` | 检索记忆卡，同时支持按卡号查询 `recall("#499")` |
| `search_ledger(keyword, limit)` | 检索账本原文 |
| `read_original(slot, skip)` | 读取指定卡片对应的原始对话 |
| `keep(text, quote, trigger, trigger_type)` | 写入模型笔记，该区域不经人工审核 |
| `stance(target, stance, content, quote)` | 对指定卡片表态：认同、不认同、存疑、批注。原卡不变，另存记录 |
| `want_to_read` / `go_again` / `my_lists` | 模型的阅读清单 |
| `briefing()` | 读取完整常驻上下文，仅在无自动注入的客户端注册 |

两项能力被有意排除：模型不能设置卡片重要度，不能删除任何记录。

---

## 配置 / Configuration

### 常用项

| 键 | 默认值 | 留空后果 |
|---|---|---|
| `is_primary` | `false` | 拒绝打开主库，防止误操作过期副本 |
| `local_utc_offset_hours` | `8` | 提炼按 UTC 计日，跨零点对话日期偏差一天 |
| `embedding.api_key` | 空 | 仅关键词检索，启动时明确提示 |
| `recall.default_limit` | `5` | 单次检索返回数量 |
| `nightly.hour` | `7` | 夜间提炼执行时刻 |

### 进阶项

| 键 | 说明 |
|---|---|
| `nightly.model` | 提炼使用的模型。留空拒绝执行，不回退到默认值 |
| `mcp_http.*` | HTTP 模式四项配置，见上 |
| `chat.*` | 对话中转层的访问凭据与运行目录 |
| `telegram.allowed_chat_ids` | 白名单，空数组拒绝启动 |
| `v3.deictic_patterns` | 指示词黑名单，命中的卡片标记待审 |
| `v3.coverage_gap_min_run` | 连续多少条消息未被任何卡片覆盖时告警 |

配置项是否应当阻断启动，判断依据是该项**缺失时**倒向哪一侧，
而非**填错时**倒向哪一侧。

---

## 目录结构 / Layout

```
pyproject.toml       包定义
examples/            可运行示例与系统提示词骨架
nacre/
  db.py              库结构、迁移、不可变触发器
  store.py           写入校验：原文锚点、逐句溯源、重要度不可自评、不可删除
  search.py          关键词与向量双通道检索
  nightly.py         夜间提炼，含全部提炼提示词
  resident_index.py  常驻上下文组装
  narrative.py       关系摘要
  atlas.py           长线追踪
  mcp_server.py      MCP server，stdio 与 HTTP
  review_app.py      审核台
  chat_api.py        对话中转层
  tg_bridge.py       Telegram 接入
  foraging.py        隔离取材，外部材料在无记忆的独立会话中读取
```

---

## 不包含内容 / Not Included

| 项目 | 原因 |
|---|---|
| 前端页面 | 原型基于禁止商用的第三方模板，与 AGPL 不兼容 |
| 测试 | 其中五个依赖前端目录，其余需真实数据与配置 |
| 设计文档 | 含真实对话记录，不在开源范围 |

---

## 设计说明 / Design Notes

- **只记录事实，不写入指令。** 记录"何时发生过什么"，而非"应当如何回应"。
  后者构成角色设定，模型会照此执行；前者是证据，模型可自行判断当前情形是否适用。
- **副本不可作为主库使用。** 在副本上读写不会报错，只会静默操作过期数据，
  因此在建立连接时即拒绝。
- **常驻上下文在一天之内逐字不变。** 否则每轮都会触发缓存重建，
  其唯一表现是成本上升。
- **校验对象缺失时应当报错，不应跳过。** 静默通过的校验比没有校验更危险。

---

## 致谢 / Acknowledgements

- **[Ombre Brain](https://github.com/P0luz/Ombre-Brain)** —— 早期版本的设计参考了它
- **[TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)** —— 分层提炼的思路参考了它

---

## 许可 / License

[AGPL-3.0](LICENSE)。允许商业使用；若将本项目或其修改版本作为网络服务提供，
需按 AGPL 开源相应改动。

AGPL §13：`review_app` 与 `mcp_server` 属于通过网络交互的程序，
向他人提供服务时需使本仓库地址（<https://github.com/whynene/nacre>）可获取。

许可范围仅限本仓库代码。作者的设计文档与对话记录不在开源范围内。

---

## 说明 / Notes

本仓库是一个私人项目的公开切片，非通用产品。其中的取舍面向特定使用场景，
建议作为参考实现阅读。

本仓库是作者私有版本的派生：提炼提示词中的示例已整体替换为虚构内容。
因此本版本的提炼结果与作者的实际版本不一致，两者亦不保持同步，
也不接受向私有版本的反向合并。

---

## 环境变量 / Environment Variables

| 变量 | 说明 |
|---|---|
| `NACRE_DB` | 覆盖 `db_path`。测试与演示必须设置，避免写入主库 |
| `NACRE_MCP_SOURCE` | 进程身份标识 `mcp` 或 `chat`，决定注册的工具集与表态归属 |
