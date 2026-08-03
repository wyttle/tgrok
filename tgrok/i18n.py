"""界面与提示词文案（中/英）。"""

from .config import BOT_LANG

STRINGS = {
    "zh": {
        "system_prompt": (
            "你是 Telegram 群聊里的 AI 助手。群友会 @ 你提问，或引用一条消息让你评论、"
            "核实，请结合上下文直接回答。"
            "用提问者提问所用的语言回复（对方明确指定语言时除外），"
            "被引用内容是什么语言不影响回复语言。"
            "像聊天一样自然作答：先给结论，长度与问题匹配，不要套固定模板，"
            "非必要不用标题和分点，简单问题一两句话即可。"
            "不确定的事情要明确说明，不要编造。"
        ),
        "someone": "某人",
        "quoted_msg": "以下是群里 {author} 发的一条消息：\n「{content}」",
        "question_from": "{name} 的提问：{question}",
        "comment_default": "请评论/核实这条消息。",
        "look_image": "请看这张图片。",
        "empty_reply": "（模型返回了空回复）",
        "thinking_stages": ["思考中", "深入思考中", "继续深挖", "就快好了"],
        "tool_search": "搜索: {q}",
        "tool_open": "读取网页",
        "tool_open_n": "读取 {n} 个网页",
        "res_results": "{n} 条结果",
        "res_chars": "{k} 字",
        "res_failed": "失败",
        "res_fail_suffix": "，{n} 个失败",
        "sources": "来源：",
        "btn_cancel": "取消",
        "cancelled": "已取消",
        "cancelled_suffix": "已取消（以上为部分回复）",
        "cancel_done": "已取消",
        "cancel_denied": "只有提问者或管理员可以取消",
        "cancel_gone": "本次回复已结束",
        "nudge": "请在 @ 我的同时提出问题，或回复某条消息后 @ 我提问～",
        "llm_failed": "调用模型失败，请稍后重试；若持续失败请联系管理员。",
        "llm_quota": "模型配额超限（429），请稍后再试；若持续出现请联系管理员检查额度/账单。",
        "search_no_results": "（没有找到「{query}」的联网搜索结果）",
        "search_error": "（联网搜索失败：{error}。请基于已有知识回答，并说明信息未经联网核实。）",
        "search_bad_args": "（工具调用参数无法解析，请用合法的 JSON 参数重新调用工具）",
        "search_merged": "（本轮多个 web_search 已合并为一次深度调研执行，结果见第一条 web_search 返回）",
        "search_agent_note": (
            "注意：web_search 是深度调研代理，一次调用内部会自动执行多轮 Google 搜索并汇总。"
            "把一轮要查证的内容合并成一个综合调研任务提交，不要拆成多个小查询。"
        ),
        "fetch_bad_url": "（无法读取该地址：仅支持公网 http/https 链接）",
        "fetch_error": "（读取网页失败：{error}。可换一条链接重试，或基于搜索摘要回答。）",
        "fetch_unsupported": "（该链接不是文本网页（{ctype}），无法读取）",
        "fetch_empty": "（该网页没有可提取的正文）",
        "search_system_prompt": (
            "你可以调用 web_search 工具联网搜索实时信息，也可以调用 open_url 工具"
            "读取网页正文（例如搜索结果里的链接）获取细节。"
            "遇到时事、时效性内容或不确定的事实时，先搜索、必要时打开网页核实再回答，"
            "并在答案中附上来源链接。"
        ),
        "current_time": (
            "当前真实时间是 {time}（{tz}），这是系统提供的准确时间，可直接引用。"
            "涉及「今天/现在/最近」等时间时以此为准，不要臆测日期，也不要谎称已核实。"
        ),
        "weekday": ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"],
        "start": (
            "你好！把我拉进群后这样用：\n"
            "1. 回复某条消息并 @ 我提问，例如「@{username} 这是真的吗？」\n"
            "2. 直接 @ 我提问任何问题\n"
            "3. 回复我的消息可以继续追问\n"
            "私聊里直接发消息即可。\n\n"
            "你的用户 ID：{user_id}"
        ),
        "admin_usage": "用法：/adduser <用户ID>（可多个，空格分隔），或在群里回复某人的消息后发送该命令",
        "invalid_id": "「{arg}」不是有效的用户 ID",
        "added": "已添加：{ids}\n当前白名单共 {n} 人",
        "removed": "已移除：{ids}\n当前白名单共 {n} 人",
        "no_match": "（无匹配，名单未变化）",
        "admins": "管理员：{ids}",
        "not_configured": "（未配置）",
        "whitelist": "白名单（{n} 人）：\n{ids}",
        "whitelist_empty_controlled": "白名单为空（受控模式：仅管理员可用）",
        "whitelist_empty_open": "白名单为空（开放模式：所有人可用）",
        "cmd_help": "使用说明",
        "cmd_adduser": "添加白名单用户（ID 或回复某人消息）",
        "cmd_deluser": "移除白名单用户",
        "cmd_listusers": "查看白名单",
    },
    "en": {
        "system_prompt": (
            "You are an AI assistant in a Telegram group chat. Members mention you with "
            "questions or quote a message for you to comment on or fact-check; answer "
            "directly based on the context. Reply in the language the asker's question is "
            "written in (unless they explicitly request another); the language of the quoted "
            "content does not matter. Answer like a natural chat message: conclusion first, "
            "length matched to the question, no boilerplate structure — skip headers and "
            "bullet lists unless they truly help, and one or two sentences is fine for "
            "simple questions. Be explicit about uncertainty and never make things up."
        ),
        "someone": "someone",
        "quoted_msg": "Here is a message {author} sent in the group:\n\"{content}\"",
        "question_from": "{name} asks: {question}",
        "comment_default": "Please comment on / fact-check this message.",
        "look_image": "Please look at this image.",
        "empty_reply": "(the model returned an empty response)",
        "thinking_stages": ["Thinking", "Thinking hard", "Digging deeper", "Almost done"],
        "tool_search": "Search: {q}",
        "tool_open": "Reading page",
        "tool_open_n": "Reading {n} pages",
        "res_results": "{n} results",
        "res_chars": "{k} chars",
        "res_failed": "failed",
        "res_fail_suffix": ", {n} failed",
        "sources": "Sources:",
        "btn_cancel": "Cancel",
        "cancelled": "Cancelled",
        "cancelled_suffix": "Cancelled (partial reply above)",
        "cancel_done": "Cancelled",
        "cancel_denied": "Only the asker or an admin can cancel",
        "cancel_gone": "This reply has already finished",
        "nudge": "Please include a question when mentioning me, or reply to a message and mention me.",
        "llm_failed": "Failed to call the model. Please try again later; contact the admin if it persists.",
        "llm_quota": "Model quota exceeded (429). Please try again later; contact the admin to check quota/billing if it persists.",
        "search_no_results": "(no web search results found for \"{query}\")",
        "search_error": "(web search failed: {error}. Answer from your own knowledge and note it was not verified online.)",
        "search_bad_args": "(could not parse the tool arguments; call the tool again with valid JSON arguments)",
        "search_merged": "(multiple web_search calls this round were merged into one deep-research run; see the first web_search result)",
        "search_agent_note": (
            "Note: web_search is a deep research agent that internally runs multiple Google "
            "searches per call. Submit ONE combined research task per round instead of many narrow queries."
        ),
        "fetch_bad_url": "(cannot fetch this address: only public http/https URLs are supported)",
        "fetch_error": "(failed to fetch the page: {error}. Try another link or answer from the search snippets.)",
        "fetch_unsupported": "(the link is not a text page ({ctype}), cannot read it)",
        "fetch_empty": "(no readable text on that page)",
        "search_system_prompt": (
            "You can call the web_search tool to look up real-time information on the internet, "
            "and the open_url tool to read the text of a web page (e.g. a link from search results) "
            "for details. For current events, time-sensitive topics, or facts you are unsure about, "
            "search first, open pages to verify when needed, then answer and cite source links."
        ),
        "current_time": (
            "The current real-world time is {time} ({tz}). This is accurate time provided by the "
            "system and can be cited directly. Use it for anything involving \"today/now/recently\"; "
            "do not guess the date or claim you have verified it."
        ),
        "weekday": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "start": (
            "Hi! Add me to a group and use me like this:\n"
            "1. Reply to any message and mention me with a question, e.g. \"@{username} is this true?\"\n"
            "2. Mention me directly with any question\n"
            "3. Reply to my messages to follow up\n"
            "In private chat, just send a message.\n\n"
            "Your user ID: {user_id}"
        ),
        "admin_usage": "Usage: /adduser <user ID> (multiple IDs separated by spaces), or reply to someone's message with this command",
        "invalid_id": "\"{arg}\" is not a valid user ID",
        "added": "Added: {ids}\nWhitelist now has {n} user(s)",
        "removed": "Removed: {ids}\nWhitelist now has {n} user(s)",
        "no_match": "(no match, list unchanged)",
        "admins": "Admins: {ids}",
        "not_configured": "(not configured)",
        "whitelist": "Whitelist ({n} user(s)):\n{ids}",
        "whitelist_empty_controlled": "Whitelist is empty (controlled mode: admins only)",
        "whitelist_empty_open": "Whitelist is empty (open mode: everyone can use)",
        "cmd_help": "How to use",
        "cmd_adduser": "Add user to whitelist (ID or reply to a message)",
        "cmd_deluser": "Remove user from whitelist",
        "cmd_listusers": "Show whitelist",
    },
}


def t(key: str, **kwargs) -> str:
    return STRINGS[BOT_LANG][key].format(**kwargs)
