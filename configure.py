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
        "s1": "【1/8】Telegram Bot Token（找 @BotFather 发 /newbot 获取）",
        "token": "Bot Token",
        "token_invalid_fmt": "格式不对，Bot Token 形如 123456789:ABCdefGhI...（从 @BotFather 获取）",
        "token_net_fail": "  ⚠ 无法连接 Telegram 验证（{err}），跳过在线验证",
        "token_ok": "  ✓ Token 有效，bot 用户名：@{username}",
        "token_bad": "  ✗ Token 无效（Telegram 返回未授权）",
        "token_use_anyway": "  仍然使用该 Token？",
        "s2a": "【2/8】超级管理员用户 ID（强烈建议填写自己的 ID，可用 @userinfobot 查询）",
        "s2b": "      配置后进入受控模式：仅管理员 + 白名单用户可用，可用 /adduser /deluser 管理",
        "admin_ids": "管理员 ID（多个用逗号分隔）",
        "ids_invalid": "请输入纯数字的用户 ID，多个用逗号分隔，例如：123456789,987654321",
        "s3": "【3/8】白名单初始用户 ID（之后随时可用 /adduser 添加，这里可跳过）",
        "allowed_ids": "白名单 ID（多个用逗号分隔）",
        "s4a": "【4/8】LLM 的 OpenAI 兼容接口",
        "s4b": "      LM Studio 默认 http://localhost:1234/v1，vLLM 默认 http://localhost:8000/v1，OpenAI 官方 https://api.openai.com/v1",
        "base_url": "接口地址",
        "api_key": "API Key（本地服务一般随便填）",
        "ua": "自定义 User-Agent（部分云端网关会校验 UA，可选）",
        "s5": "【5/8】模型名称",
        "models_fail": "  ⚠ 无法连接 {url} 获取模型列表（{err}），请手动输入",
        "models_found": "  ✓ 检测到以下可用模型：",
        "model_pick": "输入序号选择，或直接输入模型名",
        "model_name": "模型名称",
        "s6a": "【6/8】图片理解（多模态）",
        "s6b": "      模型支持视觉输入时开启：群友发图或回复图片提问，图片会发给模型一起分析",
        "vision_ask": "  开启图片理解？",
        "s7": "【7/8】生成参数",
        "max_tokens": "单次回答最大 token 数",
        "max_history": "多轮对话保留消息条数",
        "int_invalid": "请输入正整数",
        "s8": "【8/8】系统提示词（定义 bot 的角色和语气，跳过则使用内置默认值）",
        "sys_prompt": "系统提示词",
        "summary": "配置汇总：",
        "write_confirm": "确认写入 {path}？",
        "cancelled": "已取消，未写入任何文件。",
        "header": "# 由 configure.py 生成，重新运行该脚本可修改配置",
        "written": "\n✓ 已写入 {path}",
        "next": "启动 bot：python bot.py（或 docker compose up -d --build）",
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
        "s1": "[1/8] Telegram Bot Token (get one from @BotFather with /newbot)",
        "token": "Bot Token",
        "token_invalid_fmt": "Invalid format. A bot token looks like 123456789:ABCdefGhI... (from @BotFather)",
        "token_net_fail": "  ⚠ Could not reach Telegram to validate ({err}); skipping online check",
        "token_ok": "  ✓ Token is valid, bot username: @{username}",
        "token_bad": "  ✗ Invalid token (Telegram returned unauthorized)",
        "token_use_anyway": "  Use this token anyway?",
        "s2a": "[2/8] Super admin user IDs (strongly recommended — use @userinfobot to find yours)",
        "s2b": "      With admins set, the bot is in controlled mode: only admins + whitelisted users; manage with /adduser /deluser",
        "admin_ids": "Admin IDs (comma-separated)",
        "ids_invalid": "Please enter numeric user IDs, comma-separated, e.g. 123456789,987654321",
        "s3": "[3/8] Initial whitelist user IDs (you can always /adduser later; OK to skip)",
        "allowed_ids": "Whitelist IDs (comma-separated)",
        "s4a": "[4/8] OpenAI-compatible LLM endpoint",
        "s4b": "      LM Studio default http://localhost:1234/v1, vLLM http://localhost:8000/v1, official OpenAI https://api.openai.com/v1",
        "base_url": "Endpoint URL",
        "api_key": "API key (anything works for most local servers)",
        "ua": "Custom User-Agent (some cloud gateways validate it; optional)",
        "s5": "[5/8] Model name",
        "models_fail": "  ⚠ Could not fetch model list from {url} ({err}); please type it manually",
        "models_found": "  ✓ Available models detected:",
        "model_pick": "Pick a number, or type a model name",
        "model_name": "Model name",
        "s6a": "[6/8] Image understanding (multimodal)",
        "s6b": "      Enable if the model supports vision: images sent or quoted in chat are passed to the model",
        "vision_ask": "  Enable image understanding?",
        "s7": "[7/8] Generation parameters",
        "max_tokens": "Max tokens per reply",
        "max_history": "Messages kept per conversation",
        "int_invalid": "Please enter a positive integer",
        "s8": "[8/8] System prompt (defines the bot's role and tone; skip for the built-in default)",
        "sys_prompt": "System prompt",
        "summary": "Configuration summary:",
        "write_confirm": "Write to {path}?",
        "cancelled": "Cancelled. Nothing was written.",
        "header": "# Generated by configure.py; re-run the script to change settings",
        "written": "\n✓ Written to {path}",
        "next": "Start the bot: python bot.py (or docker compose up -d --build)",
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

    # ---- 4. LLM endpoint ----
    print(T["s4a"])
    print(T["s4b"])
    base_url = ask(T["base_url"], default=old.get("LLM_BASE_URL", "http://localhost:1234/v1"), required=True)
    cfg["LLM_BASE_URL"] = base_url
    cfg["LLM_API_KEY"] = ask(T["api_key"], default=old.get("LLM_API_KEY", "not-needed"))
    cfg["LLM_USER_AGENT"] = ask(T["ua"], default=old.get("LLM_USER_AGENT", ""))
    print()

    # ---- 5. Model ----
    print(T["s5"])
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
            raw = ask(T["model_pick"], default=old.get("LLM_MODEL", models[0]), required=True)
            model = models[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(models) else raw
    if not model:
        model = ask(T["model_name"], default=old.get("LLM_MODEL", "local-model"), required=True)
    cfg["LLM_MODEL"] = model
    print()

    # ---- 6. Vision ----
    print(T["s6a"])
    print(T["s6b"])
    vision_default = old.get("ENABLE_VISION", "false").lower() == "true"
    cfg["ENABLE_VISION"] = "true" if confirm(T["vision_ask"], default_yes=vision_default) else "false"
    print()

    # ---- 7. Generation params ----
    print(T["s7"])
    cfg["MAX_TOKENS"] = ask(T["max_tokens"], default=old.get("MAX_TOKENS", "1024"), validate=validate_int)
    cfg["MAX_HISTORY"] = ask(T["max_history"], default=old.get("MAX_HISTORY", "20"), validate=validate_int)
    print()

    # ---- 8. System prompt ----
    print(T["s8"])
    cfg["SYSTEM_PROMPT"] = ask(T["sys_prompt"], default=old.get("SYSTEM_PROMPT", ""))
    print()

    # ---- Summary ----
    print("=" * 52)
    print(T["summary"])
    for key, val in cfg.items():
        if not val:
            continue
        shown = (val[:8] + "…" + val[-4:]) if key == "TELEGRAM_BOT_TOKEN" else val
        print(f"  {key} = {shown}")
    print("=" * 52)
    if not confirm(T["write_confirm"].format(path=env_path), default_yes=True):
        print(T["cancelled"])
        sys.exit(0)

    lines = [T["header"], ""]
    lines += [env_line(k, v) for k, v in cfg.items() if v]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(T["written"].format(path=env_path))
    print(T["next"])


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n" + T["cancelled"])
        sys.exit(1)
