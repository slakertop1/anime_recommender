"""Клиент для получения данных с MyAnimeList.

Список просмотренного берётся через публичный эндпоинт ``load.json`` сайта MAL
(работает по нику, без OAuth — профиль должен быть публичным).
Рекомендации и детали тайтлов берутся через неофициальный Jikan API (v4).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

JIKAN_BASE = "https://api.jikan.moe/v4"
MAL_LIST_URL = "https://myanimelist.net/animelist/{user}/load.json"

# Статусы MAL: 1 — смотрю, 2 — просмотрено, 3 — отложено, 4 — брошено, 6 — в планах.
STATUS_COMPLETED = 2

_HEADERS = {"User-Agent": "anime-recommender/1.0 (+https://github.com/)"}

# Jikan просит не больше ~3 запросов в секунду. Держим запас.
_JIKAN_DELAY = 0.4


class MALError(Exception):
    """Ошибка обращения к MAL/Jikan, понятная для показа пользователю."""


@dataclass
class WatchedAnime:
    """Один просмотренный тайтл из списка пользователя."""

    mal_id: int
    title: str
    score: int  # оценка пользователя 1..10, 0 — если не оценено
    genres: list[str] = field(default_factory=list)
    image_url: str | None = None
    url: str | None = None


@dataclass
class Candidate:
    """Кандидат в рекомендации."""

    mal_id: int
    title: str
    image_url: str | None = None
    url: str | None = None
    votes: int = 0  # суммарные «голоса» рекомендаций MAL
    genres: list[str] = field(default_factory=list)
    sources: set[int] = field(default_factory=set)  # из каких тайтлов пришёл


def fetch_completed_list(username: str, session: requests.Session | None = None) -> list[WatchedAnime]:
    """Возвращает список просмотренных (completed) аниме пользователя.

    Эндпоинт ``load.json`` отдаёт максимум 300 записей за раз, поэтому
    листаем постранично через ``offset``.
    """
    username = username.strip()
    if not username:
        raise MALError("Укажите ник пользователя MyAnimeList.")

    sess = session or requests.Session()
    result: list[WatchedAnime] = []
    offset = 0
    page_size = 300

    while True:
        url = MAL_LIST_URL.format(user=username)
        params = {"status": STATUS_COMPLETED, "offset": offset}
        try:
            resp = sess.get(url, params=params, headers=_HEADERS, timeout=20)
        except requests.RequestException as exc:
            raise MALError(f"Не удалось связаться с MyAnimeList: {exc}") from exc

        if resp.status_code == 404:
            raise MALError(
                f"Пользователь «{username}» не найден или его список аниме скрыт "
                "(профиль должен быть публичным)."
            )
        if resp.status_code == 429:
            time.sleep(2)
            continue
        if resp.status_code != 200:
            raise MALError(f"MyAnimeList вернул ошибку {resp.status_code}.")

        try:
            page = resp.json()
        except ValueError as exc:
            raise MALError("MyAnimeList вернул неожиданный ответ (не JSON).") from exc

        if not page:
            break

        for item in page:
            result.append(
                WatchedAnime(
                    mal_id=item.get("anime_id", 0),
                    title=item.get("anime_title", "—"),
                    score=item.get("score", 0) or 0,
                    genres=[g.get("name", "") for g in (item.get("genres") or []) if g.get("name")],
                    image_url=item.get("anime_image_path"),
                    url=_abs_url(item.get("anime_url")),
                )
            )

        if len(page) < page_size:
            break
        offset += page_size
        time.sleep(0.3)

    return result


def fetch_recommendations(mal_id: int, session: requests.Session | None = None) -> list[dict]:
    """Рекомендации MAL для одного тайтла (через Jikan).

    Возвращает список словарей с ключами ``mal_id``, ``title``, ``image_url``,
    ``url``, ``votes``.
    """
    sess = session or requests.Session()
    data = _jikan_get(f"/anime/{mal_id}/recommendations", sess)
    recs = []
    for entry in data.get("data", []):
        node = entry.get("entry", {})
        if not node.get("mal_id"):
            continue
        recs.append(
            {
                "mal_id": node["mal_id"],
                "title": node.get("title", "—"),
                "image_url": (node.get("images", {}).get("jpg", {}).get("image_url")),
                "url": node.get("url"),
                "votes": entry.get("votes", 0),
            }
        )
    return recs


def fetch_anime_genres(mal_id: int, session: requests.Session | None = None) -> list[str]:
    """Жанры конкретного тайтла (через Jikan)."""
    sess = session or requests.Session()
    data = _jikan_get(f"/anime/{mal_id}", sess)
    node = data.get("data", {})
    out = []
    for key in ("genres", "themes", "demographics"):
        out.extend(g.get("name", "") for g in (node.get(key) or []) if g.get("name"))
    return out


def _jikan_get(path: str, session: requests.Session, _retries: int = 3) -> dict:
    """GET к Jikan с обработкой rate-limit (429) и троттлингом."""
    url = JIKAN_BASE + path
    for attempt in range(_retries):
        try:
            resp = session.get(url, headers=_HEADERS, timeout=20)
        except requests.RequestException as exc:
            if attempt == _retries - 1:
                raise MALError(f"Не удалось связаться с Jikan API: {exc}") from exc
            time.sleep(1)
            continue

        time.sleep(_JIKAN_DELAY)

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError as exc:
                raise MALError("Jikan вернул неожиданный ответ.") from exc
        if resp.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        if resp.status_code == 404:
            return {"data": []}
        if attempt == _retries - 1:
            raise MALError(f"Jikan вернул ошибку {resp.status_code}.")
        time.sleep(1)
    return {"data": []}


def _abs_url(path: str | None) -> str | None:
    if not path:
        return None
    if path.startswith("http"):
        return path
    return "https://myanimelist.net" + path
