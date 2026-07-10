#!/usr/bin/env python3
"""
Ежедневная подборка популярных и смешных мемов про GTA VI.

Собирает сырые данные:
  - Reddit (r/GTA6 и другие сабреддиты из REDDIT_SUBREDDITS), топ постов за сутки,
    через публичную RSS/Atom-ленту reddit.com/r/.../top/.rss (без ключа).
    Это НЕ основной выбор, а вынужденный: и самостоятельная регистрация
    OAuth-приложений для Reddit Data API, и получение ключа Imgur сейчас закрыты
    для новых хобби-проектов, а обычный reddit.com/*.json (даже без авторизации)
    возвращает 403 и с датацентровых IP, и с раннеров GitHub Actions. RSS-лента —
    единственный проверенный рабочий путь, но у неё есть ограничение: она не
    отдаёт число апвоутов/комментариев, только заголовок, картинку, ссылку и дату.
    Порядок постов в ленте уже отсортирован Reddit'ом по популярности за день,
    поэтому позиция поста в списке — единственный доступный сигнал популярности.
  - Из каждого поста берётся только визуальный контент (там, где RSS отдаёт
    превью-картинку) — текстовые посты без картинки отбрасываются на этом шаге.

Затем весь список сырых кандидатов отправляется одним запросом в Anthropic API
(Claude), который:
  - убирает повторы (репосты одной и той же картинки под разными заголовками),
  - отсеивает то, что не является мемом (скриншоты, арт, серьёзные обсуждения),
  - сохраняет исходный порядок (популярность по версии Reddit),
  - добавляет короткую смешную подпись на русском (caption_ru),
  - оставляет не более MAX_MEMES штук.

Результат сохраняется как:
  - docs/archive/YYYY-MM-DD.json — мемы за конкретный день (данные)
  - docs/manifest.json           — список всех дат, за которые есть архив
  - docs/index.html              — статическая страница-оболочка с переключателем
                                    дней и сеткой карточек-превью
  - docs/latest.json             — алиас на мемы за сегодня
"""

import datetime as dt
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

import requests

ANTHROPIC_MODEL = "claude-sonnet-5"
MAX_MEMES = 50

REDDIT_USER_AGENT = "Mozilla/5.0 (compatible; gta6-daily-meme/1.0; +https://github.com/)"

ATOM_NS = "{http://www.w3.org/2005/Atom}"
MEDIA_NS = "{http://search.yahoo.com/mrss/}"

# Домены-хосты, на которых Reddit реально хранит превью/картинки постов.
# Всё остальное (иконки, аватары, generic-плейсхолдеры) — не мем, а служебная картинка.
REDDIT_MEDIA_HOSTS = ("preview.redd.it", "i.redd.it", "external-preview.redd.it")

# ---------------------------------------------------------------------------
# Reddit: сбор сырых данных (публичная RSS/Atom-лента, без ключа)
# ---------------------------------------------------------------------------


def extract_preview_image(entry):
    """Достаёт прямую ссылку на превью-картинку записи RSS. Возвращает None, если это не мем-картинка."""
    thumb_el = entry.find(f"{MEDIA_NS}thumbnail")
    if thumb_el is not None:
        url = thumb_el.get("url")
        if url and any(host in url for host in REDDIT_MEDIA_HOSTS):
            return html.unescape(url)

    content_el = entry.find(f"{ATOM_NS}content")
    if content_el is not None and content_el.text:
        content_html = html.unescape(content_el.text)
        match = re.search(r'<img src="([^"]+)"', content_html)
        if match:
            url = html.unescape(match.group(1))
            if any(host in url for host in REDDIT_MEDIA_HOSTS):
                return url

    return None


def fetch_reddit_memes(subreddit, t="day", limit=100):
    """Топ визуальных постов за сутки из сабреддита (публичная RSS-лента, без ключа)."""
    url = f"https://www.reddit.com/r/{subreddit}/top/.rss?t={t}&limit={limit}"
    r = requests.get(
        url,
        headers={"User-Agent": REDDIT_USER_AGENT},
        timeout=15,
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)

    items = []
    for rank, entry in enumerate(root.findall(f"{ATOM_NS}entry"), start=1):
        image_url = extract_preview_image(entry)
        if not image_url:
            continue

        title_el = entry.find(f"{ATOM_NS}title")
        link_el = entry.find(f"{ATOM_NS}link")
        published_el = entry.find(f"{ATOM_NS}published")

        post_url = link_el.get("href") if link_el is not None else None
        if not post_url:
            continue

        published = published_el.text if published_el is not None else None
        date_iso = published[:10] if published else dt.datetime.utcnow().strftime("%Y-%m-%d")

        items.append(
            {
                "title": title_el.text if title_el is not None else "",
                "image_url": image_url,
                "post_url": post_url,
                "subreddit": f"r/{subreddit}",
                "rank": rank,
                "date": date_iso,
            }
        )
    return items


# ---------------------------------------------------------------------------
# Обработка через Claude API: отбор, отсев не-мемов, подписи на русском
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""Ты — редактор ежедневной подборки смешных мемов про игру GTA VI (Grand Theft Auto VI).

На вход тебе даётся JSON-массив сырых постов с Reddit (заголовок, ссылка на картинку,
ссылка на пост, сабреддит, дата, rank — позиция поста в списке "топ дня" по версии Reddit,
чем меньше rank, тем популярнее пост). Числа апвоутов/комментариев недоступны — источник
данных их не отдаёт, поэтому единственный сигнал популярности — это rank.

Твоя задача:
1. Убери повторы — если один и тот же мем/картинка встречается несколько раз (репосты),
   оставь только запись с наименьшим (то есть более высоким по популярности) rank.
2. Убери то, что НЕ является мемом/шуткой: официальные скриншоты без шутки, арт, серьёзные
   обсуждения, новостные картинки, утечки без юмористического контекста.
3. Из оставшегося выбери не более {MAX_MEMES} самых смешных, отсортируй по возрастанию rank
   (самый популярный — первым). Если реальных мемов за день меньше {MAX_MEMES} — верни
   столько, сколько есть, не выдумывай лишние и не дублируй.
4. Для каждого оставленного элемента верни объект с полями:
   - "caption_ru": развёрнутое описание на русском (4-6 предложений, примерно 60-100 слов).
     Опиши своими словами, что происходит на картинке и в чём именно шутка, дай контекст
     (например, к какой новости, слуху, тренду или расхожей теме про GTA VI отсылает мем),
     объясни, почему это может быть смешно фанатам игры. Пиши живо и с юмором, не просто
     дословно переводи заголовок — заголовок как правило слишком короткий и лишён контекста.
   - "image_url": скопируй ТОЧНО как во входных данных
   - "post_url": скопируй ТОЧНО как во входных данных
   - "subreddit": скопируй как во входных данных
   - "date": скопируй как во входных данных (YYYY-MM-DD)

Отвечай СТРОГО валидным JSON-массивом объектов с этими полями (без rank — порядок в
итоговом массиве и есть ранжирование). Никакого текста до или после JSON. Никаких
markdown-блоков вида ```json.
"""


def curate_with_claude(api_key, raw_items):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 16000,
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
# main
# ---------------------------------------------------------------------------


def main():
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        print("ERROR: переменная окружения ANTHROPIC_API_KEY не задана", file=sys.stderr)
        sys.exit(1)

    subreddits = [
        s.strip()
        for s in (os.environ.get("REDDIT_SUBREDDITS") or "GTA6").split(",")
        if s.strip()
    ]

    raw_items = []
    for sub in subreddits:
        try:
            new_items = fetch_reddit_memes(sub)
            print(f"r/{sub}: получено {len(new_items)} визуальных постов")
            raw_items.extend(new_items)
        except Exception as e:
            print(f"r/{sub}: ошибка сбора данных — {e}", file=sys.stderr)

    if not raw_items:
        print("ERROR: не удалось собрать ни одного визуального поста ни из одного сабреддита", file=sys.stderr)
        sys.exit(1)

    memes = curate_with_claude(anthropic_key, raw_items)

    date_iso = dt.datetime.utcnow().strftime("%Y-%m-%d")

    os.makedirs("docs/archive", exist_ok=True)

    with open(f"docs/archive/{date_iso}.json", "w", encoding="utf-8") as f:
        json.dump(memes, f, ensure_ascii=False, indent=2)

    with open("docs/latest.json", "w", encoding="utf-8") as f:
        json.dump(memes, f, ensure_ascii=False, indent=2)

    update_manifest(date_iso)

    print(f"Готово: мемов за {date_iso}: {len(memes)}")


if __name__ == "__main__":
    main()
