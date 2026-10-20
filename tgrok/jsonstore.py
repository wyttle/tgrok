"""JSON 文件持久化（白名单、记忆、梗词典共用）。

读取失败返回调用方给的初始值；写入先写临时文件再原子替换，写到一半崩溃也不会留下半截文件。
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load(path: Path, default, expect: type):
    """读 JSON；文件不存在、读不了、解析失败或顶层类型不是 expect 时返回 default。"""
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("读取 %s 失败，改用初始值：%s", path, e)
        return default
    if not isinstance(data, expect):
        logger.warning("%s 内容格式不对（应为 %s），改用初始值", path, expect.__name__)
        return default
    return data


def save(path: Path, data) -> None:
    """原子写入 JSON；失败只记日志，不打断调用方。"""
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logger.warning("写入 %s 失败：%s", path, e)
