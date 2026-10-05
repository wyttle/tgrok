#!/usr/bin/env python
"""
Interactive configuration wizard / 交互式配置向导:  python configure.py

Asks for every config item and generates/updates the .env file.
  - Existing .env values become defaults (press Enter to keep)
  - Validates the bot token online (optional)
  - Connects to the LLM endpoint and lists available models
  - Input validation for user IDs, numbers, etc.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 下管道/重定向时默认用本地代码页，强制 UTF-8 以正确处理中文
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import httpx
except ImportError:
    httpx = None

TEXT = {
    "zh": {
        "no_httpx": "提示：未安装 httpx，跳过联网验证（在虚拟环境中运行可启用验证）\n",
        "banner_title": "  Telegram 群聊 AI 助手 — 配置向导",
        "banner_sub": "  逐项填写，回车使用默认值/保留现有值",
        "existing": "检测到已有配置 {path}，现有值将作为默认值。\n",
        "keep": "回车保留",
        "required": "必填",
        "optional": "可选，回车跳过",
        "field_required": "  错误：该项必填，请输入。\n",
        "validation_error": "错误：{message}",
        "yes_values": ("y", "yes", "是"),
        "s1": "【1/9】Telegram Bot Token（找 @BotFather 发 /newbot 获取）",
        "token": "Bot Token",
        "token_invalid_fmt": "格式不对，Bot Token 形如 123456789:ABCdefGhI...（从 @BotFather 获取）",
        "token_net_fail": "  警告： 无法连接 Telegram 验证（{err}），跳过在线验证",
        "token_ok": "  成功： Token 有效，bot 用户名：@{username}",
        "token_bad": "  错误： Token 无效（Telegram 返回未授权）",
        "token_use_anyway": "  仍然使用该 Token？",
        "s2a": "【2/9】超级管理员用户 ID（强烈建议填写自己的 ID，可用 @userinfobot 查询）",
        "s2b": "      配置后进入受控模式：仅管理员 + 白名单用户可用，可用 /adduser /deluser 管理",
        "admin_ids": "管理员 ID（多个用逗号分隔）",
        "ids_invalid": "请输入纯数字的用户 ID，多个用逗号分隔，例如：123456789,987654321",
        "s3": "【3/9】白名单初始用户 ID（之后随时可用 /adduser 添加，这里可跳过）",
        "allowed_ids": "白名单 ID（多个用逗号分隔）",
        "s4a": "【4/9】模型后端",
        "s4b": "      1 = OpenAI 兼容接口（中转站 / LM Studio / vLLM / OpenAI 官方等，需填接口地址）\n      2 = Gemini 官方（Google AI Studio，key 在 https://aistudio.google.com/apikey 免费申请）\n      3 = Claude 原生（Anthropic 协议 /v1/messages，原生 thinking；中转站需支持转发）",
        "backend_pick": "后端类型：1=OpenAI 兼容  2=Gemini 官方  3=Claude 原生",
        "backend_invalid": "请输入 1、2 或 3",
        "claude_key": "Claude API Key（官方为 Anthropic key；走中转站则为中转站 key）",
        "claude_route_pick": "Claude 接入：1=Anthropic 官方直连  2=中转站（转发原生格式）",
        "gemini_key": "Gemini API Key（官方为 AI Studio key；走中转站则为中转站 key）",
        "gemini_route_pick": "Gemini 接入：1=Google 官方直连  2=中转站（转发原生格式）",
        "gemini_route_invalid": "请输入 1 或 2",
        "relay_addr": "中转站根地址（如 https://xxx.com）",
        "note_relay_compat": "  → 原生/grounding 走该地址，回复走 {url}（中转站 OpenAI 兼容路径）",
        "base_url": "接口地址",
        "api_key": "API Key（本地服务一般随便填）",
        "ua": "自定义 User-Agent（部分云端网关会校验 UA，可选）",
        "s5": "【5/9】模型名称",
        "models_fail": "  警告： 无法连接 {url} 获取模型列表（{err}），请手动输入",
        "models_found": "  成功： 检测到以下可用模型：",
        "model_pick": "输入序号选择，或直接输入模型名",
        "model_name": "模型名称",
        "s6a": "【6/9】图片理解（多模态）",
        "s6b": "      模型支持视觉输入时开启：群友发图或回复图片提问，图片会发给模型一起分析",
        "vision_ask": "  开启图片理解？",
        "s_search_a": "【7/9】联网搜索（web_search + open_url 工具）",
        "s_search_b": "      模型可自主联网搜索、并打开搜索结果网页读取正文（需模型支持 function calling）：\n      tavily 需免费 API key（tavily.com），duckduckgo 零配置，searxng 需自建实例，\n      serper 为真实 Google 结果（serper.dev 免费 2500 次）\n      可同时选多个源（逗号分隔），并发聚合、去重合并搜索结果",
        "search_pick": "搜索源：0=关闭  1=tavily  2=duckduckgo  3=searxng  4=serper（可多选，如 1,4）",
        "search_invalid": "请输入 0-4 或源名称（tavily/duckduckgo/searxng/serper），多个用逗号分隔；0 只能单独使用",
        "tavily_key": "Tavily API Key",
        "serper_key": "Serper API Key（serper.dev）",
        "s_gmode_a": "【Gemini】搜索方式",
        "s_gmode_b": "      1 = 原生：回复模型自带 google_search（注意：3.5 系列免费档无 grounding 配额）\n      2 = 混合：搜索由指定 grounding 模型执行（如 gemini-2.5-flash，免费档可用），回复用所选模型\n      3 = bot 自带搜索源（tavily / serper / duckduckgo…）",
        "gmode_pick": "搜索方式：1=原生  2=混合  3=自带搜索源",
        "gmode_invalid": "请输入 1、2 或 3",
        "gsearch_model": "grounding 搜索模型",
        "s_genhance": "      —— Gemini grounding 增强：web_search 改由 Gemini 模型 + Google 官方搜索执行（需 AI Studio key，\n      免费申请），结果更准；失败时自动回退上面配置的搜索源",
        "genhance_ask": "  启用 Gemini grounding 增强搜索？",
        "gmode_skip_search": "（搜索由 Gemini 执行，跳过 bot 自带搜索源配置；原有搜索配置保留作为回退）",
        "searxng_url": "SearXNG 实例地址（如 http://localhost:8080）",
        "jina_ask": "  网页直接读取失败（反爬/JS 页面）时走 Jina Reader（r.jina.ai）兜底？",
        "jina_key": "Jina API Key（可选，提高速率限制，jina.ai 免费申请）",
        "s7": "【8/9】生成参数与时区",
        "max_tokens": "单次回答最大 token 数",
        "temperature": "采样温度 temperature（0~2，越高越活泼；留空=后端默认，输入 - 清除已设值，模型不支持时自动忽略）",
        "top_p": "核采样 top_p（0~1，留空=后端默认，输入 - 清除已设值）",
        "float_invalid": "请输入数字（如 0.9），或输入 - 清除",
        "max_tokens_reasoning": "  推理模型的思考过程也计入 token 上限，建议至少 4096，默认值已调高",
        "effort_intro": "检测到 {model} 是推理型 GPT 模型，选择推理强度（越高越慢、越贵；群聊建议 low）：",
        "effort_desc": {
            "none": "不推理，最快（部分模型不支持，如 gpt-6.1-sol）",
            "minimal": "极少推理（部分模型不支持）",
            "low": "快，日常聊天推荐",
            "medium": "多数模型的默认值",
            "high": "更仔细，明显变慢",
            "xhigh": "很慢，适合难题",
            "max": "最慢最贵",
        },
        "effort_pick": "推理强度（填序号，或直接输入其它值；输入 - 表示不设置、用后端默认）",
        "effort_invalid": "请输入列表里的序号、推理强度名称（如 low），或输入 -",
        "sampling_skipped": "  推理模型不接受 temperature/top_p，已跳过并清空这两项",
        "extra_params": "其它额外请求参数（高级，可留空）：写成 键=值，多个用逗号分隔，嵌套键用点号，如 thinking.type=enabled, thinking.budget_tokens=1000；输入 - 清除",
        "extra_invalid": "格式应为 键=值（多个用逗号分隔，如 a=1, b.c=low），或输入 - 清除",
        "extra_old_invalid": "  原 LLM_EXTRA_BODY 无法解析，已忽略：{raw}",
        "max_history": "多轮对话保留消息条数",
        "tz": "时区（IANA 名称，用于告知模型当前真实时间；无法识别时 bot 会回退 UTC）",
        "int_invalid": "请输入正整数",
        "s8": "【9/9】系统提示词（定义 bot 的角色和语气，跳过则使用内置默认值）",
        "sys_prompt": "系统提示词",
        "multiline_hint": "  直接回车保留原值，输入 - 清除；也可以粘贴多行内容，粘贴完后单独一行输入 . 结束",
        "api_pick": "接口类型：1 = Chat Completions（/chat/completions，兼容性最好）  2 = Responses（/responses，GPT-6.1 Sol 等新模型调用工具需要）",
        "summary": "配置汇总：",
        "write_confirm": "确认写入 {path}？",
        "cancelled": "已取消，未写入任何文件。",
        "header": "# 由 configure.py 生成，重新运行该脚本可修改配置",
        "written": "\n成功： 已写入 {path}",
        "next": "启动 bot：python bot.py（或 docker compose up -d --build）",
        "menu_title": "当前生效配置：{active}",
        "menu_no_profile": "（未匹配任何配置档）",
        "menu_body": "  1. 编辑当前配置（向导）\n  2. 切换配置档\n  3. 新建/编辑配置档（向导）\n  4. 删除配置档\n  0. 退出",
        "p_list_header": "配置档（* = 当前生效）：",
        "p_none": "（还没有配置档，可用菜单 3 新建）",
        "p_pick_use": "选择要启用的配置档（序号或名称，回车取消）",
        "p_pick_del": "选择要删除的配置档（序号或名称，回车取消）",
        "p_missing": "没有名为「{name}」的配置档",
        "p_name_ask": "配置档名称（字母/数字/下划线/短横线，如 relay、gemini）",
        "p_bad_name": "名称不合法，只能用字母、数字、下划线、短横线（1-32 字符）",
        "p_applied": "成功： 已启用配置档：{name}",
        "p_synced": "成功： 修改已同步到配置档：{name}",
        "p_backup": "  原 .env 已备份为 {path}",
        "p_use_now": "立即启用该配置档？",
        "p_del_confirm": "确认删除配置档「{name}」？",
        "p_deleted": "成功： 已删除：{name}",
        "p_restart_ask": "重启 Docker 容器使配置生效？",
        "p_restarting": "重启容器…",
        "p_restart_done": "成功： 容器已重启",
        "p_restart_fail": "警告： 重启命令返回错误，请手动检查（docker compose up -d）",
        "p_restart_hint": "提示：配置需重启后生效（docker compose up -d 或重启 python bot.py）",
    },
    "en": {
        "no_httpx": "Note: httpx not installed, skipping online validation (run inside the venv to enable it)\n",
        "banner_title": "  Telegram Group AI Assistant — Setup Wizard",
        "banner_sub": "  Answer each item; press Enter to accept the default/current value",
        "existing": "Found existing config {path}; current values will be used as defaults.\n",
        "keep": "Enter to keep",
        "required": "required",
        "optional": "optional, Enter to skip",
        "field_required": "  Error: This field is required.\n",
        "validation_error": "Error: {message}",
        "yes_values": ("y", "yes"),
        "s1": "[1/9] Telegram Bot Token (get one from @BotFather with /newbot)",
        "token": "Bot Token",
        "token_invalid_fmt": "Invalid format. A bot token looks like 123456789:ABCdefGhI... (from @BotFather)",
        "token_net_fail": "  Warning: Could not reach Telegram to validate ({err}); skipping online check",
        "token_ok": "  Success: Token is valid, bot username: @{username}",
        "token_bad": "  Error: Invalid token (Telegram returned unauthorized)",
        "token_use_anyway": "  Use this token anyway?",
        "s2a": "[2/9] Super admin user IDs (strongly recommended — use @userinfobot to find yours)",
        "s2b": "      With admins set, the bot is in controlled mode: only admins + whitelisted users; manage with /adduser /deluser",
        "admin_ids": "Admin IDs (comma-separated)",
        "ids_invalid": "Please enter numeric user IDs, comma-separated, e.g. 123456789,987654321",
        "s3": "[3/9] Initial whitelist user IDs (you can always /adduser later; OK to skip)",
        "allowed_ids": "Whitelist IDs (comma-separated)",
        "s4a": "[4/9] Model backend",
        "s4b": "      1 = OpenAI-compatible endpoint (relay / LM Studio / vLLM / official OpenAI; needs an endpoint URL)\n      2 = Official Gemini (Google AI Studio; get a free key at https://aistudio.google.com/apikey)\n      3 = Native Claude (Anthropic protocol /v1/messages, real thinking; relay must forward it)",
        "backend_pick": "Backend: 1=OpenAI-compatible  2=Official Gemini  3=Native Claude",
        "backend_invalid": "Enter 1, 2 or 3",
        "claude_key": "Claude API key (Anthropic key for official; relay key when routed through a relay)",
        "claude_route_pick": "Claude routing: 1=official Anthropic  2=relay (forwards native format)",
        "gemini_key": "Gemini API key (AI Studio key for official; relay key when routed through a relay)",
        "gemini_route_pick": "Gemini routing: 1=official Google  2=relay (forwards native format)",
        "gemini_route_invalid": "Enter 1 or 2",
        "relay_addr": "Relay root URL (e.g. https://xxx.com)",
        "note_relay_compat": "  -> native/grounding use that URL; replies go through {url} (relay's OpenAI-compatible path)",
        "base_url": "Endpoint URL",
        "api_key": "API key (anything works for most local servers)",
        "ua": "Custom User-Agent (some cloud gateways validate it; optional)",
        "s5": "[5/9] Model name",
        "models_fail": "  Warning: Could not fetch model list from {url} ({err}); please type it manually",
        "models_found": "  Success: Available models detected:",
        "model_pick": "Pick a number, or type a model name",
        "model_name": "Model name",
        "s6a": "[6/9] Image understanding (multimodal)",
        "s6b": "      Enable if the model supports vision: images sent or quoted in chat are passed to the model",
        "vision_ask": "  Enable image understanding?",
        "s_search_a": "[7/9] Web search (web_search + open_url tools)",
        "s_search_b": "      Lets the model search the internet and open result pages to read their text (requires function calling support):\n      tavily needs a free API key (tavily.com), duckduckgo is zero-config, searxng needs a self-hosted instance,\n      serper returns real Google results (serper.dev, 2500 free queries)\n      Multiple providers can be combined (comma-separated); results are fetched concurrently and merged",
        "search_pick": "Provider: 0=off  1=tavily  2=duckduckgo  3=searxng  4=serper (combine with commas, e.g. 1,4)",
        "search_invalid": "Enter 0-4 or provider names (tavily/duckduckgo/searxng/serper), comma-separated; 0 must be used alone",
        "tavily_key": "Tavily API key",
        "serper_key": "Serper API key (serper.dev)",
        "s_gmode_a": "[Gemini] Search mode",
        "s_gmode_b": "      1 = Native: the reply model uses built-in google_search (note: no free-tier grounding quota on the 3.5 family)\n      2 = Hybrid: searches run on a dedicated grounding model (e.g. gemini-2.5-flash, free tier OK), replies use your chosen model\n      3 = Bot's own search providers (tavily / serper / duckduckgo…)",
        "gmode_pick": "Search mode: 1=native  2=hybrid  3=own providers",
        "gmode_invalid": "Enter 1, 2 or 3",
        "gsearch_model": "Grounding search model",
        "s_genhance": "      -- Gemini grounding boost: web_search runs on a Gemini model + official Google Search (needs a free\n      AI Studio key); more accurate results, automatically falls back to the providers above on failure",
        "genhance_ask": "  Enable Gemini grounding for search?",
        "gmode_skip_search": "(Searches are handled by Gemini; skipping the bot's own provider setup — existing search settings are kept as fallback)",
        "searxng_url": "SearXNG instance URL (e.g. http://localhost:8080)",
        "jina_ask": "  Fall back to Jina Reader (r.jina.ai) when direct page fetch fails (anti-bot/JS pages)?",
        "jina_key": "Jina API key (optional, higher rate limits, free at jina.ai)",
        "s7": "[8/9] Generation parameters & timezone",
        "max_tokens": "Max tokens per reply",
        "temperature": "Sampling temperature (0-2, higher = livelier; empty = backend default, enter - to clear, auto-ignored if unsupported)",
        "top_p": "Nucleus sampling top_p (0-1, empty = backend default, enter - to clear)",
        "float_invalid": "Enter a number (e.g. 0.9), or - to clear",
        "max_tokens_reasoning": "  Reasoning tokens count toward the limit; at least 4096 is recommended, so the default was raised",
        "effort_intro": "{model} is a reasoning GPT model. Pick a reasoning effort (higher = slower and pricier; low suits group chat):",
        "effort_desc": {
            "none": "no reasoning, fastest (unsupported by some models, e.g. gpt-6.1-sol)",
            "minimal": "very little reasoning (unsupported by some models)",
            "low": "fast, recommended for chat",
            "medium": "the default on most models",
            "high": "more careful, noticeably slower",
            "xhigh": "very slow, for hard problems",
            "max": "slowest and most expensive",
        },
        "effort_pick": "Reasoning effort (enter a number or type another value; - = don't set, use the backend default)",
        "effort_invalid": "Enter a number from the list, an effort name (e.g. low), or -",
        "sampling_skipped": "  Reasoning models don't accept temperature/top_p; skipped and cleared",
        "extra_params": "Other extra request params (advanced, optional): key=value, comma-separated, dots for nested keys, e.g. thinking.type=enabled, thinking.budget_tokens=1000; enter - to clear",
        "extra_invalid": "Use key=value (comma-separated, e.g. a=1, b.c=low), or enter - to clear",
        "extra_old_invalid": "  Could not parse the existing LLM_EXTRA_BODY, ignoring it: {raw}",
        "max_history": "Messages kept per conversation",
        "tz": "Timezone (IANA name, used to tell the model the current real time; falls back to UTC if unrecognized)",
        "int_invalid": "Please enter a positive integer",
        "s8": "[9/9] System prompt (defines the bot's role and tone; skip for the built-in default)",
        "sys_prompt": "System prompt",
        "multiline_hint": "  Press Enter to keep, type - to clear, or paste multiple lines and finish with a line containing only .",
        "api_pick": "API type: 1 = Chat Completions (/chat/completions, widest support)  2 = Responses (/responses, required for tool calling on GPT-6.1 Sol and similar models)",
        "summary": "Configuration summary:",
        "write_confirm": "Write to {path}?",
        "cancelled": "Cancelled. Nothing was written.",
        "header": "# Generated by configure.py; re-run the script to change settings",
        "written": "\nSuccess: Written to {path}",
        "next": "Start the bot: python bot.py (or docker compose up -d --build)",
        "menu_title": "Active configuration: {active}",
        "menu_no_profile": "(does not match any profile)",
        "menu_body": "  1. Edit current config (wizard)\n  2. Switch profile\n  3. Create/edit profile (wizard)\n  4. Delete profile\n  0. Exit",
        "p_list_header": "Profiles (* = active):",
        "p_none": "(no profiles yet; use menu option 3 to create one)",
        "p_pick_use": "Pick a profile to activate (number or name, Enter to cancel)",
        "p_pick_del": "Pick a profile to delete (number or name, Enter to cancel)",
        "p_missing": "No profile named \"{name}\"",
        "p_name_ask": "Profile name (letters/digits/underscore/dash, e.g. relay, gemini)",
        "p_bad_name": "Invalid name: letters, digits, underscore, dash only (1-32 chars)",
        "p_applied": "Success: Activated profile: {name}",
        "p_synced": "Success: Changes synced to profile: {name}",
        "p_backup": "  Previous .env backed up as {path}",
        "p_use_now": "Activate this profile now?",
        "p_del_confirm": "Delete profile \"{name}\"?",
        "p_deleted": "Success: Deleted: {name}",
        "p_restart_ask": "Restart the Docker container to apply?",
        "p_restarting": "Restarting container…",
        "p_restart_done": "Success: Container restarted",
        "p_restart_fail": "Warning: Restart command failed; please check manually (docker compose up -d)",
        "p_restart_hint": "Note: takes effect after restart (docker compose up -d, or restart python bot.py)",
    },
}

T = TEXT["zh"]  # set after language selection


_DQ_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}


def parse_env(text: str) -> dict:
    """解析 .env 文本，语义与 python-dotenv / Docker Compose 一致的子集：
    单引号值为字面量、双引号值支持 \\n \\t \\" \\\\ 转义，两种引号都可以跨行。
    逐行解析会把多行提示词截成第一行，所以这里按字符扫描引号的闭合位置。
    """
    values, i, n = {}, 0, len(text)
    while i < n:
        end = text.find("\n", i)
        end = n if end == -1 else end
        line = text[i:end]
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            i = end + 1
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        rest_start = i + len(line) - len(rest)
        j = rest_start
        while j < n and text[j] in " \t":
            j += 1
        quote = text[j] if j < n and text[j] in "'\"" else ""
        if not quote:
            values[key] = rest.strip()
            i = end + 1
            continue
        buf, j = [], j + 1
        while j < n and text[j] != quote:
            if quote == '"' and text[j] == "\\" and j + 1 < n:
                nxt = text[j + 1]
                buf.append(_DQ_ESCAPES.get(nxt, "\\" + nxt))
                j += 2
                continue
            buf.append(text[j])
            j += 1
        values[key] = "".join(buf)
        # 跳过闭合引号所在行的剩余部分（行内注释等）
        nl = text.find("\n", j)
        i = n if nl == -1 else nl + 1
    return values


def load_existing(path: Path) -> dict:
    if not path.exists():
        return {}
    return parse_env(path.read_text(encoding="utf-8"))


def ask(label: str, default: str = "", required: bool = False, validate=None, secret: bool = False) -> str:
    while True:
        if default:
            shown = (default[:8] + "…" + default[-4:]) if secret and len(default) > 16 else default
            hint = f"[{T['keep']}: {shown}]"
        else:
            hint = f"[{T['required']}]" if required else f"[{T['optional']}]"
        raw = input(f"{label} {hint}\n> ").strip()
        if not raw:
            if default:
                return default
            if not required:
                return ""
            print(T["field_required"])
            continue
        if validate:
            ok, msg = validate(raw)
            if not ok:
                print(f"  {T['validation_error'].format(message=msg)}\n")
                continue
        return raw


def preview(val: str, limit: int = 80) -> str:
    """单行显示：短的单行值原样返回，多行或过长的值显示首行开头 + 总字数。"""
    text = val.strip()
    first, sep, _ = text.partition("\n")
    if not sep and len(first) <= limit:
        return text
    return f"{first[:limit]}…（{len(val)}）"


def ask_multiline(label: str, default: str = "") -> str:
    """多行输入：直接回车保留原值，输入 - 清除；否则可粘贴多行，单独一行输入 . 结束。
    input() 一次只读一行，直接粘贴多行文本会被截成第一行、剩余行还会灌进后面的问题。
    """
    hint = f"[{T['keep']}: {preview(default)}]" if default else f"[{T['optional']}]"
    print(f"{label} {hint}")
    print(T["multiline_hint"])
    try:
        first = input("> ")
    except EOFError:
        return default
    if not first.strip():
        return default
    if first.strip() == "-":
        return ""
    if first.strip() == ".":
        return default
    lines = [first]
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == ".":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def confirm(prompt: str, default_yes: bool = False) -> bool:
    suffix = "(Y/n)" if default_yes else "(y/N)"
    raw = input(f"{prompt} {suffix}: ").strip().lower()
    if not raw:
        return default_yes
    return raw in T["yes_values"]


def validate_token(raw: str):
    if re.match(r"^\d+:[\w-]{30,}$", raw):
        return True, ""
    return False, T["token_invalid_fmt"]


def validate_ids(raw: str):
    parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    if all(p.isdigit() for p in parts):
        return True, ""
    return False, T["ids_invalid"]


def validate_int(raw: str):
    if raw.isdigit() and int(raw) > 0:
        return True, ""
    return False, T["int_invalid"]


def validate_opt_float(raw: str):
    # "-" 表示清除已设值（默认值非空时按回车是"保留"，需要显式清除手段）
    if raw == "-":
        return True, ""
    try:
        float(raw)
        return True, ""
    except ValueError:
        return False, T["float_invalid"]


# 推理强度候选（按从快到慢排列）；不同模型支持的子集不同，后端拒绝时 bot 会自动去掉该参数
REASONING_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
_EFFORT_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
# 两种协议各自的推理强度写法；切换协议或重选强度时先把两种都清掉
_REASONING_KEYS = ("reasoning", "reasoning_effort")


def is_reasoning_gpt(model: str) -> bool:
    """o 系列与 GPT-5 及以后的模型支持 reasoning effort（兼容 openai/gpt-6.1-sol 这类带前缀的名字）。"""
    name = model.strip().lower().rsplit("/", 1)[-1]
    if re.match(r"o\d", name):
        return True
    m = re.match(r"gpt-(\d+)", name)
    return bool(m) and int(m.group(1)) >= 5


def current_effort(extra: dict) -> str:
    reasoning = extra.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        return reasoning["effort"]
    effort = extra.get("reasoning_effort")
    return effort if isinstance(effort, str) else ""


def effort_body(effort: str, protocol: str) -> dict:
    """Responses 用 reasoning.effort，Chat Completions 用顶层 reasoning_effort。"""
    if not effort:
        return {}
    return {"reasoning": {"effort": effort}} if protocol == "responses" else {"reasoning_effort": effort}


def validate_effort(raw: str):
    raw = raw.strip().lower()
    if raw == "-" or _EFFORT_RE.match(raw) or (raw.isdigit() and 1 <= int(raw) <= len(REASONING_EFFORTS)):
        return True, ""
    return False, T["effort_invalid"]


def _scalar(raw: str):
    raw = raw.strip()
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low == "null":
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?(\d+\.\d*|\.\d+|\d+(\.\d*)?[eE][-+]?\d+)", raw):
        return float(raw)
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def parse_extra_params(raw: str) -> dict | None:
    """额外请求参数：键=值，多个用逗号分隔，嵌套键用点号（thinking.budget_tokens=1000）。
    也接受 JSON 对象原文。无法解析时返回 None。"""
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    out: dict = {}
    for part in re.split(r"[,，;；]+", raw):
        part = part.strip()
        if not part:
            continue
        key, sep, val = part.partition("=")
        keys = [k.strip() for k in key.split(".")]
        if not sep or not all(keys):
            return None
        node = out
        for k in keys[:-1]:
            node = node.setdefault(k, {})
            if not isinstance(node, dict):
                return None
        node[keys[-1]] = _scalar(val)
    return out


def format_extra_params(extra: dict) -> str:
    """dict → 键=值 文本（向导里展示和作为默认值）；含列表等无法扁平表示的值时回退 JSON。"""
    pairs = []

    def walk(prefix: str, node) -> bool:
        for k, v in node.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict) and v:
                if not walk(key, v):
                    return False
            elif isinstance(v, (str, int, float, bool)) or v is None:
                text = json.dumps(v) if isinstance(v, bool) or v is None else str(v)
                if isinstance(v, str) and ("," in v or "=" in v or v != v.strip()):
                    return False
                pairs.append(f"{key}={text}")
            else:
                return False
        return True

    if walk("", extra):
        return ", ".join(pairs)
    return json.dumps(extra, ensure_ascii=False)


def validate_extra_params(raw: str):
    if raw.strip() == "-" or parse_extra_params(raw) is not None:
        return True, ""
    return False, T["extra_invalid"]


def check_telegram_token(token: str) -> str | None:
    resp = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
    data = resp.json()
    if data.get("ok"):
        return data["result"]["username"]
    return None


def list_models(base_url: str, api_key: str, user_agent: str = "") -> list[str]:
    headers = {"Authorization": f"Bearer {api_key}"}
    if user_agent:
        headers["User-Agent"] = user_agent
    resp = httpx.get(
        base_url.rstrip("/") + "/models",
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("data", [])]


def env_line(key: str, val: str) -> str:
    """写成单行：需要时加双引号，换行等控制字符转成 \\n 转义。
    python-dotenv 和 Docker Compose 都会把双引号里的 \\n 还原成换行，
    多行提示词因此不会把 .env 拆成多行、被逐行解析的工具截断。
    """
    if any(c in val for c in (" ", "#", '"', "\\", "\n", "\r", "\t")):
        escaped = (val.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
        val = f'"{escaped}"'
    return f"{key}={val}"


def choose_language(old: dict) -> str:
    default = old.get("BOT_LANG", "zh")
    print("Language / 语言:  [1] 中文   [2] English")
    raw = input(f"> [{'1' if default == 'zh' else '2'}]: ").strip()
    if raw == "2":
        return "en"
    if raw == "1":
        return "zh"
    return default if default in ("zh", "en") else "zh"


PROFILES_DIR = Path(__file__).with_name("profiles")
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def list_profiles() -> list[str]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.env"))


def active_profile(env_path: Path) -> str | None:
    """当前 .env 内容与哪个配置档一致（按解析后的键值对比较）。"""
    cur = load_existing(env_path)
    if not cur:
        return None
    for name in list_profiles():
        if load_existing(PROFILES_DIR / f"{name}.env") == cur:
            return name
    return None


def print_profiles(env_path: Path) -> list[str]:
    names = list_profiles()
    if not names:
        print(T["p_none"])
        return names
    act = active_profile(env_path)
    print(T["p_list_header"])
    for i, name in enumerate(names, 1):
        cfg = load_existing(PROFILES_DIR / f"{name}.env")
        host = re.sub(r"^https?://", "", cfg.get("LLM_BASE_URL", "")).split("/")[0] or "?"
        mark = "*" if name == act else " "
        print(f" {mark} {i}. {name}  ({cfg.get('LLM_MODEL', '?')} @ {host})")
    return names


def pick_profile(env_path: Path, prompt: str) -> str | None:
    names = print_profiles(env_path)
    if not names:
        return None
    raw = input(f"{prompt}\n> ").strip()
    if not raw:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(names):
        return names[int(raw) - 1]
    if raw in names:
        return raw
    print(T["p_missing"].format(name=raw))
    return None


def offer_restart() -> None:
    """交互询问是否重启 Docker 容器；环境不具备时给出手动提示。"""
    compose = Path(__file__).with_name("docker-compose.yml")
    if compose.exists() and shutil.which("docker"):
        if confirm(T["p_restart_ask"], default_yes=True):
            print(T["p_restarting"])
            r = subprocess.run(
                ["docker", "compose", "up", "-d", "--build", "--force-recreate"],
                cwd=str(compose.parent),
            )
            print(T["p_restart_done"] if r.returncode == 0 else T["p_restart_fail"])
            return
    print(T["p_restart_hint"])


def activate_profile(env_path: Path, name: str) -> None:
    src = PROFILES_DIR / f"{name}.env"
    if env_path.exists():
        backup = env_path.with_name(env_path.name + ".bak-switch")
        backup.write_bytes(env_path.read_bytes())
        print(T["p_backup"].format(path=backup.name))
    env_path.write_bytes(src.read_bytes())
    print(T["p_applied"].format(name=name))
    offer_restart()


def sync_profile(env_path: Path, name: str | None) -> None:
    """编辑当前 .env 后写回它对应的配置档，否则切换配置档时修改会被旧档覆盖丢失。"""
    if not name or not env_path.exists():
        return
    ppath = PROFILES_DIR / f"{name}.env"
    if ppath.exists() and ppath.read_bytes() != env_path.read_bytes():
        ppath.write_bytes(env_path.read_bytes())
        print(T["p_synced"].format(name=name))


def run_wizard(env_path: Path, old: dict, can_check: bool, lang: str, is_profile: bool = False) -> None:
    print("=" * 52)
    print(T["banner_title"])
    print(T["banner_sub"])
    print("=" * 52 + "\n")
    if old:
        print(T["existing"].format(path=env_path))

    cfg = {"BOT_LANG": lang}

    # ---- 1. Bot Token ----
    print(T["s1"])
    while True:
        token = ask(T["token"], default=old.get("TELEGRAM_BOT_TOKEN", ""), required=True,
                    validate=validate_token, secret=True)
        if not can_check:
            break
        try:
            username = check_telegram_token(token)
        except Exception as e:
            print(T["token_net_fail"].format(err=type(e).__name__))
            break
        if username:
            print(T["token_ok"].format(username=username))
            break
        print(T["token_bad"])
        if confirm(T["token_use_anyway"]):
            break
    cfg["TELEGRAM_BOT_TOKEN"] = token
    print()

    # ---- 2. Admins ----
    print(T["s2a"])
    print(T["s2b"])
    cfg["ADMIN_USER_IDS"] = ask(T["admin_ids"], default=old.get("ADMIN_USER_IDS", ""), validate=validate_ids)
    print()

    # ---- 3. Whitelist seed ----
    print(T["s3"])
    cfg["ALLOWED_USER_IDS"] = ask(T["allowed_ids"], default=old.get("ALLOWED_USER_IDS", ""), validate=validate_ids)
    print()

    # ---- 4. Backend ----
    print(T["s4a"])
    print(T["s4b"])
    gemini_base = "https://generativelanguage.googleapis.com/v1beta/openai"

    def validate_backend(raw: str):
        return (True, "") if raw.strip() in ("1", "2", "3") else (False, T["backend_invalid"])

    def _truthy(v: str) -> bool:
        return v.strip().lower() in ("1", "true", "yes", "on")

    # 现有配置的协议（兼容布尔开关时代的旧键）
    old_protocol = old.get("LLM_PROTOCOL", "").strip().lower()
    if not old_protocol:
        if _truthy(old.get("CLAUDE_NATIVE", "")):
            old_protocol = "claude"
        elif _truthy(old.get("GEMINI_NATIVE_SEARCH", "")):
            old_protocol = "gemini"
    if old_protocol == "claude":
        backend_default = "3"
    elif old_protocol == "gemini" or "generativelanguage.googleapis.com" in old.get("LLM_BASE_URL", ""):
        backend_default = "2"
    else:
        backend_default = "1"
    backend = ask(T["backend_pick"], default=backend_default, validate=validate_backend).strip()

    def validate_route(raw: str):
        return (True, "") if raw.strip() in ("1", "2") else (False, T["gemini_route_invalid"])

    # 协议由 LLM_PROTOCOL 单键指定（openai 为默认、不写入）；布尔开关时代的旧键一并清掉
    cfg["LLM_PROTOCOL"] = ""
    cfg["CLAUDE_NATIVE"] = ""
    cfg["CLAUDE_BASE_URL"] = ""
    cfg["GEMINI_NATIVE_SEARCH"] = ""
    if backend == "3":
        # Claude 原生：官方直连或走支持 /v1/messages 转发的中转站；地址统一用
        # LLM_BASE_URL（留空 = Anthropic 官方）；UA 无意义，置空
        old_base = old.get("LLM_BASE_URL", "")
        route_default = "2" if old_protocol == "claude" and old_base else "1"
        route = ask(T["claude_route_pick"], default=route_default, validate=validate_route).strip()
        if route == "2":
            cfg["LLM_BASE_URL"] = ask(T["relay_addr"], default=old_base,
                                      required=True).strip().rstrip("/")
        else:
            cfg["LLM_BASE_URL"] = ""
        cfg["LLM_PROTOCOL"] = "claude"
        cfg["LLM_API_KEY"] = ask(T["claude_key"], default=old.get("LLM_API_KEY", ""),
                                 required=True, secret=True)
        cfg["LLM_USER_AGENT"] = ""
        base_url = ""  # 跳过 OpenAI /models 探测（Anthropic 协议不兼容该接口）
    elif backend == "2":
        # Gemini：官方直连或走支持原生格式转发的中转站；UA 无意义，置空
        route_default = "2" if old.get("GEMINI_BASE_URL", "").strip() else "1"
        route = ask(T["gemini_route_pick"], default=route_default, validate=validate_route).strip()
        if route == "2":
            addr = ask(T["relay_addr"], default=old.get("GEMINI_BASE_URL", ""),
                       required=True).strip().rstrip("/")
            cfg["GEMINI_BASE_URL"] = addr  # 原生模式与 grounding 走中转站根地址
            base_url = addr + "/v1"  # 回复走中转站的 OpenAI 兼容路径
            print(T["note_relay_compat"].format(url=base_url))
        else:
            cfg["GEMINI_BASE_URL"] = ""
            base_url = gemini_base
        cfg["LLM_BASE_URL"] = base_url
        cfg["LLM_API_KEY"] = ask(T["gemini_key"], default=old.get("LLM_API_KEY", ""),
                                 required=True, secret=True)
        cfg["LLM_USER_AGENT"] = ""
    else:
        base_url = ask(T["base_url"], default=old.get("LLM_BASE_URL", "http://localhost:1234/v1"), required=True)
        cfg["LLM_BASE_URL"] = base_url
        cfg["LLM_API_KEY"] = ask(T["api_key"], default=old.get("LLM_API_KEY", "not-needed"))
        cfg["LLM_USER_AGENT"] = ask(T["ua"], default=old.get("LLM_USER_AGENT", ""))
    print()

    # ---- 5. Model ----
    print(T["s5"])
    # 换后端时，旧的异族模型名不再是合理默认值
    model_default = old.get("LLM_MODEL", "")
    if backend == "2" and not model_default.lower().startswith("gemini"):
        model_default = "gemini-2.5-flash"
    if backend == "3" and not model_default.lower().startswith("claude"):
        model_default = "claude-opus-4-8"
    model = ""
    if can_check and base_url:
        try:
            models = list_models(base_url, cfg["LLM_API_KEY"], cfg["LLM_USER_AGENT"])
        except Exception as e:
            models = []
            print(T["models_fail"].format(url=base_url, err=type(e).__name__))
        if models:
            print(T["models_found"])
            for i, m in enumerate(models, 1):
                print(f"    {i}. {m}")
            raw = ask(T["model_pick"], default=model_default or models[0], required=True)
            model = models[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(models) else raw
    if not model:
        model = ask(T["model_name"], default=model_default or "local-model", required=True)
    cfg["LLM_MODEL"] = model
    if backend == "1" and not model.lower().startswith("gemini"):
        # OpenAI 兼容后端可选 Responses 接口：GPT-6.1 Sol 等模型只在 /responses 上支持工具调用
        api_default = "2" if old_protocol == "responses" or model.lower().startswith("gpt-6") else "1"
        api = ask(T["api_pick"], default=api_default, validate=validate_route).strip()
        cfg["LLM_PROTOCOL"] = "responses" if api == "2" else ""
    print()

    # ---- 6. Vision ----
    print(T["s6a"])
    print(T["s6b"])
    vision_default = old.get("ENABLE_VISION", "false").lower() == "true"
    cfg["ENABLE_VISION"] = "true" if confirm(T["vision_ask"], default_yes=vision_default) else "false"
    print()

    # ---- 6.5 Gemini 搜索方式（仅当端点/模型指向 Gemini 时询问）----
    is_gemini = "generativelanguage.googleapis.com" in base_url or model.lower().startswith("gemini")
    gmode = ""
    if is_gemini:
        print(T["s_gmode_a"])
        print(T["s_gmode_b"])
        if old_protocol == "gemini":
            gmode_default = "1"
        elif old.get("GEMINI_SEARCH_MODEL", "").strip():
            gmode_default = "2"
        else:
            gmode_default = "2"  # 免费档 grounding 通常只在 2.5 系列可用，混合是最稳default

        def validate_gmode(raw: str):
            return (True, "") if raw.strip() in ("1", "2", "3") else (False, T["gmode_invalid"])

        gmode = ask(T["gmode_pick"], default=gmode_default, validate=validate_gmode).strip()
        cfg["LLM_PROTOCOL"] = "gemini" if gmode == "1" else ""
        cfg["GEMINI_API_KEY"] = ""  # Gemini 后端 grounding 直接复用 LLM_API_KEY
        if gmode == "2":
            cfg["GEMINI_SEARCH_MODEL"] = ask(
                T["gsearch_model"], default=old.get("GEMINI_SEARCH_MODEL", "gemini-2.5-flash"),
                required=True)
        else:
            cfg["GEMINI_SEARCH_MODEL"] = ""
        print()
    else:
        # 非 Gemini 端点：显式置空，防止切换配置后残留的搜索模型引发启动错误
        cfg["GEMINI_SEARCH_MODEL"] = ""

    # ---- 7. Web search ----
    if gmode in ("1", "2"):
        print(T["gmode_skip_search"])
        print()
    else:
        print(T["s_search_a"])
        print(T["s_search_b"])
        provider_alias = {"1": "tavily", "2": "duckduckgo", "3": "searxng", "4": "serper"}
        off_values = ("0", "none", "off")
        known = ("tavily", "duckduckgo", "searxng", "serper")

        def parse_providers(raw: str) -> list[str] | None:
            """解析多选输入（序号或名称，逗号分隔）。None 表示无法解析。"""
            parts = [p.strip().lower() for p in raw.replace("，", ",").split(",") if p.strip()]
            if not parts:
                return []
            if any(p in off_values for p in parts):
                return [] if len(parts) == 1 else None
            out = []
            for p in parts:
                p = provider_alias.get(p, p)
                if p not in known:
                    return None
                if p not in out:
                    out.append(p)
            return out

        def validate_provider(raw: str):
            if parse_providers(raw) is None:
                return False, T["search_invalid"]
            return True, ""

        raw_provider = ask(T["search_pick"], default=old.get("SEARCH_PROVIDER", ""), validate=validate_provider)
        providers = parse_providers(raw_provider) or []
        cfg["SEARCH_PROVIDER"] = ",".join(providers)
        if "tavily" in providers:
            cfg["TAVILY_API_KEY"] = ask(T["tavily_key"], default=old.get("TAVILY_API_KEY", ""),
                                        required=True, secret=True)
        if "serper" in providers:
            cfg["SERPER_API_KEY"] = ask(T["serper_key"], default=old.get("SERPER_API_KEY", ""),
                                        required=True, secret=True)
        if "searxng" in providers:
            cfg["SEARXNG_BASE_URL"] = ask(T["searxng_url"], default=old.get("SEARXNG_BASE_URL", ""),
                                          required=True)
        if providers:
            # open_url 直取失败时的 Jina Reader 兜底（bot 默认开启，仅在用户关闭时写入 false）
            jina_default = old.get("JINA_FALLBACK", "true").strip().lower() not in ("0", "false", "no", "off")
            if confirm(T["jina_ask"], default_yes=jina_default):
                cfg["JINA_API_KEY"] = ask(T["jina_key"], default=old.get("JINA_API_KEY", ""), secret=True)
            else:
                cfg["JINA_FALLBACK"] = "false"
        if backend != "2":
            # 任意后端都可叠加 Gemini grounding：搜索由 Gemini + Google 官方搜索执行，
            # 失败自动回退上面配置的搜索源
            print()
            print(T["s_genhance"])
            if confirm(T["genhance_ask"], default_yes=bool(old.get("GEMINI_SEARCH_MODEL", "").strip())):
                cfg["GEMINI_API_KEY"] = ask(T["gemini_key"], default=old.get("GEMINI_API_KEY", ""),
                                            required=True, secret=True)
                cfg["GEMINI_SEARCH_MODEL"] = ask(
                    T["gsearch_model"], default=old.get("GEMINI_SEARCH_MODEL", "gemini-2.5-flash"),
                    required=True)
                route_default = "2" if old.get("GEMINI_BASE_URL", "").strip() else "1"
                route = ask(T["gemini_route_pick"], default=route_default, validate=validate_route).strip()
                if route == "2":
                    cfg["GEMINI_BASE_URL"] = ask(T["relay_addr"], default=old.get("GEMINI_BASE_URL", ""),
                                                 required=True).strip().rstrip("/")
                else:
                    cfg["GEMINI_BASE_URL"] = ""
            else:
                cfg["GEMINI_API_KEY"] = ""
                cfg["GEMINI_SEARCH_MODEL"] = ""
                cfg["GEMINI_BASE_URL"] = ""
        print()

    # ---- 8. Generation params & timezone ----
    print(T["s7"])
    protocol = cfg.get("LLM_PROTOCOL", "")
    reasoning_gpt = protocol in ("", "responses") and is_reasoning_gpt(cfg["LLM_MODEL"])
    old_max = old.get("MAX_TOKENS", "1024")
    if reasoning_gpt and old_max.isdigit() and int(old_max) < 4096:
        # 推理 token 也计入上限，1024 很容易被思考过程吃光，导致回复截断或为空
        print(T["max_tokens_reasoning"])
        old_max = "4096"
    cfg["MAX_TOKENS"] = ask(T["max_tokens"], default=old_max, validate=validate_int)

    old_extra_raw = old.get("LLM_EXTRA_BODY", "")
    extra = parse_extra_params(old_extra_raw)
    if extra is None:
        print(T["extra_old_invalid"].format(raw=old_extra_raw))
        extra = {}

    effort = ""
    if reasoning_gpt:
        print(T["effort_intro"].format(model=cfg["LLM_MODEL"]))
        for i, name in enumerate(REASONING_EFFORTS, 1):
            print(f"    {i}. {name:<8} {T['effort_desc'][name]}")
        raw_e = ask(T["effort_pick"], default=current_effort(extra) or "low",
                    validate=validate_effort).strip().lower()
        if raw_e.isdigit():
            effort = REASONING_EFFORTS[int(raw_e) - 1]
        elif raw_e != "-":
            effort = raw_e
        # 推理强度由向导管理：两种写法都先清掉，再按当前协议写回
        extra = {k: v for k, v in extra.items() if k not in _REASONING_KEYS}

    if reasoning_gpt and effort != "none":
        # 推理档位不是 none 时，GPT 推理模型不接受 temperature/top_p
        print(T["sampling_skipped"])
        cfg["LLM_TEMPERATURE"] = ""
        cfg["LLM_TOP_P"] = ""
    else:
        raw_t = ask(T["temperature"], default=old.get("LLM_TEMPERATURE", ""), validate=validate_opt_float)
        cfg["LLM_TEMPERATURE"] = "" if raw_t == "-" else raw_t
        raw_p = ask(T["top_p"], default=old.get("LLM_TOP_P", ""), validate=validate_opt_float)
        cfg["LLM_TOP_P"] = "" if raw_p == "-" else raw_p

    raw_x = ask(T["extra_params"], default=format_extra_params(extra) if extra else "",
                validate=validate_extra_params).strip()
    extra = {} if raw_x == "-" else (parse_extra_params(raw_x) or {})
    extra.update(effort_body(effort, protocol))
    cfg["LLM_EXTRA_BODY"] = json.dumps(extra, ensure_ascii=False, separators=(",", ":")) if extra else ""
    cfg["MAX_HISTORY"] = ask(T["max_history"], default=old.get("MAX_HISTORY", "20"), validate=validate_int)
    cfg["BOT_TZ"] = ask(T["tz"], default=old.get("BOT_TZ", "Asia/Shanghai"))
    print()

    # ---- 9. System prompt ----
    print(T["s8"])
    cfg["SYSTEM_PROMPT"] = ask_multiline(T["sys_prompt"], default=old.get("SYSTEM_PROMPT", ""))
    print()

    # 向导未覆盖的自定义配置项（如 SEARCH_MAX_RESULTS、FETCH_CHAR_LIMIT 等）原样保留
    for key, val in old.items():
        if key not in cfg and val:
            cfg[key] = val

    # ---- Summary ----
    print("=" * 52)
    print(T["summary"])
    for key, val in cfg.items():
        if not val:
            continue
        is_secret = "TOKEN" in key or key.endswith("_KEY")
        shown = (val[:8] + "…" + val[-4:]) if is_secret and len(val) > 16 else preview(val)
        print(f"  {key} = {shown}")
    print("=" * 52)
    if not confirm(T["write_confirm"].format(path=env_path), default_yes=True):
        print(T["cancelled"])
        return

    lines = [T["header"], ""]
    lines += [env_line(k, v) for k, v in cfg.items() if v]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(T["written"].format(path=env_path))
    if not is_profile:
        print(T["next"])


def main() -> None:
    global T
    parser = argparse.ArgumentParser(description="Interactive .env generator")
    parser.add_argument("--output", default=None, help="output file path (default: .env next to this script)")
    parser.add_argument("--no-check", action="store_true", help="skip all online validation")
    args = parser.parse_args()

    env_path = Path(args.output) if args.output else Path(__file__).with_name(".env")
    old = load_existing(env_path)

    lang = choose_language(old)
    T = TEXT[lang]
    print()

    can_check = httpx is not None and not args.no_check
    if httpx is None and not args.no_check:
        print(T["no_httpx"])

    # 首次使用（既无 .env 也无配置档）：保持原体验，直接进向导
    if not old and not list_profiles():
        run_wizard(env_path, old, can_check, lang)
        return

    while True:
        act = active_profile(env_path)
        print(T["menu_title"].format(active=act or T["menu_no_profile"]))
        print(T["menu_body"])
        raw = input("> ").strip().lower()
        print()
        if raw in ("", "0", "q", "quit", "exit"):
            return
        if raw == "1":
            act = active_profile(env_path)  # 编辑前记下当前对应的配置档，写完 .env 同步回去
            run_wizard(env_path, load_existing(env_path), can_check, lang)
            sync_profile(env_path, act)
            offer_restart()
        elif raw == "2":
            name = pick_profile(env_path, T["p_pick_use"])
            if name:
                activate_profile(env_path, name)
        elif raw == "3":
            name = input(T["p_name_ask"] + "\n> ").strip()
            if not PROFILE_NAME_RE.match(name):
                print(T["p_bad_name"])
                continue
            PROFILES_DIR.mkdir(exist_ok=True)
            ppath = PROFILES_DIR / f"{name}.env"
            # 新配置档以自身现有内容为默认；全新的档用当前 .env 打底，改几项即可
            defaults = load_existing(ppath) or dict(load_existing(env_path))
            run_wizard(ppath, defaults, can_check, lang, is_profile=True)
            if ppath.exists() and confirm(T["p_use_now"], default_yes=True):
                activate_profile(env_path, name)
        elif raw == "4":
            name = pick_profile(env_path, T["p_pick_del"])
            if name and confirm(T["p_del_confirm"].format(name=name)):
                (PROFILES_DIR / f"{name}.env").unlink()
                print(T["p_deleted"].format(name=name))
        print()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n" + T["cancelled"])
        sys.exit(1)
