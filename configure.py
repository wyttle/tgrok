#!/usr/bin/env python
"""
交互式配置向导：python configure.py

逐项询问所有配置，生成/更新 .env 文件。特性：
  - 已有 .env 时读取现有值作为默认值，回车即保留
  - 在线验证 Bot Token 是否有效（可跳过）
  - 自动连接本地 LLM 服务，列出可用模型供选择
  - 用户 ID、数字等格式校验
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
            hint = f"[回车保留: {shown}]"
        else:
            hint = "[必填]" if required else "[可选，回车跳过]"
        raw = input(f"{label} {hint}\n> ").strip()
        if not raw:
            if default:
                return default
            if not required:
                return ""
            print("  ✗ 该项必填，请输入。\n")
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
    return raw in ("y", "yes", "是")


def validate_token(raw: str):
    if re.match(r"^\d+:[\w-]{30,}$", raw):
        return True, ""
    return False, "格式不对，Bot Token 形如 123456789:ABCdefGhI...（从 @BotFather 获取）"


def validate_ids(raw: str):
    parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    if all(p.isdigit() for p in parts):
        return True, ""
    return False, "请输入纯数字的用户 ID，多个用逗号分隔，例如：123456789,987654321"


def validate_int(raw: str):
    if raw.isdigit() and int(raw) > 0:
        return True, ""
    return False, "请输入正整数"


def check_telegram_token(token: str) -> str | None:
    """返回 bot 用户名；验证失败返回 None，网络异常时抛出。"""
    resp = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
    data = resp.json()
    if data.get("ok"):
        return data["result"]["username"]
    return None


def list_models(base_url: str, api_key: str) -> list[str]:
    resp = httpx.get(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("data", [])]


def env_line(key: str, val: str) -> str:
    if any(c in val for c in (" ", "#", '"')) :
        val = '"' + val.replace('"', '\\"') + '"'
    return f"{key}={val}"


def main() -> None:
    parser = argparse.ArgumentParser(description="交互式生成 .env 配置")
    parser.add_argument("--output", default=None, help="输出文件路径（默认为脚本同目录的 .env）")
    parser.add_argument("--no-check", action="store_true", help="跳过所有联网验证")
    args = parser.parse_args()

    env_path = Path(args.output) if args.output else Path(__file__).with_name(".env")
    old = load_existing(env_path)
    can_check = httpx is not None and not args.no_check
    if httpx is None and not args.no_check:
        print("提示：未安装 httpx，跳过联网验证（在虚拟环境中运行可启用验证）\n")

    print("=" * 52)
    print("  Telegram 群聊 AI 助手 — 配置向导")
    print("  逐项填写，回车使用默认值/保留现有值")
    print("=" * 52 + "\n")
    if old:
        print(f"检测到已有配置 {env_path}，现有值将作为默认值。\n")

    cfg = {}

    # ---- 1. Bot Token ----
    print("【1/8】Telegram Bot Token（找 @BotFather 发 /newbot 获取）")
    while True:
        token = ask("Bot Token", default=old.get("TELEGRAM_BOT_TOKEN", ""), required=True,
                    validate=validate_token, secret=True)
        if not can_check:
            break
        try:
            username = check_telegram_token(token)
        except Exception as e:
            print(f"  ⚠ 无法连接 Telegram 验证（{type(e).__name__}），跳过在线验证")
            break
        if username:
            print(f"  ✓ Token 有效，bot 用户名：@{username}")
            break
        print("  ✗ Token 无效（Telegram 返回未授权）")
        if confirm("  仍然使用该 Token？"):
            break
    cfg["TELEGRAM_BOT_TOKEN"] = token
    print()

    # ---- 2. 管理员 ----
    print("【2/8】超级管理员用户 ID（强烈建议填写自己的 ID，可用 @userinfobot 查询）")
    print("      配置后进入受控模式：仅管理员 + 白名单用户可用，可用 /adduser /deluser 管理")
    cfg["ADMIN_USER_IDS"] = ask("管理员 ID（多个用逗号分隔）", default=old.get("ADMIN_USER_IDS", ""), validate=validate_ids)
    print()

    # ---- 3. 白名单初始值 ----
    print("【3/8】白名单初始用户 ID（之后随时可用 /adduser 添加，这里可跳过）")
    cfg["ALLOWED_USER_IDS"] = ask("白名单 ID（多个用逗号分隔）", default=old.get("ALLOWED_USER_IDS", ""), validate=validate_ids)
    print()

    # ---- 4. LLM 接口 ----
    print("【4/8】本地 LLM 的 OpenAI 兼容接口")
    print("      LM Studio 默认 http://localhost:1234/v1，vLLM 默认 http://localhost:8000/v1")
    base_url = ask("接口地址", default=old.get("LLM_BASE_URL", "http://localhost:1234/v1"), required=True)
    cfg["LLM_BASE_URL"] = base_url
    cfg["LLM_API_KEY"] = ask("API Key（本地服务一般随便填）", default=old.get("LLM_API_KEY", "not-needed"))
    print()

    # ---- 5. 模型 ----
    print("【5/8】模型名称")
    model = ""
    if can_check:
        try:
            models = list_models(base_url, cfg["LLM_API_KEY"])
        except Exception as e:
            models = []
            print(f"  ⚠ 无法连接 {base_url} 获取模型列表（{type(e).__name__}），请手动输入")
        if models:
            print("  ✓ 检测到以下可用模型：")
            for i, m in enumerate(models, 1):
                print(f"    {i}. {m}")
            raw = ask("输入序号选择，或直接输入模型名", default=old.get("LLM_MODEL", models[0]), required=True)
            model = models[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(models) else raw
    if not model:
        model = ask("模型名称", default=old.get("LLM_MODEL", "local-model"), required=True)
    cfg["LLM_MODEL"] = model
    print()

    # ---- 6. 多模态 ----
    print("【6/8】图片理解（多模态）")
    print("      模型支持视觉输入时开启：群友发图或回复图片提问，图片会发给模型一起分析")
    vision_default = old.get("ENABLE_VISION", "false").lower() == "true"
    cfg["ENABLE_VISION"] = "true" if confirm("  开启图片理解？", default_yes=vision_default) else "false"
    print()

    # ---- 7. 生成参数 ----
    print("【7/8】生成参数")
    cfg["MAX_TOKENS"] = ask("单次回答最大 token 数", default=old.get("MAX_TOKENS", "1024"), validate=validate_int)
    cfg["MAX_HISTORY"] = ask("多轮对话保留消息条数", default=old.get("MAX_HISTORY", "20"), validate=validate_int)
    print()

    # ---- 8. 系统提示词 ----
    print("【8/8】系统提示词（定义 bot 的角色和语气，跳过则使用内置中文默认值）")
    cfg["SYSTEM_PROMPT"] = ask("系统提示词", default=old.get("SYSTEM_PROMPT", ""))
    print()

    # ---- 汇总确认 ----
    print("=" * 52)
    print("配置汇总：")
    for key, val in cfg.items():
        if not val:
            continue
        shown = (val[:8] + "…" + val[-4:]) if key == "TELEGRAM_BOT_TOKEN" else val
        print(f"  {key} = {shown}")
    print("=" * 52)
    if not confirm(f"确认写入 {env_path}？", default_yes=True):
        print("已取消，未写入任何文件。")
        sys.exit(0)

    lines = ["# 由 configure.py 生成，重新运行该脚本可修改配置", ""]
    lines += [env_line(k, v) for k, v in cfg.items() if v]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✓ 已写入 {env_path}")
    print("启动 bot：python bot.py（或 docker compose up -d --build）")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消，未写入任何文件。")
        sys.exit(1)
