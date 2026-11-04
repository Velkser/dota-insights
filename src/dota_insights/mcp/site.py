"""The public pages: the landing page at ``/`` and the privacy policy at ``/privacy``.

Both are self-contained HTML (inline CSS, no JavaScript) in English and Russian. The language
comes from ``?lang=en|ru``, else the ``Accept-Language`` header, else English. The tool list on
the landing page is rendered from ``catalog``, the same data as the README's tool table.
"""

from __future__ import annotations

from html import escape

from starlette.requests import Request
from starlette.responses import HTMLResponse

from dota_insights.mcp.catalog import Domain, cached_catalog

LANGS = ("en", "ru")
SITE_URL = "https://dota-insights.vercel.app"
MCP_URL = f"{SITE_URL}/mcp"
REPO = "https://github.com/Velkser/dota-insights"
DOCS = f"{REPO}/blob/main/docs"


def pick_lang(request: Request) -> str:
    """``?lang=`` if it names a supported language, else the best ``Accept-Language`` match."""
    asked = request.query_params.get("lang", "").lower()
    if asked in LANGS:
        return asked
    best, best_q = "en", 0.0
    for part in request.headers.get("accept-language", "").split(","):
        tag, _, params = part.strip().partition(";")
        lang = tag.strip().lower().split("-")[0]
        if lang not in LANGS:
            continue
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        if q > best_q:
            best, best_q = lang, q
    return best


CSS = """
:root{--bg:#fbfaf8;--fg:#1d1d1f;--muted:#5f5f66;--card:#fff;--line:#e4e1dc;--accent:#b3261e;
--code:#f2efe9;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececef;--muted:#a3a3ad;
--card:#1c1c20;--line:#2e2e35;--accent:#ff7a6e;--code:#232328}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main,header,footer{max-width:960px;margin:0 auto;padding:0 16px}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px 16px;
padding-top:20px}
header .brand{font-weight:700;color:var(--fg);text-decoration:none}
nav{display:flex;flex-wrap:wrap;gap:4px 14px}
a{color:var(--accent)}
h1{font-size:clamp(1.8rem,5vw,2.6rem);line-height:1.15;margin:40px 0 12px}
h2{margin:48px 0 12px;padding-top:8px;border-top:1px solid var(--line)}
h3{margin:24px 0 8px}
.lead{font-size:1.15rem;color:var(--muted);max-width:42em}
code,pre{font:0.9em/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--code);
border-radius:6px}
code{padding:1px 5px}
pre{padding:12px 14px;overflow-x:auto;white-space:pre}
pre code{padding:0;background:none}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:8px 0}
summary{cursor:pointer;padding:10px 16px;font-weight:600}
summary span{color:var(--muted);font-weight:400}
.tools{list-style:none;margin:0;padding:0 16px 12px}
.tools li{padding:8px 0;border-top:1px solid var(--line)}
.tools .ex{display:block;color:var(--muted);font-style:italic}
.muted{color:var(--muted)}
ul.plain{padding-left:20px}
.notice{border-left:4px solid var(--accent);padding:10px 14px;background:var(--card)}
footer{margin-top:56px;padding-bottom:40px;color:var(--muted);font-size:0.9rem}
"""

DOMAIN_NAMES = {
    "ru": {
        "analysis": "Анализ",
        "builds": "Сборки",
        "charts": "Графики",
        "economy": "Экономика",
        "farming": "Фарм",
        "general": "Общая статистика",
        "heroes": "Герои",
        "items": "Предметы",
        "live": "Live-матчи",
        "matches": "Матчи",
        "matchups": "Матчапы и драфт",
        "meta": "Сервер",
        "players": "Игроки",
        "pro": "Про-сцена",
        "reference": "Справочник",
        "trends": "Тренды и патчи",
    },
}

EXAMPLE_QUESTIONS = {
    "en": [
        "What should I build on Juggernaut this patch?",
        "What counters Huskar in Ancient–Divine?",
        "Is 600 GPM good for an Anti-Mage?",
        "What happened in match 8012345678, and which fight decided it?",
        "Where am I below my rank's average? (account 56939869)",
        "We have Axe and Lion against Sniper and Drow; what should I pick mid?",
        "Which heroes rose the most after the last patch?",
        "How often do teams win from 10k behind at 25 minutes?",
    ],
    "ru": [
        "Что собирать на Джаггернауте в этом патче?",
        "Кто контрит Хускара на Ancient–Divine?",
        "600 GPM на Анти-Маге — это хорошо?",
        "Что произошло в матче 8012345678 и какая драка всё решила?",
        "В чём я хуже среднего по своему рангу? (аккаунт 56939869)",
        "У нас Акс и Лион против Снайпера и Дров — кого брать на мид?",
        "Какие герои сильнее всего поднялись после последнего патча?",
        "Как часто команды выигрывают, отставая на 10k к 25-й минуте?",
    ],
}

T = {
    "en": {
        "title": "Dota Insights — Dota 2 statistics for your AI assistant",
        "description": "A free public MCP server with Dota 2 statistics tools for Claude, "
        "Cursor and other AI clients.",
        "nav_tools": "Tools",
        "nav_connect": "Connect",
        "nav_privacy": "Privacy",
        "h1": "Dota 2 statistics for your AI assistant",
        "lead": "Dota Insights is a free public MCP server. Connect it to Claude Code, Claude "
        "Desktop, Cursor or another MCP client and ask about heroes, items, builds, matchups, "
        "your matches and the pro scene in plain language. Answers come from real match data "
        "from OpenDota and STRATZ.",
        "endpoint": "MCP endpoint (Streamable HTTP):",
        "what_h": "What is Dota Insights",
        "what": [
            'An <a href="https://modelcontextprotocol.io">MCP</a> server: your AI client calls '
            "its tools, gets numbers, and explains them to you.",
            "Every result carries its sample size, patch, filters, confidence intervals, "
            "caveats and data sources, so the assistant can say how sure an answer is.",
            "When there are too few matches the tool says <code>insufficient_data</code> and "
            "how to widen the filters, instead of making up a number.",
            "No sign-up and no API key. It is open source.",
        ],
        "tools_h": "Features: MCP tools",
        "tools_intro": "{n} tools in {d} groups, listed from the running server. Most aggregate "
        "tools also take a <code>filters</code> object (patch, rank bracket, position, game "
        "mode, minimum sample). The server also has {p} ready-made prompts.",
        "examples_h": "Example questions",
        "examples_intro": "Ask in your own words, in any language. For example:",
        "connect_h": "Connect",
        "cc_h": "Claude Code",
        "cc_text": "Run once in a terminal. Add <code>--scope user</code> to use it in every "
        "project; check with <code>claude mcp list</code> or <code>/mcp</code> in a session.",
        "cd_h": "Claude Desktop",
        "cd_text": "Settings → Connectors → <b>Add custom connector</b>, name it "
        "<i>Dota Insights</i> and paste the URL:",
        "cd_alt": "If your plan or version has no custom connectors, use the "
        "<code>mcp-remote</code> bridge in <code>claude_desktop_config.json</code> (Settings → "
        "Developer → Edit Config; needs Node.js), then restart Claude Desktop:",
        "cursor_h": "Cursor and other MCP clients",
        "cursor_text": "Cursor: add to <code>~/.cursor/mcp.json</code> (or "
        "<code>.cursor/mcp.json</code> in a project):",
        "other_text": "Any client that supports remote <b>Streamable HTTP</b> servers takes the "
        "URL above. Clients that only start local (stdio) servers can use "
        "<code>npx -y mcp-remote {url}</code> as the command.",
        "guides": "Step-by-step guides with troubleshooting:",
        "data_h": "Data sources and limitations",
        "data": [
            "<b>OpenDota, free tier.</b> 60 requests a minute and 3,000 a day, shared by "
            "everyone using this server. Results are cached, but a cold question over many heroes "
            "or matches can hit the limit; the tool then returns an error with a retry hint.",
            "<b>Only about 8% of public matches are replay-parsed.</b> Timelines, teamfights, "
            "wards, lanes, purchase logs and per-minute farm exist only for parsed matches; "
            "aggregates over parsed data rest on smaller samples.",
            "<b>No separate Immortal bracket in OpenDota</b>: it is folded into Divine. "
            "Rank-filtered hero, item and build statistics come from STRATZ, which does "
            "separate Immortal.",
            "<b>STRATZ</b> data is credited in every result that uses it: data from "
            '<a href="https://stratz.com">STRATZ</a>.',
            "<b>Sample sizes and caveats.</b> Win rates of items, builds and timings describe "
            "the games where players made that choice; they show correlation, not causation. "
            "Small samples are flagged, not hidden.",
            "<b>Public profiles only.</b> Player tools need a public match history "
            "(“Expose Public Match Data” in Dota 2); private profiles return nothing.",
            "Our own database is filled from public ranked matches; it groups lettered patches "
            "under their major patch.",
        ],
        "docs_h": "Docs",
        "docs": [
            ("README: tools, limitations, development", f"{REPO}#readme"),
            ("Connection guides", f"{DOCS}/connect/"),
            ("Tool conventions", f"{DOCS}/architecture/tools.md"),
            ("Server status (JSON)", "/status"),
            ("Privacy policy", "/privacy"),
            ("Source code on GitHub", REPO),
        ],
        "footer": 'Data from <a href="https://www.opendota.com">OpenDota</a> and '
        '<a href="https://stratz.com">STRATZ</a>. Dota 2 is a registered trademark of Valve '
        "Corporation; this project is not affiliated with or endorsed by Valve.",
        "other_lang": "Русский",
    },
    "ru": {
        "title": "Dota Insights — статистика Dota 2 для вашего ИИ-ассистента",
        "description": "Бесплатный публичный MCP-сервер со статистикой Dota 2 для Claude, "
        "Cursor и других ИИ-клиентов.",
        "nav_tools": "Инструменты",
        "nav_connect": "Подключение",
        "nav_privacy": "Конфиденциальность",
        "h1": "Статистика Dota 2 для вашего ИИ-ассистента",
        "lead": "Dota Insights — бесплатный публичный MCP-сервер. Подключите его к Claude Code, "
        "Claude Desktop, Cursor или другому MCP-клиенту и спрашивайте обычным языком о героях, "
        "предметах, сборках, матчапах, своих матчах и про-сцене. Ответы строятся на реальных "
        "данных матчей из OpenDota и STRATZ.",
        "endpoint": "MCP-эндпоинт (Streamable HTTP):",
        "what_h": "Что такое Dota Insights",
        "what": [
            '<a href="https://modelcontextprotocol.io">MCP</a>-сервер: ваш ИИ-клиент вызывает '
            "его инструменты, получает цифры и объясняет их вам.",
            "Каждый результат содержит размер выборки, патч, фильтры, доверительные интервалы, "
            "оговорки и источники данных, поэтому ассистент может сказать, насколько ответ "
            "надёжен.",
            "Если матчей слишком мало, инструмент возвращает <code>insufficient_data</code> и "
            "подсказку, как расширить фильтры, а не выдумывает число.",
            "Без регистрации и без API-ключа. Открытый исходный код.",
        ],
        "tools_h": "Возможности: MCP-инструменты",
        "tools_intro": "{n} инструментов в {d} группах — список берётся с работающего сервера. "
        "Большинство агрегатных инструментов принимают объект <code>filters</code> (патч, "
        "ранг, позиция, режим игры, минимальная выборка). Готовых промптов на сервере: {p}. "
        "Описания инструментов — на английском, как их видит ИИ-клиент.",
        "examples_h": "Примеры вопросов",
        "examples_intro": "Спрашивайте своими словами, на любом языке. Например:",
        "connect_h": "Подключение",
        "cc_h": "Claude Code",
        "cc_text": "Выполните один раз в терминале. Добавьте <code>--scope user</code>, чтобы "
        "сервер был доступен во всех проектах; проверка — <code>claude mcp list</code> или "
        "<code>/mcp</code> в сессии.",
        "cd_h": "Claude Desktop",
        "cd_text": "Settings → Connectors → <b>Add custom connector</b>, назовите его "
        "<i>Dota Insights</i> и вставьте URL:",
        "cd_alt": "Если в вашем тарифе или версии нет пользовательских коннекторов, используйте "
        "мост <code>mcp-remote</code> в <code>claude_desktop_config.json</code> (Settings → "
        "Developer → Edit Config; нужен Node.js) и перезапустите Claude Desktop:",
        "cursor_h": "Cursor и другие MCP-клиенты",
        "cursor_text": "Cursor: добавьте в <code>~/.cursor/mcp.json</code> (или "
        "<code>.cursor/mcp.json</code> в проекте):",
        "other_text": "Любой клиент с поддержкой удалённых серверов <b>Streamable HTTP</b> "
        "принимает URL выше. Клиентам, которые запускают только локальные (stdio) серверы, "
        "укажите команду <code>npx -y mcp-remote {url}</code>.",
        "guides": "Пошаговые инструкции с разбором проблем (на английском):",
        "data_h": "Источники данных и ограничения",
        "data": [
            "<b>OpenDota, бесплатный тариф.</b> 60 запросов в минуту и 3000 в день на всех "
            "пользователей сервера. Результаты кэшируются, но «холодный» вопрос по многим героям "
            "или матчам может упереться в лимит — тогда инструмент вернёт ошибку с подсказкой, "
            "когда повторить.",
            "<b>Распарсено лишь около 8% публичных матчей.</b> Таймлайны, драки, варды, лайнинг, "
            "покупки и фарм по минутам есть только у распарсенных матчей, а агрегаты по ним "
            "строятся на меньших выборках.",
            "<b>В OpenDota нет отдельного ранга Immortal</b> — он объединён с Divine. "
            "Статистика по рангам для героев, предметов и сборок берётся из STRATZ, где Immortal "
            "выделен.",
            "<b>STRATZ</b> указывается в каждом результате, где используются его данные: "
            'данные <a href="https://stratz.com">STRATZ</a>.',
            "<b>Выборки и оговорки.</b> Винрейт предметов, сборок и таймингов описывает игры, в "
            "которых игроки сделали такой выбор: это корреляция, а не причина победы. Малые "
            "выборки помечаются, а не скрываются.",
            "<b>Только открытые профили.</b> Инструментам по игрокам нужна публичная история "
            "матчей («Expose Public Match Data» в Dota 2); по закрытым профилям данных нет.",
            "Наша база данных заполняется из публичных рейтинговых матчей; буквенные патчи "
            "объединяются с основным (7.39a, 7.39b → 7.39).",
        ],
        "docs_h": "Документация",
        "docs": [
            ("README: инструменты, ограничения, разработка", f"{REPO}#readme"),
            ("Инструкции по подключению", f"{DOCS}/connect/"),
            ("Соглашения по инструментам", f"{DOCS}/architecture/tools.md"),
            ("Статус сервера (JSON)", "/status"),
            ("Политика конфиденциальности", "/privacy?lang=ru"),
            ("Исходный код на GitHub", REPO),
        ],
        "footer": 'Данные <a href="https://www.opendota.com">OpenDota</a> и '
        '<a href="https://stratz.com">STRATZ</a>. Dota 2 — зарегистрированная торговая марка '
        "Valve Corporation; проект не связан с Valve и не одобрен ею.",
        "other_lang": "English",
    },
}

GUIDES = [
    ("Claude Code", "claude-code.md"),
    ("Claude Desktop", "claude-desktop.md"),
    ("Cursor", "cursor.md"),
    ("VS Code, Windsurf, mcp-remote", "other-clients.md"),
]

CLAUDE_CODE_CMD = f"claude mcp add --transport http dota-insights {MCP_URL}"
DESKTOP_JSON = f"""{{
  "mcpServers": {{
    "dota-insights": {{
      "command": "npx",
      "args": ["-y", "mcp-remote", "{MCP_URL}"]
    }}
  }}
}}"""
CURSOR_JSON = f"""{{
  "mcpServers": {{
    "dota-insights": {{
      "url": "{MCP_URL}"
    }}
  }}
}}"""


def _pre(text: str) -> str:
    return f"<pre><code>{escape(text)}</code></pre>"


def _page(lang: str, path: str, title: str, description: str, body: str) -> str:
    t = T[lang]
    other = "ru" if lang == "en" else "en"
    nav = (
        f'<a href="/?lang={lang}#tools">{t["nav_tools"]}</a>'
        f'<a href="/?lang={lang}#connect">{t["nav_connect"]}</a>'
        f'<a href="/privacy?lang={lang}">{t["nav_privacy"]}</a>'
        f'<a href="{path}?lang={other}" hreflang="{other}" lang="{other}">{t["other_lang"]}</a>'
    )
    alternates = "".join(
        f'<link rel="alternate" hreflang="{code}" href="{SITE_URL}{path}?lang={code}">'
        for code in LANGS
    )
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        f'<meta name="description" content="{escape(description)}">'
        f"{alternates}<style>{CSS}</style></head><body>"
        f'<header><a class="brand" href="/?lang={lang}">Dota Insights</a><nav>{nav}</nav></header>'
        f"<main>{body}</main><footer><p>{t['footer']}</p></footer></body></html>"
    )


def _tools_html(lang: str, domains: tuple[Domain, ...]) -> str:
    names = DOMAIN_NAMES.get(lang, {})
    parts = []
    for domain in domains:
        label = escape(names.get(domain.name, domain.name.capitalize()))
        items = []
        for tool in domain.tools:
            example = f'<span class="ex">“{escape(tool.example)}”</span>' if tool.example else ""
            items.append(
                f'<li id="tool-{tool.name}"><code>{tool.name}</code> — '
                f"{escape(tool.purpose)}{example}</li>"
            )
        parts.append(
            f"<details><summary>{label} <span>· {len(domain.tools)}</span></summary>"
            f'<ul class="tools">{"".join(items)}</ul></details>'
        )
    return "".join(parts)


def landing_html(lang: str, domains: tuple[Domain, ...], prompts: int) -> str:
    t = T[lang]
    total = sum(len(d.tools) for d in domains)
    what = "".join(f"<li>{item}</li>" for item in t["what"])
    examples = "".join(f'<div class="card">“{escape(q)}”</div>' for q in EXAMPLE_QUESTIONS[lang])
    data = "".join(f"<li>{item}</li>" for item in t["data"])
    docs = "".join(f'<li><a href="{href}">{escape(label)}</a></li>' for label, href in t["docs"])
    guides = "".join(
        f'<li><a href="{DOCS}/connect/{file}">{escape(label)}</a></li>' for label, file in GUIDES
    )
    body = (
        f'<h1>{t["h1"]}</h1><p class="lead">{t["lead"]}</p>'
        f"<p>{t['endpoint']}</p>{_pre(MCP_URL)}"
        f'<h2 id="about">{t["what_h"]}</h2><ul class="plain">{what}</ul>'
        f'<h2 id="tools">{t["tools_h"]}</h2>'
        f"<p>{t['tools_intro'].format(n=total, d=len(domains), p=prompts)}</p>"
        f"{_tools_html(lang, domains)}"
        f'<h2 id="examples">{t["examples_h"]}</h2><p>{t["examples_intro"]}</p>'
        f'<div class="grid">{examples}</div>'
        f'<h2 id="connect">{t["connect_h"]}</h2>'
        f'<h3 id="claude-code">{t["cc_h"]}</h3>{_pre(CLAUDE_CODE_CMD)}<p>{t["cc_text"]}</p>'
        f'<h3 id="claude-desktop">{t["cd_h"]}</h3><p>{t["cd_text"]}</p>{_pre(MCP_URL)}'
        f"<p>{t['cd_alt']}</p>{_pre(DESKTOP_JSON)}"
        f'<h3 id="cursor">{t["cursor_h"]}</h3><p>{t["cursor_text"]}</p>{_pre(CURSOR_JSON)}'
        f"<p>{t['other_text'].format(url=MCP_URL)}</p>"
        f'<p>{t["guides"]}</p><ul class="plain">{guides}</ul>'
        f'<h2 id="data">{t["data_h"]}</h2><ul class="plain">{data}</ul>'
        f'<h2 id="docs">{t["docs_h"]}</h2><ul class="plain">{docs}</ul>'
    )
    return _page(lang, "/", t["title"], t["description"], body)


# Placeholders the owner replaces before relying on the policy.
CONTACT = "[CONTACT EMAIL — owner to fill in]"
OPERATOR = "[OPERATOR NAME AND COUNTRY — owner to fill in]"
UPDATED = "[DATE — owner to fill in]"

PRIVACY = {
    "en": f"""
<h1>Privacy policy</h1>
<p class="notice"><b>Template.</b> This policy describes how the code in this repository
handles data. The owner of this deployment must review it, fill in the bracketed fields and
check it against the laws that apply to them. It is not legal advice.</p>
<p class="muted">Operator: {OPERATOR}. Contact: {CONTACT}. Last updated: {UPDATED}.</p>

<h2>Summary</h2>
<ul class="plain">
<li>There are no accounts, sign-ups or cookies, and the pages use no tracking scripts.</li>
<li>The server answers questions with <b>public</b> Dota 2 match and profile data from
OpenDota and STRATZ.</li>
<li>To enforce rate limits the server sees your IP address; it stores only a short hash of it,
for at most two days.</li>
</ul>

<h2>What is processed</h2>
<h3>Public match and profile data</h3>
<p>Tools fetch public data from <a href="https://www.opendota.com">OpenDota</a> and
<a href="https://stratz.com">STRATZ</a>: matches, heroes, items, and, for players whose
Dota 2 profile exposes public match data, account IDs, persona names, ranks and match
histories. Results are cached for minutes to days. A collector also stores public ranked
matches in our database (match and per-player statistics with account IDs) to build
aggregate statistics. Anonymous players (who hide their profile) are not identified.</p>
<h3>Your questions and requests</h3>
<p>Your AI client sends tool calls (for example a hero name or an account ID) to the server.
The server logs each call: tool name, duration, status, data sources, cache use and whether a
rate limit applied. It does not log your IP address in these lines. What you type to your AI
assistant stays between you and that assistant's provider; the server receives only the tool
calls.</p>
<h3>IP addresses and rate limiting</h3>
<p>Each client is limited to a number of tool calls per minute and per day. The server reads
the caller's IP address from the request, keeps only the first 16 hex characters of its SHA-256
hash as the counter key, and lets each counter expire after twice its window (at most two days).
The hosting provider (Vercel) records standard request logs, including IP addresses, and keeps
them under its own retention policy. [Owner: state the retention of your Vercel log plan.]</p>

<h2>Third parties</h2>
<ul class="plain">
<li><b>Vercel</b> (hosting, request logs) —
<a href="https://vercel.com/legal/privacy-policy">privacy policy</a>.</li>
<li><b>Neon</b> (PostgreSQL database with collected public match data) —
<a href="https://neon.com/privacy-policy">privacy policy</a>.</li>
<li><b>Upstash</b> (Redis: cache, rate-limit counters with hashed IPs) —
<a href="https://upstash.com/trust/privacy.pdf">privacy policy</a>.</li>
<li><b>OpenDota</b> and <b>STRATZ</b> (sources of public Dota 2 data; the server calls their
APIs, not your browser) — <a href="https://www.opendota.com">opendota.com</a>,
<a href="https://stratz.com">stratz.com</a>.</li>
</ul>
<p>[Owner: list the regions where your Vercel, Neon and Upstash resources run.]</p>

<h2>Removing a player's data</h2>
<p>To stop new matches from being public, turn off “Expose Public Match Data” in Dota 2
settings. OpenDota and STRATZ keep their own copies; ask them directly to remove data there.
To have your account's data removed from this server's database and cache, email {CONTACT}
with your Steam account ID (32-bit Dota ID). We will delete the stored per-player rows for that
account and exclude it from future collection within [N] days. [Owner: set the time frame and
describe how you verify the request.]</p>

<h2>Children</h2>
<p>The service is not directed at children and collects no data that identifies a person beyond
public game profiles.</p>

<h2>Changes</h2>
<p>Changes to this policy are published on this page and in the project's repository.</p>
""",
    "ru": f"""
<h1>Политика конфиденциальности</h1>
<p class="notice"><b>Шаблон.</b> Политика описывает, как с данными работает код этого
репозитория. Владелец развёртывания должен проверить её, заполнить поля в квадратных скобках и
сверить с применимым к нему законодательством. Это не юридическая консультация.</p>
<p class="muted">Оператор: {OPERATOR}. Контакт: {CONTACT}. Обновлено: {UPDATED}.</p>

<h2>Коротко</h2>
<ul class="plain">
<li>Нет аккаунтов, регистрации и cookie; на страницах нет скриптов отслеживания.</li>
<li>Сервер отвечает на вопросы по <b>публичным</b> данным матчей и профилей Dota 2 из OpenDota
и STRATZ.</li>
<li>Для лимитов запросов сервер видит ваш IP-адрес; хранится только короткий хэш, не дольше
двух дней.</li>
</ul>

<h2>Какие данные обрабатываются</h2>
<h3>Публичные данные матчей и профилей</h3>
<p>Инструменты получают публичные данные из <a href="https://www.opendota.com">OpenDota</a> и
<a href="https://stratz.com">STRATZ</a>: матчи, герои, предметы, а для игроков с открытой
историей матчей — ID аккаунта, никнейм, ранг и историю матчей. Результаты кэшируются от минут до
дней. Коллектор также сохраняет публичные рейтинговые матчи в нашу базу (статистика матча и
игроков с ID аккаунтов), чтобы строить агрегированную статистику. Анонимные игроки (со скрытым
профилем) не идентифицируются.</p>
<h3>Ваши вопросы и запросы</h3>
<p>Ваш ИИ-клиент отправляет на сервер вызовы инструментов (например, имя героя или ID
аккаунта). Сервер записывает в лог каждый вызов: название инструмента, длительность, статус,
источники данных, использование кэша и сработал ли лимит. IP-адрес в эти записи не попадает. То,
что вы пишете ассистенту, остаётся между вами и его провайдером; сервер получает только вызовы
инструментов.</p>
<h3>IP-адреса и лимиты запросов</h3>
<p>У каждого клиента есть лимит вызовов в минуту и в день. Сервер берёт IP-адрес из запроса,
хранит как ключ счётчика только первые 16 hex-символов его SHA-256-хэша, и счётчик удаляется
через удвоенный период окна (не дольше двух дней). Хостинг (Vercel) ведёт стандартные логи
запросов с IP-адресами и хранит их по своей политике. [Владелец: укажите срок хранения логов
вашего тарифа Vercel.]</p>

<h2>Третьи стороны</h2>
<ul class="plain">
<li><b>Vercel</b> (хостинг, логи запросов) — <a href="https://vercel.com/legal/privacy-policy">политика</a>.</li>
<li><b>Neon</b> (база PostgreSQL с собранными публичными матчами) —
<a href="https://neon.com/privacy-policy">политика</a>.</li>
<li><b>Upstash</b> (Redis: кэш, счётчики лимитов с хэшами IP) —
<a href="https://upstash.com/trust/privacy.pdf">политика</a>.</li>
<li><b>OpenDota</b> и <b>STRATZ</b> (источники публичных данных Dota 2; к их API обращается
сервер, а не ваш браузер) — <a href="https://www.opendota.com">opendota.com</a>,
<a href="https://stratz.com">stratz.com</a>.</li>
</ul>
<p>[Владелец: укажите регионы, где размещены ресурсы Vercel, Neon и Upstash.]</p>

<h2>Удаление данных игрока</h2>
<p>Чтобы новые матчи не были публичными, отключите «Expose Public Match Data» в настройках
Dota 2. У OpenDota и STRATZ свои копии данных — для удаления там обращайтесь к ним напрямую.
Чтобы удалить данные вашего аккаунта из базы и кэша этого сервера, напишите на {CONTACT} и
укажите Steam ID аккаунта (32-битный Dota ID). Мы удалим сохранённые строки по этому аккаунту и
исключим его из дальнейшего сбора в течение [N] дней. [Владелец: укажите срок и как вы
проверяете запрос.]</p>

<h2>Дети</h2>
<p>Сервис не предназначен для детей и не собирает данных, идентифицирующих человека, сверх
публичных игровых профилей.</p>

<h2>Изменения</h2>
<p>Изменения политики публикуются на этой странице и в репозитории проекта.</p>
""",
}

PRIVACY_TITLE = {
    "en": "Privacy policy — Dota Insights",
    "ru": "Политика конфиденциальности — Dota Insights",
}


def privacy_html(lang: str) -> str:
    return _page(lang, "/privacy", PRIVACY_TITLE[lang], PRIVACY_TITLE[lang], PRIVACY[lang])


def _response(html: str, lang: str) -> HTMLResponse:
    return HTMLResponse(
        html,
        headers={
            "Content-Language": lang,
            "Vary": "Accept-Language",
            "Cache-Control": "public, max-age=300",
        },
    )


async def landing(request: Request, prompts: int) -> HTMLResponse:
    lang = pick_lang(request)
    return _response(landing_html(lang, await cached_catalog(), prompts), lang)


async def privacy(request: Request) -> HTMLResponse:
    lang = pick_lang(request)
    return _response(privacy_html(lang), lang)
