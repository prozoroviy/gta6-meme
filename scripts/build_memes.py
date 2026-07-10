#!/usr/bin/env python3
"""
Ежедневная подборка популярных и смешных мемов про GTA VI.

Собирает сырые данные:
  - Reddit (r/GTA6 и другие сабреддиты из REDDIT_SUBREDDITS), топ постов за сутки,
    через публичный reddit.com/*.json (без ключа — самостоятельная регистрация
    OAuth-приложений для Data API у Reddit сейчас закрыта для новых хобби-проектов).
  - Из каждого поста берётся только визуальный контент (картинка/гифка/видео/
    галерея) — текстовые посты и ссылки на статьи отбрасываются на этом шаге.

Затем весь список сырых кандидатов отправляется одним запросом в Anthropic API
(Claude), который:
  - убирает повторы (репосты одной и той же картинки под разными заголовками),
  - отсеивает то, что не является мемом (скриншоты, арт, серьёзные обсуждения),
  - сортирует по популярности/смешности,
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
import sys

import requests

ANTHROPIC_MODEL = "claude-sonnet-5"
MAX_MEMES = 50

REDDIT_USER_AGENT = "Mozilla/5.0 (compatible; gta6-daily-meme/1.0; +https://github.com/)"

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".gifv", ".webp")

# ---------------------------------------------------------------------------
# Reddit: сбор сырых данных (публичный JSON, без ключа)
# ---------------------------------------------------------------------------


def extract_preview_image(d):
    """Достаёт прямую ссылку на превью-картинку поста. Возвращает None, если пост не визуальный."""
    url = d.get("url", "") or ""

    if d.get("is_gallery") and d.get("media_metadata"):
        first = next(iter(d["media_metadata"].values()), None)
        if first and first.get("s", {}).get("u"):
            return html.unescape(first["s"]["u"])

    preview = d.get("preview", {}).get("images", [])
    if preview:
        source_url = preview[0].get("source", {}).get("url")
        if source_url:
            return html.unescape(source_url)

    if url.lower().endswith(IMAGE_EXTENSIONS):
        return url

    thumb = d.get("thumbnail", "")
    if thumb.startswith("http"):
        return thumb

    return None


def is_visual_post(d):
    hint = d.get("post_hint", "")
    if hint in ("image", "hosted:video", "rich:video"):
        return True
    if d.get("is_gallery"):
        return True
    if (d.get("url") or "").lower().endswith(IMAGE_EXTENSIONS):
        return True
    return False


def fetch_reddit_memes(subreddit, t="day", limit=100):
    """Топ визуальных постов за сутки из сабреддита (публичный JSON, без ключа)."""
    url = f"https://www.reddit.com/r/{subreddit}/top.json?t={t}&limit={limit}"
    r = requests.get(
        url,
        headers={"User-Agent": REDDIT_USER_AGENT},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()

    items = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("stickied") or d.get("over_18"):
            continue
        if not is_visual_post(d):
            continue

        image_url = extract_preview_image(d)
        if not image_url:
            continue

        created_iso = dt.datetime.utcfromtimestamp(d.get("created_utc", 0)).strftime(
            "%Y-%m-%d"
        )

        items.append(
            {
                "title": d.get("title"),
                "image_url": image_url,
                "post_url": "https://www.reddit.com" + d.get("permalink", ""),
                "subreddit": f"r/{subreddit}",
                "score": d.get("score", 0),
                "comments": d.get("num_comments", 0),
                "date": created_iso,
            }
        )
    return items


# ---------------------------------------------------------------------------
# Обработка через Claude API: отбор, отсев не-мемов, подписи на русском
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""Ты — редактор ежедневной подборки смешных мемов про игру GTA VI (Grand Theft Auto VI).

На вход тебе даётся JSON-массив сырых постов с Reddit (заголовок, ссылка на картинку/видео,
ссылка на пост, сабреддит, число апвоутов, число комментариев, дата).

Твоя задача:
1. Убери повторы — если один и тот же мем/картинка встречается несколько раз (репосты),
   оставь только запись с наибольшим числом апвоутов.
2. Убери то, что НЕ является мемом/шуткой: официальные скриншоты без шутки, арт, серьёзные
   обсуждения, новостные картинки, утечки без юмористического контекста.
3. Из оставшегося выбери не более {MAX_MEMES} самых популярных и смешных, отсортируй по
   убыванию популярности (в первую очередь апвоуты, при близких значениях — учитывай
   насколько заголовок "мемный"/смешной). Если реальных мемов за день меньше {MAX_MEMES} —
   верни столько, сколько есть, не выдумывай лишние и не дублируй.
4. Для каждого оставленного элемента верни объект с полями:
   - "caption_ru": короткая смешная подпись/пояснение на русском (до 15 слов), своими
     словами объясняющая в чём шутка (не просто перевод заголовка дословно)
   - "image_url": скопируй ТОЧНО как во входных данных
   - "post_url": скопируй ТОЧНО как во входных данных
   - "subreddit": скопируй как во входных данных
   - "score": скопируй как во входных данных (число)
   - "comments": скопируй как во входных данных (число)
   - "date": скопируй как во входных данных (YYYY-MM-DD)

Отвечай СТРОГО валидным JSON-массивом объектов с этими полями. Никакого текста до или
после JSON. Никаких markdown-блоков вида ```json.
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
# main
# ---------------------------------------------------------------------------


def main():
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        print("ERROR: переменная окружения ANTHROPIC_API_KEY не задана", file=sys.stderr)
        sys.exit(1)

    subreddits = [
        s.strip()
        for s in os.environ.get("REDDIT_SUBREDDITS", "GTA6").split(",")
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
