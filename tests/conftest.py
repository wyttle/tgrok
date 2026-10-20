"""测试环境：导入 tgrok 之前固定环境变量（不读开发机的 .env 设置、不碰仓库里的数据文件），
每个测试结束后还原配置、模型适配器和各模块的内存状态。"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp(prefix="tgrok-test-"))
os.environ.update({
    "TELEGRAM_BOT_TOKEN": "x",
    "LLM_PROTOCOL": "openai",
    "LLM_FALLBACK_MODEL": "",
    "GEMINI_SEARCH_MODEL": "",
    "SEARCH_PROVIDER": "",
    "ADMIN_USER_IDS": "999",
    "ALLOWED_USER_IDS": "",
    "BOT_LANG": "zh",
    "STREAM_IDLE_TIMEOUT": "0",
    "REPLY_SINGLE_PARAGRAPH": "false",
    "ENABLE_VISION": "false",
    "WHITELIST_FILE": str(_TMP / "allowed_users.json"),
    "MEMORY_ENABLED": "true",
    "MEMORY_FILE": str(_TMP / "memory.json"),
    "SLANG_ENABLED": "true",
    "SLANG_FILE": str(_TMP / "slang.json"),
})
os.environ.pop("SYSTEM_PROMPT", None)

from tgrok import chat, config, conversations, llm, media, memory, slang, tg_auth  # noqa: E402


def _clear_state() -> None:
    memory._store.clear()
    memory._compressing.clear()
    slang._entries.clear()
    conversations._cache.clear()
    conversations._sizes.clear()
    conversations._total = 0
    media.album_cache.clear()
    chat.active_generations.clear()


@pytest.fixture(autouse=True)
def isolate():
    saved_config = {k: v for k, v in vars(config).items() if k.isupper()}
    saved_adapters = (llm.adapter, llm.fallback_adapter)
    saved_users = set(tg_auth.allowed_users)
    _clear_state()
    yield
    for k, v in saved_config.items():
        setattr(config, k, v)
    llm.adapter, llm.fallback_adapter = saved_adapters
    tg_auth.allowed_users.clear()
    tg_auth.allowed_users.update(saved_users)
    _clear_state()
