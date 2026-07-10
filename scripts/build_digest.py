#!/usr/bin/env python3
"""
Ежедневный дайджест самых обсуждаемых тем по GTA VI.

Собирает сырые данные из:
  - Reddit (r/GTA6, топ за сутки)
  - Google News (запрос "GTA VI" / "GTA 6")
  - YouTube Data API (опционально, если задан YOUTUBE_API_KEY) — просмотры/лайки видео

Затем отправляет всё это одним запросом в Anthropic API (Claude), который:
  - убирает дубликаты и группирует упоминания одного события в одну тему,
  - переводит и формулирует заголовки/описания на русском языке,
  - сохраняет ссылки на исходные источники и числовые метрики (просмотры/лайки/апвоуты).

Результат сохраняется как:
  - docs/archive/YYYY-MM-DD.json  — темы за конкретный день (данные)
  - docs/manifest.json            — список всех дат, за которые есть архив
  - docs/index.html               — статическая страница-оболочка с переключателем
                                     дней, которая подгружает нужный JSON через fetch()
  - docs/latest.json              — алиас на темы за сегодня (для внешних потребителей)
"""

import datetime as dt
import json
import os
import sys
import urllib.parse
import xml.etree.ElementTree as ET

import requests

ANTHROPIC_MODEL = "claude-sonnet-5"
MAX_TOPICS = 50

# ---------------------------------------------------------------------------
# Сбор сырых данных
# ---------------------------------------------------------------------------


def fetch_reddit_top(subreddit="GTA6", t="day", limit=100):
    """Топовые посты за сутки из указанного сабреддита (публичный JSON API, без ключа)."""
    url = f"https://www.reddit.com/r/{subreddit}/top.json?t={t}&limit={limit}"
    headers = {"User-Agent": "gta6-daily-digest-bot/1.0"}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()

    items = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("stickied"):
            continue
        items.append(
            {
                "source_type": "reddit",
                "source": f"Reddit r/{subreddit}",
                "title": d.get("title"),
                "url": "https://www.reddit.com" + d.get("permalink", ""),
                "metric_raw": {
                    "upvotes": d.get("score"),
                    "comments": d.get("num_comments"),
                },
            }
        )
    return items


def fetch_google_news(query='"GTA VI" OR "GTA 6"', max_items=50):
    """Заголовки последних новостей через Google News RSS (без ключа)."""
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.content)

    items = []
    for item in root.findall(".//item")[:max_items]:
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        source_el = item.find("source")
        source_name = source_el.text if source_el is not None else "Google News"
        items.append(
            {
                "source_type": "news",
                "source": source_name,
                "title": title,
                "url": link,
                "metric_raw": {},
            }
        )
    return items


def fetch_youtube_stats(api_key, query="GTA 6", max_results=50):
    """Статистика просмотров/лайков по видео (нужен бесплатный YouTube Data API ключ)."""
    if not api_key:
        return []

    search_url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "order": "date",
        "maxResults": min(max_results, 50),  # лимит API — 50 за один запрос
        "key": api_key,
    }
    r = requests.get(search_url, params=params, timeout=15)
    r.raise_for_status()
    video_ids = [it["id"]["videoId"] for it in r.json().get("items", [])]
    if not video_ids:
        return []

    stats_url = "https://www.googleapis.com/youtube/v3/videos"
    params2 = {"part": "statistics,snippet", "id": ",".join(video_ids), "key": api_key}
    r2 = requests.get(stats_url, params=params2, timeout=15)
    r2.raise_for_status()

    items = []
    for v in r2.json().get("items", []):
        stats = v.get("statistics", {})
        snippet = v.get("snippet", {})
        items.append(
            {
                "source_type": "youtube",
                "source": f"YouTube: {snippet.get('channelTitle', '')}",
                "title": snippet.get("title"),
                "url": f"https://www.youtube.com/watch?v={v.get('id')}",
                "metric_raw": {
                    "views": stats.get("viewCount"),
                    "likes": stats.get("likeCount"),
                    "comments": stats.get("commentCount"),
                },
            }
        )
    return items


# ---------------------------------------------------------------------------
# Обработка через Claude API: дедупликация, перевод, оформление
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""Ты — редактор ежедневного дайджеста новостей по игре GTA VI (Grand Theft Auto VI).

На вход тебе даётся JSON-массив сырых записей (посты Reddit, новости, видео YouTube) за последние сутки.

Твоя задача:
1. Выбери не более {MAX_TOPICS} самых значимых и обсуждаемых тем. Объедини записи об одном
   и том же событии в одну тему (не показывай дубликаты по отдельности). Если реальных
   различных тем меньше {MAX_TOPICS} — верни столько, сколько есть, не выдумывай лишние.
2. Для каждой темы верни объект со следующими полями:
   - "title_ru": короткий заголовок на русском языке (до 12 слов)
   - "summary_ru": 1-2 предложения на русском, объясняющие суть темы своими словами
   - "metric": строка на русском с самой показательной численной метрикой этой темы,
     например "165 млн просмотров на YouTube" или "2400 апвоутов, 540 комментариев на Reddit".
     Если для темы нет числовых метрик — верни null.
   - "sources": массив объектов {{"name": "...", "url": "..."}} — все релевантные источники
     из входных данных, url копируй ТОЧНО как во входных данных, ничего не выдумывай.
3. Сортируй темы по убыванию значимости/обсуждаемости.

Отвечай СТРОГО валидным JSON-массивом объектов с полями title_ru, summary_ru, metric, sources.
Никакого текста до или после JSON. Никаких markdown-блоков вида ```json.
"""


def summarize_with_claude(api_key, raw_items):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 8000,
            "system": SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": json.dumps(raw_items, ensure_ascii=False)}
            ],
        },
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()

    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()

    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]

    return json.loads(text)


# ---------------------------------------------------------------------------
# Манифест доступных дат
# ---------------------------------------------------------------------------


def update_manifest(date_str):
    path = "docs/manifest.json"
    dates = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                dates = json.load(f)
            except json.JSONDecodeError:
                dates = []
    if date_str not in dates:
        dates.append(date_str)
    dates = sorted(set(dates), reverse=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dates, f, ensure_ascii=False, indent=2)
    return dates


# ---------------------------------------------------------------------------
# Статическая страница-оболочка (с переключателем дней, подгрузка через fetch)
# ---------------------------------------------------------------------------

INDEX_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GTA VI — дайджест тем по дням</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #0f1115;
    --card: #171a21;
    --border: #2a2e38;
    --text: #eef0f3;
    --muted: #9aa0ab;
    --accent: #ff7a45;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 40px 16px;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
  }
  .wrap { max-width: 760px; margin: 0 auto; }
  .top-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 6px;
  }
  h1 { font-size: 24px; font-weight: 600; margin: 0; }
  .sub { color: var(--muted); font-size: 14px; margin-bottom: 24px; }
  select#day-select {
    background: var(--card);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 14px;
  }
  .card {
    display: flex;
    gap: 16px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px 20px;
    margin-bottom: 14px;
  }
  .rank {
    flex-shrink: 0;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: var(--accent);
    color: #1a1a1a;
    font-weight: 700;
    font-size: 13px;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .card h2 { font-size: 16px; font-weight: 600; margin: 0 0 6px; }
  .card p { font-size: 14px; color: var(--muted); margin: 0 0 10px; line-height: 1.5; }
  .metric {
    display: inline-block;
    font-size: 12px;
    color: var(--accent);
    background: rgba(255, 122, 69, 0.12);
    border-radius: 6px;
    padding: 3px 8px;
    margin-bottom: 8px;
  }
  .sources { font-size: 12px; color: var(--muted); }
  .sources a { color: #7fb8ff; text-decoration: none; margin-right: 4px; }
  .sources a:hover { text-decoration: underline; }
  .empty { color: var(--muted); font-size: 14px; padding: 20px 0; }
  footer { margin-top: 32px; font-size: 12px; color: var(--muted); text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <div class="top-row">
    <h1>GTA VI — самые обсуждаемые темы</h1>
    <select id="day-select"></select>
  </div>
  <div class="sub" id="sub">Загрузка...</div>
  <div id="cards"></div>
  <footer>Собрано автоматически из Reddit, Google News и YouTube, обработано Claude</footer>
</div>
<script>
const MONTHS_RU = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"];

function formatDateRu(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return `${d} ${MONTHS_RU[m - 1]} ${y}`;
}

function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function renderTopics(topics) {
  const container = document.getElementById("cards");
  if (!topics || topics.length === 0) {
    container.innerHTML = '<div class="empty">За этот день данных нет.</div>';
    return;
  }
  container.innerHTML = topics.map((t, i) => {
    const sourcesHtml = (t.sources || [])
      .filter(s => s && s.url)
      .map(s => `<a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.name)}</a>`)
      .join(" · ");
    const metricHtml = t.metric ? `<div class="metric">${escapeHtml(t.metric)}</div>` : "";
    return `
      <div class="card">
        <div class="rank">${i + 1}</div>
        <div>
          <h2>${escapeHtml(t.title_ru)}</h2>
          <p>${escapeHtml(t.summary_ru)}</p>
          ${metricHtml}
          <div class="sources">Источники: ${sourcesHtml || "—"}</div>
        </div>
      </div>`;
  }).join("");
}

async function loadDay(iso) {
  document.getElementById("sub").textContent = `Темы за ${formatDateRu(iso)} · загрузка...`;
  try {
    const res = await fetch(`archive/${iso}.json`);
    if (!res.ok) throw new Error("not found");
    const topics = await res.json();
    renderTopics(topics);
    document.getElementById("sub").textContent =
      `Темы за ${formatDateRu(iso)} · всего: ${topics.length}`;
  } catch (e) {
    document.getElementById("cards").innerHTML =
      '<div class="empty">Не удалось загрузить данные за этот день.</div>';
    document.getElementById("sub").textContent = `Темы за ${formatDateRu(iso)}`;
  }
}

async function init() {
  const select = document.getElementById("day-select");
  try {
    const res = await fetch("manifest.json");
    const dates = await res.json();
    if (!dates.length) {
      document.getElementById("sub").textContent = "Архив пока пуст.";
      return;
    }
    select.innerHTML = dates.map(d => `<option value="${d}">${formatDateRu(d)}</option>`).join("");
    select.addEventListener("change", e => loadDay(e.target.value));
    await loadDay(dates[0]);
  } catch (e) {
    document.getElementById("sub").textContent = "Не удалось загрузить список дней (manifest.json).";
  }
}

init();
</script>
</body>
</html>
"""


def write_index_shell():
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(INDEX_HTML)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        print("ERROR: переменная окружения ANTHROPIC_API_KEY не задана", file=sys.stderr)
        sys.exit(1)

    youtube_key = os.environ.get("YOUTUBE_API_KEY")  # опционально

    raw_items = []

    for fetch_fn, name in [
        (lambda: fetch_reddit_top(), "Reddit"),
        (lambda: fetch_google_news(), "Google News"),
        (lambda: fetch_youtube_stats(youtube_key), "YouTube"),
    ]:
        try:
            new_items = fetch_fn()
            print(f"{name}: получено {len(new_items)} записей")
            raw_items.extend(new_items)
        except Exception as e:
            print(f"{name}: ошибка сбора данных — {e}", file=sys.stderr)

    if not raw_items:
        print("ERROR: не удалось собрать ни одной записи ни из одного источника", file=sys.stderr)
        sys.exit(1)

    topics = summarize_with_claude(anthropic_key, raw_items)

    date_iso = dt.datetime.utcnow().strftime("%Y-%m-%d")

    os.makedirs("docs/archive", exist_ok=True)

    with open(f"docs/archive/{date_iso}.json", "w", encoding="utf-8") as f:
        json.dump(topics, f, ensure_ascii=False, indent=2)

    with open("docs/latest.json", "w", encoding="utf-8") as f:
        json.dump(topics, f, ensure_ascii=False, indent=2)

    update_manifest(date_iso)
    write_index_shell()

    print(f"Готово: тем за {date_iso}: {len(topics)}")


if __name__ == "__main__":
    main()
