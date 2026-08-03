# tgrok 架构设计文档

Telegram 群聊 AI 助手（类似 X 上的 @grok 用法）。后端为任意 OpenAI 兼容接口,
可选 Gemini 原生接入与 Google grounding 搜索。

## 模块划分

```
bot.py              入口（python bot.py），只做 from tgrok.tg import main
configure.py        交互式配置向导 + 多配置档管理（独立运行，不进 Docker 镜像）
tests/smoke_test.py 冒烟/回归套件（无需网络与真实 Telegram，python tests/smoke_test.py）
tgrok/
├── config.py   环境变量解析、常量、日志与时区初始化（无内部依赖，最底层）
├── i18n.py     全部界面/提示词文案（zh/en 键严格对齐）+ t()
├── prompt.py   SYSTEM_PROMPT 组装（含搜索能力声明）+ 实时时间注入 with_time()
├── llm.py      模型接入层：OpenAI 兼容流式(create_stream/_drain_stream)、
│               Gemini 原生流式、工具 schema、tool_call 聚合与 assistant 消息重组
├── web.py      联网层：多搜索源并发聚合、Gemini grounding 检索、
│               网页抓取（进程隔离提取 + Jina 兜底）
├── chat.py     会话层：TUI 进度显示、工具执行(同轮合并)、stream_reply 主循环、
│               取消按钮回调
├── tg_auth.py  白名单/管理员判定（chat 与 tg 共用，独立成小模块防循环导入）
└── tg.py       Telegram 接入层：消息路由、图片/相册、管理命令、应用装配 main()
```

依赖方向自底向上：config ← i18n ← prompt/llm/web ← chat ← tg。无循环导入。
可变运行时状态的归属：`llm.tools_supported`（后端拒绝 tools 的粘性开关）、
`config._gemini_search_blocked_until`（grounding 429 冷却）、`chat.active_generations`
（进行中生成任务，取消按钮用）、`tg_auth.allowed_users`、`tg.conversations`/`album_cache`。
跨模块引用一律走模块属性（`config.X`/`llm.f()`），保证测试可 monkeypatch。

## 一条消息的生命周期

```
Telegram update
  → tg.handle_message：路由（@提及/回复bot/私聊）、鉴权、相册被动收集、
    组装 history（system + 引用上下文 + 图片 + with_time 时间注入）
  → chat.stream_reply：
      占位气泡（带 取消按钮）→ 注册 active_generations
      ┌ 原生模式(GEMINI_NATIVE_SEARCH)：llm.gemini_create_stream 单轮，
      │ grounding 引用附「来源：」链接
      └ 工具循环（≤SEARCH_MAX_ROUNDS 轮，最后一轮强制无工具）：
          llm.create_stream(OpenAI 兼容) → llm._drain_stream
          → 模型请求工具 → chat._execute_tool_calls
              web_search → web.run_web_search：grounding 优先（429 冷却回退）
                           → 多源并发聚合（交错合并+URL去重）
              open_url   → web.run_fetch_url：直取（httpx 共享连接池 →
                           独立进程 trafilatura 提取，10s 看门狗强杀）→ Jina 兜底
          → 结果回灌，进度气泡逐行更新（搜索词/读取网页/结果摘要，不露 URL）
      正文流式编辑（1.5s 节流，>3400 字自动分段多条消息）
  → 定稿 MarkdownV2（失败回退纯文本）→ tg.remember 写对话缓存（供追问）
```

## 关键设计决策（为什么这么写）

- **时间注入挂在最新一条用户消息末尾**而非系统提示：系统提示与历史轮次保持
  字节不变，命中上游 prompt 缓存（prompt.with_time 的 docstring 有详述）。
- **正文提取在独立进程**：trafilatura/lxml 是 CPU 密集纯 Python 工作，线程会
  占住 GIL 饿死事件循环。子进程内先截断再 send（64KB 管道缓冲写阻塞 vs 父进程
  join 等退出会互等死锁）；父进程 poll+recv 后再收尸。
- **流式三层健壮性**（llm._drain_stream / chat.stream_reply 轮循环）：
  ① 空闲看门狗——已有正文且无半截 tool_call 时 STREAM_IDLE_TIMEOUT 秒无新数据
  视为完成（部分网关不发结束帧）；② 掐流但正文已到手 → 按完成处理入缓存；
  ③ 零输出（含纯空白）掐流 → 静默重试一次；429 配额类错误不重试、独立文案。
- **thought_signature 回传**：Gemini 3.x 思考型模型经兼容端点做 function calling
  时，流式 tool_call 的 extra_content 必须原样回传，否则下轮 400。相应地
  tool_call 无 index 时每 chunk 分配新槽位（该端点整调用单 chunk 发全）。
- **grounding 当调研代理而非搜索框**：工具描述引导主模型一轮提交一个综合任务；
  同轮多个 web_search 强制合并为一次 grounding 调研（每次 grounding 内部本就是
  多跳检索）。GEMINI_API_KEY/GEMINI_BASE_URL 与主模型解耦，支持中转站转发原生格式。
- **进度显示纯文本+缩进**：避免使用可能在部分 Telegram 客户端渲染成彩色图标的特殊符号；不向群成员暴露 URL/域名。
- **SSRF 防护**：open_url 仅允许公网 http(s)，拒绝内网/回环 IP 字面量。

## 配置

全部经环境变量（.env），见 .env.example 逐项注释。多配置档存 profiles/*.env
（完整快照，gitignore），configure.py 菜单交互式新建/切换/删除，切换自动备份
.env 并询问重启容器。

## 测试与部署

- `python tests/smoke_test.py`：14 项行为级断言（假流/假 Telegram 对象），覆盖
  重试/看门狗/取消/分段/TUI/签名回传/合并调研/相册/SSRF。改动后必跑。
- 部署：本地 commit → push → 服务器 `git pull && docker compose up -d --build
  --force-recreate`（compose 偶发不重建容器，--force-recreate 保险），之后
  `docker exec tgrok-bot-1 grep -c <新代码标识> bot.py` 或看启动日志验证。
