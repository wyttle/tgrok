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
        "field_required": "  ✗ 该项必填，请输入。\n",
        "yes_values": ("y", "yes", "是"),
        "s1": "【1/9】Telegram Bot Token（找 @BotFather 发 /newbot 获取）",
        "token": "Bot Token",
        "token_invalid_fmt": "格式不对，Bot Token 形如 123456789:ABCdefGhI...（从 @BotFather 获取）",
        "token_net_fail": "  ⚠ 无法连接 Telegram 验证（{err}），跳过在线验证",
        "token_ok": "  ✓ Token 有效，bot 用户名：@{username}",
        "token_bad": "  ✗ Token 无效（Telegram 返回未授权）",
        "token_use_anyway": "  仍然使用该 Token？",
        "s2a": "【2/9】超级管理员用户 ID（强烈建议填写自己的 ID，可用 @userinfobot 查询）",
        "s2b": "      配置后进入受控模式：仅管理员 + 白名单用户可用，可用 /adduser /deluser 管理",
        "admin_ids": "管理员 ID（多个用逗号分隔）",
        "ids_invalid": "请输入纯数字的用户 ID，多个用逗号分隔，例如：123456789,987654321",
        "s3": "【3/9】白名单初始用户 ID（之后随时可用 /adduser 添加，这里可跳过）",
        "allowed_ids": "白名单 ID（多个用逗号分隔）",
        "s4a": "【4/9】模型后端",
        "s4b": "      1 = OpenAI 兼容接口（中转站 / LM Studio / vLLM / OpenAI 官方等，需填接口地址）\n      2 = Gemini 官方（Google AI Studio，key 在 https://aistudio.google.com/apikey 免费申请）",
        "backend_pick": "后端类型：1=OpenAI 兼容  2=Gemini 官方",
        "backend_invalid": "请输入 1 或 2",
        "gemini_key": "Gemini API Key（官方为 AI Studio key；走中转站则为中转站 key）",
        "gemini_route_pick": "Gemini 接入：1=Google 官方直连  2=中转站（转发原生格式）",
        "gemini_route_invalid": "请输入 1 或 2",
        "relay_addr": "中转站根地址（如 https://xxx.com）",
        "note_relay_compat": "  → 原生/grounding 走该地址，回复走 {url}（中转站 OpenAI 兼容路径）",
        "base_url": "接口地址",
        "api_key": "API Key（本地服务一般随便填）",
        "ua": "自定义 User-Agent（部分云端网关会校验 UA，可选）",
        "s5": "【5/9】模型名称",
        "models_fail": "  ⚠ 无法连接 {url} 获取模型列表（{err}），请手动输入",
        "models_found": "  ✓ 检测到以下可用模型：",
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
        "max_history": "多轮对话保留消息条数",
        "tz": "时区（IANA 名称，用于告知模型当前真实时间；无法识别时 bot 会回退 UTC）",
        "int_invalid": "请输入正整数",
        "s8": "【9/9】系统提示词（定义 bot 的角色和语气，跳过则使用内置默认值）",
        "sys_prompt": "系统提示词",
        "summary": "配置汇总：",
        "write_confirm": "确认写入 {path}？",
        "cancelled": "已取消，未写入任何文件。",
        "header": "# 由 configure.py 生成，重新运行该脚本可修改配置",
        "written": "\n✓ 已写入 {path}",
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
        "p_applied": "✓ 已启用配置档：{name}",
        "p_backup": "  原 .env 已备份为 {path}",
        "p_use_now": "立即启用该配置档？",
        "p_del_confirm": "确认删除配置档「{name}」？",
        "p_deleted": "✓ 已删除：{name}",
        "p_restart_ask": "重启 Docker 容器使配置生效？",
        "p_restarting": "重启容器…",
        "p_restart_done": "✓ 容器已重启",
        "p_restart_fail": "⚠ 重启命令返回错误，请手动检查（docker compose up -d）",
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
        "field_required": "  ✗ This field is required.\n",
        "yes_values": ("y", "yes"),
        "s1": "[1/9] Telegram Bot Token (get one from @BotFather with /newbot)",
        "token": "Bot Token",
        "token_invalid_fmt": "Invalid format. A bot token looks like 123456789:ABCdefGhI... (from @BotFather)",
        "token_net_fail": "  ⚠ Could not reach Telegram to validate ({err}); skipping online check",
        "token_ok": "  ✓ Token is valid, bot username: @{username}",
        "token_bad": "  ✗ Invalid token (Telegram returned unauthorized)",
        "token_use_anyway": "  Use this token anyway?",
        "s2a": "[2/9] Super admin user IDs (strongly recommended — use @userinfobot to find yours)",
        "s2b": "      With admins set, the bot is in controlled mode: only admins + whitelisted users; manage with /adduser /deluser",
        "admin_ids": "Admin IDs (comma-separated)",
        "ids_invalid": "Please enter numeric user IDs, comma-separated, e.g. 123456789,987654321",
        "s3": "[3/9] Initial whitelist user IDs (you can always /adduser later; OK to skip)",
        "allowed_ids": "Whitelist IDs (comma-separated)",
        "s4a": "[4/9] Model backend",
        "s4b": "      1 = OpenAI-compatible endpoint (relay / LM Studio / vLLM / official OpenAI; needs an endpoint URL)\n      2 = Official Gemini (Google AI Studio; get a free key at https://aistudio.google.com/apikey)",
        "backend_pick": "Backend: 1=OpenAI-compatible  2=Official Gemini",
        "backend_invalid": "Enter 1 or 2",
        "gemini_key": "Gemini API key (AI Studio key for official; relay key when routed through a relay)",
        "gemini_route_pick": "Gemini routing: 1=official Google  2=relay (forwards native format)",
        "gemini_route_invalid": "Enter 1 or 2",
        "relay_addr": "Relay root URL (e.g. https://xxx.com)",
        "note_relay_compat": "  -> native/grounding use that URL; replies go through {url} (relay's OpenAI-compatible path)",
        "base_url": "Endpoint URL",
        "api_key": "API key (anything works for most local servers)",
        "ua": "Custom User-Agent (some cloud gateways validate it; optional)",
        "s5": "[5/9] Model name",
        "models_fail": "  ⚠ Could not fetch model list from {url} ({err}); please type it manually",
        "models_found": "  ✓ Available models detected:",
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
        "max_history": "Messages kept per conversation",
        "tz": "Timezone (IANA name, used to tell the model the current real time; falls back to UTC if unrecognized)",
        "int_invalid": "Please enter a positive integer",
        "s8": "[9/9] System prompt (defines the bot's role and tone; skip for the built-in default)",
        "sys_prompt": "System prompt",
        "summary": "Configuration summary:",
        "write_confirm": "Write to {path}?",
        "cancelled": "Cancelled. Nothing was written.",
        "header": "# Generated by configure.py; re-run the script to change settings",
        "written": "\n✓ Written to {path}",
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
        "p_applied": "✓ Activated profile: {name}",
        "p_backup": "  Previous .env backed up as {path}",
        "p_use_now": "Activate this profile now?",
        "p_del_confirm": "Delete profile \"{name}\"?",
        "p_deleted": "✓ Deleted: {name}",
        "p_restart_ask": "Restart the Docker container to apply?",
        "p_restarting": "Restarting container…",
        "p_restart_done": "✓ Container restarted",
        "p_restart_fail": "⚠ Restart command failed; please check manually (docker compose up -d)",
        "p_restart_hint": "Note: takes effect after restart (docker compose up -d, or restart python bot.py)",
    },
}

T = TEXT["zh"]  # set after language selection


def load_existing(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] == '"':
            val = val[1:-1].replace('\\"', '"')
        values[key.strip()] = val
    return values


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
                print(f"  ✗ {msg}\n")
                continue
        return raw


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
    if any(c in val for c in (" ", "#", '"')):
        val = '"' + val.replace('"', '\\"') + '"'
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
            r = subprocess.run(["docker", "compose", "up", "-d"], cwd=str(compose.parent))
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
        return (True, "") if raw.strip() in ("1", "2") else (False, T["backend_invalid"])

    backend_default = "2" if "generativelanguage.googleapis.com" in old.get("LLM_BASE_URL", "") else "1"
    backend = ask(T["backend_pick"], default=backend_default, validate=validate_backend).strip()

    def validate_route(raw: str):
        return (True, "") if raw.strip() in ("1", "2") else (False, T["gemini_route_invalid"])

    if backend == "2":
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
    # 换到 Gemini 后端时，旧的非 gemini 模型名不再是合理默认值
    model_default = old.get("LLM_MODEL", "")
    if backend == "2" and not model_default.lower().startswith("gemini"):
        model_default = "gemini-2.5-flash"
    model = ""
    if can_check:
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
        if old.get("GEMINI_NATIVE_SEARCH", "").strip().lower() in ("1", "true", "yes", "on"):
            gmode_default = "1"
        elif old.get("GEMINI_SEARCH_MODEL", "").strip():
            gmode_default = "2"
        else:
            gmode_default = "2"  # 免费档 grounding 通常只在 2.5 系列可用，混合是最稳default

        def validate_gmode(raw: str):
            return (True, "") if raw.strip() in ("1", "2", "3") else (False, T["gmode_invalid"])

        gmode = ask(T["gmode_pick"], default=gmode_default, validate=validate_gmode).strip()
        cfg["GEMINI_NATIVE_SEARCH"] = "true" if gmode == "1" else "false"
        cfg["GEMINI_API_KEY"] = ""  # Gemini 后端 grounding 直接复用 LLM_API_KEY
        if gmode == "2":
            cfg["GEMINI_SEARCH_MODEL"] = ask(
                T["gsearch_model"], default=old.get("GEMINI_SEARCH_MODEL", "gemini-2.5-flash"),
                required=True)
        else:
            cfg["GEMINI_SEARCH_MODEL"] = ""
        print()
    else:
        # 非 Gemini 端点：显式置空，防止切换配置后残留的开关引发启动错误
        cfg["GEMINI_NATIVE_SEARCH"] = ""
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
    cfg["MAX_TOKENS"] = ask(T["max_tokens"], default=old.get("MAX_TOKENS", "1024"), validate=validate_int)
    cfg["MAX_HISTORY"] = ask(T["max_history"], default=old.get("MAX_HISTORY", "20"), validate=validate_int)
    cfg["BOT_TZ"] = ask(T["tz"], default=old.get("BOT_TZ", "Asia/Shanghai"))
    print()

    # ---- 9. System prompt ----
    print(T["s8"])
    cfg["SYSTEM_PROMPT"] = ask(T["sys_prompt"], default=old.get("SYSTEM_PROMPT", ""))
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
        shown = (val[:8] + "…" + val[-4:]) if is_secret and len(val) > 16 else val
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
            run_wizard(env_path, load_existing(env_path), can_check, lang)
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
