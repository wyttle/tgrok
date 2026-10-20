"""测试用的假对象：Telegram 消息、模型流、SDK 400 错误。"""

import asyncio
import datetime
import json
import types

import httpx
from telegram import Chat, Message, MessageEntity, User

from tgrok import config, llm
from tgrok.llm.openai import OpenAIAdapter

HIST = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def run(coro):
    return asyncio.run(coro)


def ns(**kw):
    return types.SimpleNamespace(**kw)


class FakeSent:
    """bot 发出的一条消息；log 记录这条回复的每一次显示（发送和编辑）。"""

    def __init__(self, log):
        self.text = ""
        self.message_id = 1
        self.log = log

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.log.append(text)


class FakeMsg:
    """群友发来的、bot 要回复的那条消息。"""

    def __init__(self, uid=7):
        self.sent: list[FakeSent] = []
        self.log: list[str] = []
        self.actions: list[str] = []
        self.media: list[tuple] = []  # bot 随回复发出的图片：(类型, 内容, 说明)
        self.chat_id = -100
        self.from_user = ns(id=uid)

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        fs = FakeSent(self.log)
        fs.text = text
        fs.message_id = len(self.sent) + 1
        self.sent.append(fs)
        self.log.append(text)
        return fs

    async def reply_chat_action(self, action):
        self.actions.append(action)

    async def reply_photo(self, photo, caption=None):
        self.media.append(("photo", photo, caption))

    async def reply_animation(self, animation, caption=None):
        self.media.append(("animation", animation, caption))

    async def reply_document(self, document, filename=None, caption=None):
        self.media.append(("document", document, caption))

    async def reply_media_group(self, media):
        self.media.append(("group", [m.media for m in media], [m.caption for m in media]))


def chunk_text(text):
    return ns(choices=[ns(delta=ns(content=text, tool_calls=None))])


def chunk_tool(idx, name, args, sig=None, null_index=False):
    tc = ns(index=None if null_index else idx, id=f"c{idx}", function=ns(name=name, arguments=json.dumps(args)))
    tc.model_extra = {"extra_content": {"google": {"thought_signature": sig}}} if sig else {}
    return ns(choices=[ns(delta=ns(content=None, tool_calls=[tc]))])


def stream_of(parts, tail="stop", delay=0.0):
    """模型流：依次吐出 parts。tail=stop 正常结束，hang 卡住不动，其他值按该文案抛错（模拟断流）。"""

    class Stream:
        def __init__(self):
            self.parts = list(parts)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if delay:
                await asyncio.sleep(delay)
            if self.parts:
                return self.parts.pop(0)
            if tail == "stop":
                raise StopAsyncIteration
            if tail == "hang":
                await asyncio.sleep(999)
            raise RuntimeError(tail)

        async def close(self):
            pass

    return Stream()


def openai_adapter(create, model="local-model"):
    """OpenAI 兼容适配器，请求交给 create(messages, 是否挂了 tools) 处理。"""

    async def completions_create(**kw):
        return await create(kw["messages"], "tools" in kw)

    return OpenAIAdapter(client=ns(chat=ns(completions=ns(create=completions_create))),
                         endpoint=config.LLMEndpoint("openai", model, "", "", None))


def use_model(create):
    llm.adapter = openai_adapter(create)


def bad_request(cls, message, body=None):
    return cls(message, response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=body)


# ---- 真实的 python-telegram-bot 消息对象（给 handlers 用）----
NOW = datetime.datetime.now()
BOT_USER = User(42, "bot", True)


def group(chat_id=-7):
    return Chat(chat_id, "supergroup")


def user(uid, name, username=None):
    return User(uid, name, False, username=username)


def mention(mid, chat, from_user, text):
    """群里 @bot 提问的消息。"""
    return Message(mid, NOW, chat, from_user=from_user, text="@bot " + text,
                   entities=[MessageEntity(MessageEntity.MENTION, 0, 4)])


def update_of(msg):
    return ns(effective_message=msg)


class _File:
    async def download_as_bytearray(self):
        return bytearray(b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 8)


async def _get_file(file_id):
    return _File()


CTX = ns(bot=ns(id=42, username="bot", get_file=_get_file))


def capture_replies(monkeypatch):
    """把 handlers 里的回复生成换掉，返回每次收到的对话历史列表。"""
    from tgrok import handlers

    captured = []

    async def fake_reply(msg, history):
        captured.append(history)
        return [], ""

    monkeypatch.setattr(handlers, "stream_reply", fake_reply)
    return captured
