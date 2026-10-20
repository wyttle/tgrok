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
- **Image understanding**: with a vision-capable model, ask about photos and stickers sent in the group. Replying to the bot with a sticker gets a reply even without vision (the model sees the sticker's emoji)
- **Web search**: the model can search the internet on its own via a `web_search` tool and answer with sources (Tavily / DuckDuckGo / SearXNG / Serper)
- **Calculator**: a built-in `calculate` tool lets the model check arithmetic, percentages, unit conversions, and date math instead of guessing (no setup needed)
- **Long-term memory**: remembers who's who and what the chat has talked about, within a fixed character budget (`MEMORY_MAX_CHARS`)
- **Slang glossary**: once the bot figures out a meme or metaphor (or someone corrects it), it remembers the real meaning and uses it next time
- **Streaming replies**: answers appear progressively (typewriter style); long generations won't be cut off by gateway idle timeouts
- **Access control**: admin-managed whitelist via bot commands; unauthorized users are silently ignored
- **Bilingual**: all bot messages and the setup wizard available in English and Chinese (`BOT_LANG`)
- **Four LLM protocols**: select OpenAI-compatible Chat Completions, the OpenAI Responses API, native Gemini, or native Claude with `LLM_PROTOCOL`; native Claude keeps Anthropic thinking and tool-use semantics
- Works with local servers, cloud gateways, official OpenAI, Google Gemini, and Anthropic APIs

## 1. Create a Telegram Bot

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`
2. Pick a name and a username (must end with `bot`, e.g. `my_local_ai_bot`)
3. Save the **bot token** (looks like `123456:ABC-xxxx`)
4. **Disable privacy mode** (important — otherwise the bot may not see mentions in groups):
   - Send `/setprivacy` to BotFather → select your bot → choose **Disable**
   - Warning: If the bot was already in a group before this change, **remove and re-add it** for the change to take effect

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
# validates the token online and lists available models for the primary and fallback
# models using each one's protocol (OpenAI-compatible / Claude / Gemini)
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
| `LLM_PROTOCOL` | reply-model protocol: `openai`, `responses`, `gemini`, or `claude` | `openai` |
| `LLM_BASE_URL` | OpenAI-compatible, Responses, or Claude endpoint; Claude accepts a root URL or a URL ending in `/v1` | `http://localhost:1234/v1` |
| `GEMINI_BASE_URL` | native Gemini endpoint for Gemini replies and grounding; empty = official Google endpoint | (empty) |
| `LLM_MODEL` | model name | `local-model` |
| `LLM_API_KEY` | API key; anything works for most local servers | `not-needed` |
| `LLM_USER_AGENT` | custom User-Agent (some cloud gateways validate it) | SDK default |
| `SYSTEM_PROMPT` | system prompt; keep it on one line and use `\n` for line breaks (double quotes recommended) | built-in default |
| `REPLY_SINGLE_PARAGRAPH` | reply as one paragraph: adds a prompt rule and merges paragraphs, lists, and headings before display (code blocks kept) | `false` |
| `MAX_TOKENS` | max tokens per reply | `1024` |
| `LLM_TEMPERATURE` | sampling temperature; empty = backend default, auto-dropped if rejected | (empty) |
| `LLM_TOP_P` | nucleus sampling; empty = backend default, auto-dropped if rejected | (empty) |
| `LLM_EXTRA_BODY` | JSON object passed through by OpenAI-compatible, Responses, and native Claude requests; ignored by native Gemini | (empty) |
| `LLM_FALLBACK_MODEL` | fallback model, used for a reply when the primary model errors before producing text; empty = disabled | (empty) |
| `LLM_FALLBACK_PROTOCOL` / `LLM_FALLBACK_BASE_URL` / `LLM_FALLBACK_API_KEY` | fallback protocol, endpoint, and key; empty = same as the primary model | (empty) |
| `LLM_FALLBACK_EXTRA_BODY` | extra request params for the fallback model (not inherited from `LLM_EXTRA_BODY`) | (empty) |
| `MAX_HISTORY` | messages kept per conversation | `20` |
| `MEMORY_ENABLED` | per-chat long-term memory (digest + recent questions injected into new conversations as background) | `true` |
| `MEMORY_MAX_CHARS` | max characters of memory injected per conversation; the digest uses at most half | `800` |
| `MEMORY_FRESH_DAYS` | passing topics in the digest stop being injected once last mentioned more than this many days ago | `14` |
| `MEMORY_OBSERVE_CHATS` | comma-separated group chat ids where the bot silently reads all messages for memory | (empty) |
| `MEMORY_OBSERVE_INTERVAL` | minutes between summaries of overheard messages, per group | `60` |
| `MEMORY_OBSERVE_MAX_CHARS` | max characters of overheard messages buffered per group; the oldest are dropped | `12000` |
| `SLANG_ENABLED` | slang glossary: the model saves memes it figured out with `remember_slang`; matching terms get their meaning attached | `true` |
| `SLANG_MAX_ENTRIES` | max glossary entries; the least recently used are dropped | `500` |
| `ENABLE_VISION` | image understanding (vision-capable models) | `false` |
| `MAX_IMAGES` | images attached per request | `4` |
| `SEARCH_PROVIDER` | comma-separated providers: `tavily`, `duckduckgo`, `searxng`, `serper`; empty = off | (empty) |
| `TAVILY_API_KEY` | Tavily API key | (empty) |
| `SERPER_API_KEY` | Serper API key | (empty) |
| `SEARXNG_BASE_URL` | SearXNG instance URL | (empty) |
| `GEMINI_SEARCH_MODEL` | dedicated Gemini grounding model for hybrid search | (empty) |
| `GEMINI_API_KEY` | Gemini/grounding key; empty = reuse `LLM_API_KEY` | (empty) |
| `SEARCH_MAX_RESULTS` | search results fed back to the model per query | `5` |
| `FETCH_CHAR_LIMIT` | page-text characters fed back per `open_url` call | `3500` |
| `JINA_FALLBACK` | use Jina Reader when direct page extraction fails | `true` |
| `JINA_API_KEY` | optional Jina API key for higher rate limits | (empty) |
| `RESEARCH_MODELS` | omp model names (comma-separated) for the `research` tool; each becomes one researcher; empty = off | (empty) |
| `RESEARCH_TIMEOUT` | seconds each researcher may run | `150` |
| `RESEARCH_THINKING` | researcher thinking level (`omp --thinking`) | `low` |
| `RESEARCH_CONCURRENCY` | research tasks running at the same time | `2` |
| `OMP_BIN` | omp executable | `omp` |
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
| `/memory` | show this chat's memory (admins; anyone authorized in a private chat) |
| `/forget` | clear this chat's memory (same permissions) |
| `/learn term=meaning\|when` | teach a usage for this chat (the context part is optional); `/learn global ...` for all chats (authorized users) |
| `/slang` | list saved memes and expressions (admins; anyone authorized in a private chat) |
| `/unlearn term` | delete a saved expression (same permissions as `/slang`) |

Memory is stored in `memory.json` next to the whitelist file (`/data` in Docker). Each new conversation starts with the chat's memory as a background note: a model-written digest (who people are, what they want from the bot, agreements, running jokes; no one-off topics, passwords, keys, or contact details) plus the newest questions (who asked what, without the bot's old answers) that fit the budget. The note tells the model to ignore it unless someone asks about the past or the current question depends on it, so the bot doesn't drag old topics into new answers. Follow-ups reuse the same history, so memory is injected once per conversation. Every time six raw exchanges pile up, the model folds the older ones into the digest in the background (one short extra call on the primary model, falling back to the fallback model if it fails).

Memory fades on purpose. Venting, jokes, exaggeration, and passing moods ("I got liquidated, gonna jump off a roof") are not written into the digest; the bot answers them in the moment and moves on. Every injected line shows how long ago it was ("2 h ago", "yesterday", "about 3 months ago"), so the model knows what's old news. Digest lines are either tagged as lasting (who people are, lasting preferences, requests to the bot, agreements), which always stay, or passing topics, which stop being injected after `MEMORY_FRESH_DAYS` days without coming up and are deleted after 30. Recent questions older than three days are no longer injected.

**Silent observation.** List group ids in `MEMORY_OBSERVE_CHATS` (`/memory` in a group shows its id) and the bot also reads messages there that don't mention it, without replying. They are only buffered; once per `MEMORY_OBSERVE_INTERVAL` minutes per group, and only after at least 5 have piled up, the model folds them into that group's memory digest in one call. A failed summary keeps the buffer and waits for the next interval. `/memory` shows how many overheard messages are pending. Telegram only delivers ordinary group messages to bots with privacy mode off: in BotFather run `/setprivacy` → Disable and re-add the bot to the group, or make the bot a group admin.

**Slang glossary.** Chinese memes, slang, puns, and metaphors rarely mean what they literally say, and the same word can mean different things in different sentences. So the glossary doesn't map a term to one fixed meaning: each term keeps up to three usages, each with when it carries that meaning and the message where it was used that way. When the model works out a usage, from a page that explains it or because someone corrected or explained it, it saves it with the `remember_slang` tool; a correction is added as the newest usage. Whenever a later message (or the message it quotes) contains a saved term, its usages are attached as a reference, with an explicit note to judge by the current context and that the word may well be meant literally. Only terms that actually appear are attached, at most 5; single-character memes such as 典 or 急 only count in short messages. General internet memes are shared by every chat; group in-jokes stay in the group where they were learned. People can teach usages with `/learn term=meaning|when it means this`. With web search on, the bot is also told how to look up memes properly: search "X 是什么梗 / 出处", open pages that explain that specific meme (Moegirl, Zhihu, Baidu Baike, Bilibili explainers) and read them instead of trusting snippets, then check which layer the message uses. The glossary is stored in `slang.json` next to the whitelist file.

In groups you can also **reply to someone's message** with `/adduser` / `/deluser` — no need
to look up their ID.

Whitelist changes apply immediately and persist to `allowed_users.json` across restarts.
`ALLOWED_USER_IDS` in `.env` is only used as a seed on first start.

To find your own user ID: message the bot `/start` (requires access), or use @userinfobot.
Set yourself as admin in `ADMIN_USER_IDS` before the first start.

## LLM protocols and cloud APIs

Set `LLM_PROTOCOL` to choose the wire protocol:

- `openai` (default): OpenAI-compatible `/chat/completions`, using `LLM_BASE_URL`.
- `responses`: OpenAI Responses API `/responses`, using `LLM_BASE_URL`. Use it for models that only support tool calling there, such as `gpt-6.1-sol` (Chat Completions accepts those models only without tools). The bot's tools keep working; set reasoning effort with `LLM_EXTRA_BODY={"reasoning":{"effort":"low"}}`.
- `gemini`: native Gemini through the google-genai SDK, using `GEMINI_BASE_URL` (empty means Google's official endpoint). Google Search and URL context run server-side, so the bot's own tool loop is bypassed.
- `claude`: native Anthropic Messages API, using `LLM_BASE_URL`. Native thinking blocks, signatures, and tool-use semantics are preserved, and the bot's own search tools remain available.

The old `GEMINI_NATIVE_SEARCH` and `CLAUDE_NATIVE` switches are no longer read: if they are still in `.env` without `LLM_PROTOCOL`, the bot refuses to start and says which value to use. Running `python configure.py` once rewrites them as `LLM_PROTOCOL`.

**Fallback model.** Set `LLM_FALLBACK_MODEL` (or use `python configure.py`, step 8.5) to add a backup. Every reply tries the primary model first. If it errors before any text has been streamed (HTTP errors, quota, timeouts, unknown model, and so on), the rest of that reply runs on the fallback model, the progress line says so while switching, and the reply ends with a note naming the fallback model. The note is display-only and is not stored in the follow-up history. If the primary already streamed text before failing, the partial answer is kept as before. The fallback can use a different protocol, endpoint, and key; empty fields reuse the primary model's.

For the official OpenAI API, set `LLM_BASE_URL` to `https://api.openai.com/v1` and use `LLM_PROTOCOL=openai`, or `LLM_PROTOCOL=responses` for models that need the Responses API for tool calling. The bot automatically handles newer models that require `max_completion_tokens`.

`LLM_TEMPERATURE` and `LLM_TOP_P` are optional. `LLM_EXTRA_BODY` is a JSON object for vendor-specific fields such as reasoning effort or thinking settings; it is passed through by OpenAI-compatible, Responses, and native Claude requests, but not native Gemini. If a backend explicitly rejects these optional fields, the adapter retries without them and keeps them disabled for the current process.

You don't have to write that JSON by hand: when `python configure.py` (step 8) sees a reasoning GPT model (o-series, GPT-5 and later), it offers a reasoning-effort menu, accepts a custom value, writes the field the chosen protocol expects (`reasoning.effort` for Responses, `reasoning_effort` for Chat Completions), raises a too-low `MAX_TOKENS` default to 4096, and skips temperature/top_p. Other extra params can be entered as `key=value` pairs, with dots for nested keys, e.g. `thinking.type=enabled, thinking.budget_tokens=1000`.

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
back, and the model retells the findings in its own words, adding one or two key source links when useful (the message shows a search status while searching).

Set `SEARCH_PROVIDER` to pick a provider (or re-run `python configure.py`, wizard step 7):

| Provider | Extra config | Notes |
|---|---|---|
| `tavily` | `TAVILY_API_KEY` | hosted, LLM-optimized results, best quality; free tier ~1000 searches/mo ([tavily.com](https://tavily.com)) |
| `duckduckgo` | none | zero-config, no key (uses the `ddgs` package); less reliable, may get rate-limited |
| `searxng` | `SEARXNG_BASE_URL` | self-hosted metasearch, free and private; the instance must have JSON output enabled |
| `serper` | `SERPER_API_KEY` | hosted Google results; can be combined with other providers |

Notes:

- The model/backend must support **function calling** (tool calling). Mainstream cloud models
  and recent open models served by LM Studio / vLLM / llama.cpp (with `--jinja`) all do; if the
  backend rejects tools, the bot automatically falls back to plain chat.
- An answer uses at most 3 tool rounds (`SEARCH_MAX_ROUNDS`, shared by search and `calculate`),
  i.e. up to 4 model calls, so tool-assisted answers are a bit slower.

## Deep research (omp)

For questions that need real checking (memes and niche jargon, disputed claims, exact numbers and dates), the
model can call a `research` tool. The task goes to every model in `RESEARCH_MODELS` at once; each runs as a headless
[omp](https://omp.sh) process that may only search and read public web pages, reads the actual pages, and returns key
facts with the sources it read. All reports go back to the reply model, which compares them and decides what to
trust. One call takes about 30 s to 2 min; the reply model keeps using `web_search` for quick lookups.

Setup:

1. omp must be installed where the bot runs. The Docker image installs it (pinned by the `OMP_VERSION` build arg;
   `--build-arg OMP_VERSION=` skips it).
2. Give omp the models' endpoint and key. With Docker, `./omp` is mounted as omp's config directory; create
   `./omp/agent/models.yml`, for example:

   ```yaml
   providers:
     cpa:
       baseUrl: https://your-relay.example.com
       apiKey: RESEARCH_API_KEY   # read from this environment variable; put RESEARCH_API_KEY=... in .env
       authHeader: true
       api: anthropic-messages
       discovery:
         type: openai-models-list
   ```

3. Set `RESEARCH_MODELS=cpa/claude-opus-5-5,cpa/gemini-3.8-flash-high` (omp model names, see `omp models`).

Safety: researchers only get omp's `web_search` and `read` tools, and a hook (`tgrok/tools/research_hook.ts`) blocks
`read` on anything but public http(s) pages, so prompt injection can't make them read local files, omp's own
credentials, or internal addresses. They run in an empty temporary directory and are killed on timeout or when the
reply is cancelled. Hostnames that resolve to private IPs (DNS rebinding) are not caught.

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
- **"Failed to call the model"**: verify the model service is running and the selected protocol's address and model match it: `LLM_BASE_URL` for OpenAI/Claude, `GEMINI_BASE_URL` for native Gemini, plus `LLM_MODEL`.
- **Conversations forgotten after restart**: history lives in memory and is lost on restart;
  replying to the bot still works, with only its last answer as context.
