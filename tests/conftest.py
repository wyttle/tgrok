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
    "RESEARCH_MODELS": "",
    "GEMINI_SEARCH_MODEL": "",
    "GEMINI_MEDIA_MODEL": "",
    "GEMINI_HELPER_MODEL": "",
    "SEARCH_PROVIDER": "",
    "ADMIN_USER_IDS": "999",
    "ALLOWED_USER_IDS": "",
    "BOT_LANG": "zh",
    "STREAM_IDLE_TIMEOUT": "0",
    "ENABLE_VISION": "false",
    "WHITELIST_FILE": str(_TMP / "allowed_users.json"),
    "MEMORY_ENABLED": "true",
    "MEMORY_FILE": str(_TMP / "memory.json"),
    "MEMORY_DB": str(_TMP / "memory.db"),
    "SLANG_ENABLED": "true",
    "SLANG_FILE": str(_TMP / "slang.json"),
    "RESEARCH_WARM": "false",  # 常驻 omp 绑定事件循环；要测它的用例自己开、自己收尾
})
os.environ.pop("SYSTEM_PROMPT", None)

from tgrok import chat, config, conversations, llm, logs, media, recent, slang, tg_auth, viewer  # noqa: E402
from tgrok.db import db  # noqa: E402
from tgrok.memory import consolidate  # noqa: E402

# 假模型和假工具都是瞬间返回：私聊的显示限速按 0 算，否则工具行在下个刷新周期前就被终稿替换，测不到进度
config.STREAM_EDIT_INTERVAL_PRIVATE = 0


def _clear_state() -> None:
    db().executescript("DELETE FROM messages; DELETE FROM notes; DELETE FROM progress; DELETE FROM usage;")
    logs._current.set(None)
    consolidate._running.clear()
    slang._entries.clear()
    conversations.clear()
    media.album_cache.clear()
    media.watch_cache.clear()
    recent.clear()
    viewer.clear()
    chat.active_generations.clear()


@pytest.fixture(autouse=True)
def isolate():
    saved_config = {k: v for k, v in vars(config).items() if k.isupper()}
    saved_adapters = (llm.adapter, llm.fallback_adapter, llm.helper_adapter)
    saved_users = set(tg_auth.allowed_users)
    _clear_state()
    yield
    for k, v in saved_config.items():
        setattr(config, k, v)
    llm.adapter, llm.fallback_adapter, llm.helper_adapter = saved_adapters
    tg_auth.allowed_users.clear()
    tg_auth.allowed_users.update(saved_users)
    _clear_state()
