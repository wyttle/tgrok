# tgrok — Grok-style AI Assistant for Telegram Groups

English | [简体中文](README.zh-CN.md)

Bring the X (Twitter) @grok experience to your Telegram groups: reply to any message,
mention the bot with a question like *"is this true?"*, and it answers based on the quoted
message — powered by your own local LLM or any OpenAI-compatible API.

## Features

- **Quote & ask**: reply to a message + mention the bot — it answers with the quoted content as context (the core @grok workflow)
- **Direct questions**: mention the bot anywhere in a group
- **Follow-ups**: reply to the bot's answers to continue the conversation with full context
- **Private chat**: just message the bot directly
- **Image understanding**: with a vision-capable model, ask about photos sent in the group
- **Web search**: the model can search the internet on its own via a `web_search` tool and answer with sources (Tavily / DuckDuckGo / SearXNG)
- **Streaming replies**: answers appear progressively (typewriter style); long generations won't be cut off by gateway idle timeouts
- **Access control**: admin-managed whitelist via bot commands; unauthorized users are silently ignored
- **Bilingual**: all bot messages and the setup wizard available in English and Chinese (`BOT_LANG`)
- Works with any **OpenAI-compatible endpoint** (LM Studio / vLLM / llama.cpp server / Ollama / official OpenAI API)

## 1. Create a Telegram Bot

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`
2. Pick a name and a username (must end with `bot`, e.g. `my_local_ai_bot`)
3. Save the **bot token** (looks like `123456:ABC-xxxx`)
4. **Disable privacy mode** (important — otherwise the bot may not see mentions in groups):
   - Send `/setprivacy` to BotFather → select your bot → choose **Disable**
   - ⚠️ If the bot was already in a group before this change, **remove and re-add it** for the change to take effect

## 2. Start your LLM server

LM Studio example: open the **Developer / Local Server** page, load a model and start the
server (default `http://localhost:1234/v1`), note the model name shown on the page.

vLLM example:

```bash
vllm serve Qwen/Qwen2.5-14B-Instruct --port 8000
# endpoint: http://localhost:8000/v1, model name: Qwen/Qwen2.5-14B-Instruct
```

## 3. Configure and run

```bash
# install dependencies (a virtualenv is recommended)
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# interactive setup wizard: generates .env step by step
# validates the token online and lists available models from your endpoint
python configure.py

# (or configure manually: copy .env.example to .env and edit)

# run
python bot.py
```

Add the bot to a group, then reply to any message with `@your_bot_username is this true?`.

## Configuration (.env)

| Variable | Description | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | token from BotFather | (required) |
| `BOT_LANG` | bot message language: `en` or `zh` | `zh` |
| `LLM_BASE_URL` | OpenAI-compatible endpoint | `http://localhost:1234/v1` |
| `LLM_MODEL` | model name | `local-model` |
| `LLM_API_KEY` | anything works for most local servers | `not-needed` |
| `LLM_USER_AGENT` | custom User-Agent (some cloud gateways validate it) | SDK default |
| `SYSTEM_PROMPT` | system prompt | built-in default |
| `MAX_TOKENS` | max tokens per reply | `1024` |
| `MAX_HISTORY` | messages kept per conversation | `20` |
| `ENABLE_VISION` | image understanding (vision-capable models) | `false` |
| `SEARCH_PROVIDER` | web search provider: `tavily` / `duckduckgo` / `searxng`, empty = off | (empty) |
| `TAVILY_API_KEY` | Tavily API key (required with `SEARCH_PROVIDER=tavily`) | (empty) |
| `SEARXNG_BASE_URL` | SearXNG instance URL (required with `SEARCH_PROVIDER=searxng`) | (empty) |
| `SEARCH_MAX_RESULTS` | search results fed back to the model per query | `5` |
| `ADMIN_USER_IDS` | super admin IDs (comma-separated) | (empty) |
| `ALLOWED_USER_IDS` | initial whitelist, first start only | (empty) |

## Access control

**Three modes:**

- `ADMIN_USER_IDS` and whitelist both empty: open mode — everyone can use the bot
- `ADMIN_USER_IDS` set: controlled mode — only **admins + whitelisted users**
- Unauthorized users are **silently ignored** (groups, private chat, `/start` — no feedback at all); attempts are logged

**Admin commands** (silently ignored for everyone else):

| Command | Description |
|---|---|
| `/adduser 123456789` | add user(s) to the whitelist (space-separated IDs) |
| `/deluser 123456789` | remove user(s) from the whitelist |
| `/listusers` | show the current whitelist |

In groups you can also **reply to someone's message** with `/adduser` / `/deluser` — no need
to look up their ID.

Whitelist changes apply immediately and persist to `allowed_users.json` across restarts.
`ALLOWED_USER_IDS` in `.env` is only used as a seed on first start.

To find your own user ID: message the bot `/start` (requires access), or use @userinfobot.
Set yourself as admin in `ADMIN_USER_IDS` before the first start.

## Cloud APIs / multimodal

`LLM_BASE_URL` accepts any OpenAI-compatible service, not just local models. For the official
OpenAI API: re-run `python configure.py`, set the endpoint to `https://api.openai.com/v1`,
your API key, and a model name (the bot handles the `max_completion_tokens` requirement of
newer official models automatically).

With a vision-capable model, set `ENABLE_VISION=true` (wizard step 6) to:

- reply to a photo and ask the bot: *"what is this?"*, *"is the claim in this image true?"*
- send a photo with a caption mentioning the bot
- up to 4 images per request; image files over 10MB are skipped

Note: with vision enabled, group images are sent to your configured LLM service — if that's
a cloud API, images leave your server.

## Web search

Language models have no internet access by themselves — asked about current events, they
either say so or hallucinate. With web search enabled, the bot attaches a `web_search` tool:
the model decides on its own when to search, the bot performs the search and feeds the results
back, and the model answers with source links (the message shows a 🔍 status while searching).

Set `SEARCH_PROVIDER` to pick a provider (or re-run `python configure.py`, wizard step 7):

| Provider | Extra config | Notes |
|---|---|---|
| `tavily` | `TAVILY_API_KEY` | hosted, LLM-optimized results, best quality; free tier ~1000 searches/mo ([tavily.com](https://tavily.com)) |
| `duckduckgo` | none | zero-config, no key (uses the `ddgs` package); less reliable, may get rate-limited |
| `searxng` | `SEARXNG_BASE_URL` | self-hosted metasearch, free and private; the instance must have JSON output enabled |

Notes:

- The model/backend must support **function calling** (tool calling). Mainstream cloud models
  and recent open models served by LM Studio / vLLM / llama.cpp (with `--jinja`) all do; if the
  backend rejects tools, the bot automatically falls back to plain chat.
- An answer uses at most 3 search rounds (`SEARCH_MAX_ROUNDS`), i.e. up to 4 model calls, so
  search-assisted answers are a bit slower.

## Deployment (long-running)

The bot uses **long polling** (it pulls updates from Telegram), so no public IP or domain is
needed — running it at home on the same machine as LM Studio works fine.

### Docker (recommended for servers)

```bash
cp .env.example .env   # fill in your settings (or run configure.py)
docker compose up -d --build
docker compose logs -f # watch logs
```

- `restart: unless-stopped` brings it back after crashes and server reboots
- the whitelist persists in `./data/allowed_users.json` across container rebuilds
- to reach an LLM on the host from inside the container, use `http://host.docker.internal:port/v1`
  (`localhost` won't work inside Docker)

### systemd (bare-metal Linux alternative)

`/etc/systemd/system/tgbot.service`:

```ini
[Unit]
Description=Telegram LLM Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/tgrok
ExecStart=/opt/tgrok/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now tgbot
journalctl -u tgbot -f
```

## FAQ

- **No reaction to mentions in groups**: check that privacy mode is disabled (step 4 above)
  and that the bot was re-added to the group afterwards.
- **The model says it can't access the internet**: by default it really can't. Set
  `SEARCH_PROVIDER` to enable web search (see "Web search" above).
- **"Failed to call the model"**: verify the LLM server is running and `LLM_BASE_URL` /
  `LLM_MODEL` match the server.
- **Conversations forgotten after restart**: history lives in memory and is lost on restart;
  replying to the bot still works, with only its last answer as context.
