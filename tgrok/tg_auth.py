"""白名单与管理员判定。"""

import logging

from . import config, jsonstore

logger = logging.getLogger(__name__)


def _load_allowed_users() -> set[int]:
    """优先读白名单文件（运行时增删的结果），首次启动用 .env 的 ALLOWED_USER_IDS。"""
    initial = set(config.ALLOWED_USER_IDS)
    try:
        return {int(x) for x in jsonstore.load(config.WHITELIST_FILE, sorted(initial), list)}
    except (TypeError, ValueError):
        logger.warning("%s 里有不是用户 ID 的内容，改用 ALLOWED_USER_IDS", config.WHITELIST_FILE)
        return initial


allowed_users: set[int] = _load_allowed_users()


def save_allowed_users() -> None:
    jsonstore.save(config.WHITELIST_FILE, sorted(allowed_users))


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_USER_IDS


def is_authorized(user_id: int) -> bool:
    """管理员永远可用；配置了管理员或白名单后即进入受控模式，否则对所有人开放。"""
    if is_admin(user_id):
        return True
    if not config.ADMIN_USER_IDS and not allowed_users:
        return True
    return user_id in allowed_users
