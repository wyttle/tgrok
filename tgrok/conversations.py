"""追问用的对话缓存：bot 的每条回复消息对应一段对话历史，群友回复那条消息就能接着聊。

key = (chat_id, bot 回复消息的 message_id)，value = OpenAI 格式的 messages 列表。
按条目数和近似内容字符量（文本和图片 data URL 的长度）两个上限驱逐最旧的；
字符量不是进程内存硬上限，共享消息对象会被重复计数，高估只会让缓存更早驱逐。
"""

from collections import OrderedDict

from . import config

_cache: "OrderedDict[tuple[int, int], list[dict]]" = OrderedDict()
_sizes: dict[tuple[int, int], int] = {}
_total = 0


def _history_chars(history: list[dict]) -> int:
    total = 0
    for message in history:
        content = message.get("content", "")
        if isinstance(content, str):
            total += len(content)
        else:
            for part in content:
                total += len(part.get("text", ""))
                total += len((part.get("image_url") or {}).get("url", ""))
    return total


def get(chat_id: int, message_id: int) -> list[dict] | None:
    return _cache.get((chat_id, message_id))


def remember(chat_id: int, message_id: int, history: list[dict]) -> None:
    global _total
    key = (chat_id, message_id)
    _total -= _sizes.get(key, 0)
    size = _history_chars(history)
    _cache[key] = history
    _sizes[key] = size
    _total += size
    _cache.move_to_end(key)
    while len(_cache) > 1 and (
        len(_cache) > config.CONVERSATION_CACHE_SIZE or _total > config.CONVERSATION_CONTENT_BUDGET
    ):
        oldest, _ = _cache.popitem(last=False)
        _total -= _sizes.pop(oldest)


def trim(history: list[dict]) -> list[dict]:
    """保留 system 消息 + 最近 config.MAX_HISTORY 条对话，且截断后第一条对话必须是 user。

    以 assistant 开头的历史会被 Claude 原生接口和多数本地模型的对话模板
    （要求 user/assistant 严格交替）拒绝，也会丢掉那条回答对应的问题。
    """
    if len(history) <= config.MAX_HISTORY + 1:
        return history
    tail = history[-config.MAX_HISTORY:]
    start = next((i for i, m in enumerate(tail) if m["role"] == "user"), len(tail))
    return [history[0]] + tail[start:]
