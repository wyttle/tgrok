"""白名单与管理员判定。"""

import json
import logging

from .config import ADMIN_USER_IDS, ALLOWED_USER_IDS, WHITELIST_FILE

logger = logging.getLogger(__name__)

def load_allowed_users() -> set[int]:
    """白名单：优先读 allowed_users.json（运行时增删的结果），首次启动用 .env 初始值。"""
    if WHITELIST_FILE.exists():
        try:
            return {int(x) for x in json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))}
        except (ValueError, json.JSONDecodeError):
            logger.warning("allowed_users.json 解析失败，回退到 .env 中的 ALLOWED_USER_IDS")
    return set(ALLOWED_USER_IDS)


def save_allowed_users() -> None:
    WHITELIST_FILE.write_text(json.dumps(sorted(allowed_users)), encoding="utf-8")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def is_authorized(user_id: int) -> bool:
    """管理员永远可用；配置了管理员或白名单后即进入受控模式，否则对所有人开放。"""
    if is_admin(user_id):
        return True
    if not ADMIN_USER_IDS and not allowed_users:
        return True
    return user_id in allowed_users


allowed_users: set[int] = load_allowed_users()
