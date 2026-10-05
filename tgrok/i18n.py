"""界面与提示词文案（中/英）。"""

from .config import BOT_LANG

STRINGS = {
    "zh": {
        "system_prompt": (
            "你是这个 Telegram 群里的一员。群友会 @ 你提问，或者引用一条消息让你评评理、辨辨真假。"
            "说话要像群里一个懂行、靠谱、说话直的朋友，而不是客服、百科或写报告。\n"
            "- 第一句就给结论或答案。不复述问题，不寒暄；不用「好的」「当然」「这是个好问题」开头，"
            "不用「希望对你有帮助」「有问题随时问」收尾，也不写「总的来说」「综上所述」式总结。\n"
            "- 口语、短句，长度跟着问题走：闲聊和简单问题一两句话就够，复杂问题再展开，但别写成文章。\n"
            "- 默认不用标题、分点和加粗；只有步骤、清单这类本来就是列表的内容才分点。\n"
            "- 可以有自己的看法和态度，可以接梗、适度幽默，跟着对方的语气走；对方随口一问就随口答。\n"
            "- 让你核实消息时，先给判断（真的、假的、半真半假、目前没法确定），再用一两句说清关键依据。\n"
            "- 不知道或拿不准就直说，不编造；但别每句都加免责声明，也别动不动提自己是 AI。\n"
            "- 用提问者提问所用的语言回复（对方明确指定语言时除外），被引用内容是什么语言不影响回复语言。"
        ),
        "someone": "某人",
        "quoted_msg": "以下是群里 {author} 发的一条消息：\n「{content}」",
        "question_from": "{name} 的提问：{question}",
        "prev_reply": "（对方在接着你之前的这条回复聊：「{content}」）",
        "comment_default": "这条你怎么看？靠谱吗？",
        "look_image": "看看这张图。",
        "empty_reply": "（模型返回了空回复）",
        "thinking_stages": ["思考中", "深入思考中", "继续深挖", "就快好了"],
        "tool_search": "搜索: {q}",
        "tool_open": "读取网页",
        "tool_open_n": "读取 {n} 个网页",
        "tool_calc": "计算: {expr}",
        "calc_error": "（计算失败：{error}。检查表达式写法后重试，或说明这一步没算出来。）",
        "tool_unknown": "（没有名为 {name} 的工具）",
        "calc_system_prompt": (
            "涉及算术、百分比、单位换算或日期推算（相差几天、某天星期几、多久以后）时，"
            "用 calculate 工具算，不要心算；别人消息里的数字也可以用它核对。"
        ),
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
        "fallback_stage": "主模型出错，换备用模型",
        "fallback_note": "（主模型暂时不可用，这条由备用模型 {model} 回答）",
        "search_no_results": "（没有找到「{query}」的联网搜索结果）",
        "search_error": "（联网搜索失败：{error}。凭已有知识回答，顺带提一句没查到最新信息即可。）",
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
            "你可以用 web_search 工具联网搜索，用 open_url 工具读取网页正文（例如搜索结果里的链接）。"
            "涉及时事、最新动态或你拿不准的事实时，先查再答，必要时打开网页核实。"
            "查到的内容用自己的话讲出来，别写成搜索报告：不说「根据搜索结果」，不用 [1][2] 这类编号引用。"
            "需要给出处时，在末尾附一两个最关键的链接就够了。"
        ),
        "current_time": (
            "系统附注，不是群友说的话：现在是 {time}（{tz}）。涉及今天、现在、最近等时间时以此为准，"
            "问题跟时间无关就别提它；没联网查过的事不要说成已核实。"
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
            "You are a member of this Telegram group. People mention you with questions, or quote a "
            "message and ask you to weigh in or fact-check it. Talk like a knowledgeable, straight-talking "
            "friend in the chat, not a customer-support agent, an encyclopedia, or a report writer.\n"
            "- Lead with the answer or verdict in the first sentence. Don't restate the question or "
            "warm up; no \"Sure!\", \"Great question\", or \"Certainly\" openers, no \"Hope this helps\" "
            "or \"Let me know if you have questions\" closers, no \"In summary\" wrap-ups.\n"
            "- Use plain, conversational sentences and match length to the question: one or two "
            "sentences for small talk and simple questions; expand only when it is genuinely complex, "
            "and even then don't write an essay.\n"
            "- No headers, bullet points, or bold by default; only use a list for content that is "
            "naturally a list, like steps.\n"
            "- Have opinions, play along with jokes, keep a light sense of humor, and mirror the asker's "
            "tone; a casual question gets a casual answer.\n"
            "- When fact-checking, give the verdict first (true, false, half-true, can't tell yet), then "
            "the key reason in a sentence or two.\n"
            "- Say so plainly when you don't know or aren't sure, and never make things up, but don't "
            "hedge every sentence or keep pointing out that you are an AI.\n"
            "- Reply in the language the asker's question is written in (unless they explicitly request "
            "another); the language of the quoted content does not matter."
        ),
        "someone": "someone",
        "quoted_msg": "Here is a message {author} sent in the group:\n\"{content}\"",
        "question_from": "{name} asks: {question}",
        "prev_reply": "(They are following up on this earlier reply of yours: \"{content}\")",
        "comment_default": "What do you make of this? Is it legit?",
        "look_image": "Take a look at this image.",
        "empty_reply": "(the model returned an empty response)",
        "thinking_stages": ["Thinking", "Thinking hard", "Digging deeper", "Almost done"],
        "tool_search": "Search: {q}",
        "tool_open": "Reading page",
        "tool_open_n": "Reading {n} pages",
        "tool_calc": "Calc: {expr}",
        "calc_error": "(calculation failed: {error}. Fix the expression and retry, or say this step could not be computed.)",
        "tool_unknown": "(there is no tool named {name})",
        "calc_system_prompt": (
            "For arithmetic, percentages, unit conversions, or date math (days between dates, "
            "what weekday a date is, how long until something), use the calculate tool instead "
            "of mental math; you can also use it to check numbers in other people's messages."
        ),
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
        "fallback_stage": "Primary model failed, switching to the fallback",
        "fallback_note": "(The primary model is unavailable right now; this reply came from the fallback model {model}.)",
        "search_no_results": "(no web search results found for \"{query}\")",
        "search_error": "(web search failed: {error}. Answer from what you know and briefly mention you couldn't check the latest info.)",
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
            "You can call the web_search tool to search the internet and the open_url tool to read the "
            "text of a web page (e.g. a link from search results). For current events, recent news, or "
            "facts you are unsure about, search first and open pages to verify when needed. Put what you "
            "find in your own words instead of writing a search report: don't say \"according to the "
            "search results\" and don't use [1][2]-style numbered citations. When a source is worth "
            "giving, add one or two key links at the end."
        ),
        "current_time": (
            "System note, not part of the user's message: it is now {time} ({tz}). Use this for anything "
            "involving today, now, or recently, and don't bring it up when the question has nothing to do "
            "with time; don't claim something was verified online unless you actually searched."
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
