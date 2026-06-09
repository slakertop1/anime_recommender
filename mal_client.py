"""Клиент для получения данных с MyAnimeList.

Список просмотренного берётся через публичный эндпоинт ``load.json`` сайта MAL
(работает по нику, без OAuth — профиль должен быть публичным).
Рекомендации и детали тайтлов берутся через неофициальный Jikan API (v4).
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.parse
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


# ---------------------------------------------------------------------------
# Официальный MAL API v2: авторизация (OAuth2 + PKCE) и запись в список.
#
# В отличие от чтения публичного списка, запись меняет данные пользователя,
# поэтому MAL требует вход через OAuth2. MAL поддерживает только PKCE-метод
# "plain", т.е. code_challenge совпадает с code_verifier.
# Док: https://myanimelist.net/apiconfig/references/authorization
# ---------------------------------------------------------------------------

OAUTH_AUTHORIZE_URL = "https://myanimelist.net/v1/oauth2/authorize"
OAUTH_TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
API_V2_BASE = "https://api.myanimelist.net/v2"

# Должен дословно совпадать с App Redirect URL, указанным при регистрации
# приложения на https://myanimelist.net/apps.
REDIRECT_URI = "http://localhost:8501/"

# Статусы записи в официальном API (поле my_list_status.status).
LIST_STATUS_WATCHING = "watching"
LIST_STATUS_COMPLETED = "completed"
LIST_STATUS_ON_HOLD = "on_hold"
LIST_STATUS_DROPPED = "dropped"
LIST_STATUS_PLAN = "plan_to_watch"

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_FILE = os.path.join(_PROJECT_DIR, ".env")
_TOKEN_FILE = os.path.join(_PROJECT_DIR, ".mal_token.json")
_PENDING_FILE = os.path.join(_PROJECT_DIR, ".mal_oauth_pending.json")


class MALAuthError(MALError):
    """Проблема с авторизацией/токеном — пользователю нужно войти заново."""


def load_config() -> dict:
    """Возвращает {'client_id', 'client_secret'} из .env или переменных окружения."""
    file_cfg: dict[str, str] = {}
    if os.path.exists(_ENV_FILE):
        with open(_ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                file_cfg[key.strip()] = val.strip().strip('"').strip("'")
    return {
        "client_id": os.environ.get("MAL_CLIENT_ID") or file_cfg.get("MAL_CLIENT_ID", ""),
        "client_secret": os.environ.get("MAL_CLIENT_SECRET") or file_cfg.get("MAL_CLIENT_SECRET", ""),
    }


def _new_code_verifier() -> str:
    # PKCE-метод "plain": challenge == verifier. Длина по RFC 7636 — 43..128.
    return secrets.token_urlsafe(96)[:128]


def build_auth_url(client_id: str) -> str:
    """Готовит ссылку на страницу авторизации MAL.

    PKCE-verifier и state сохраняются в файл, потому что после редиректа обратно
    Streamlit поднимает новую сессию и значения из session_state теряются.
    """
    verifier = _new_code_verifier()
    state = secrets.token_urlsafe(16)
    with open(_PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump({"verifier": verifier, "state": state}, f)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "code_challenge": verifier,        # метод plain → challenge = verifier
        "code_challenge_method": "plain",
        "state": state,
        "redirect_uri": REDIRECT_URI,
    }
    return OAUTH_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def exchange_code_for_token(
    client_id: str, client_secret: str, code: str, state: str | None = None
) -> dict:
    """Меняет authorization code на access/refresh-токены и сохраняет их."""
    try:
        with open(_PENDING_FILE, encoding="utf-8") as f:
            pending = json.load(f)
    except (OSError, ValueError) as exc:
        raise MALAuthError("Сессия авторизации потеряна — войдите заново.") from exc

    if state and pending.get("state") and state != pending["state"]:
        raise MALAuthError("Несовпадение state при авторизации (возможна подмена).")

    data = {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": pending["verifier"],
        "redirect_uri": REDIRECT_URI,
    }
    if client_secret:
        data["client_secret"] = client_secret

    token = _post_token(data)
    _save_token(token)
    try:
        os.remove(_PENDING_FILE)
    except OSError:
        pass
    return token


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Обновляет access-токен по refresh-токену."""
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if client_secret:
        data["client_secret"] = client_secret
    token = _post_token(data)
    _save_token(token)
    return token


def valid_access_token(token: dict, client_id: str, client_secret: str) -> tuple[str, dict]:
    """Действующий access-токен (с автообновлением). Возвращает (token_str, token_dict)."""
    if token.get("expires_at", 0) > time.time() + 60:
        return token["access_token"], token
    refresh = token.get("refresh_token")
    if not refresh:
        raise MALAuthError("Токен истёк — войдите в MAL заново.")
    fresh = refresh_access_token(client_id, client_secret, refresh)
    return fresh["access_token"], fresh


def update_list_status(
    access_token: str,
    anime_id: int,
    *,
    status: str = LIST_STATUS_COMPLETED,
    score: int | None = None,
    num_watched_episodes: int | None = None,
) -> dict:
    """Создаёт/обновляет запись в списке пользователя (PATCH my_list_status)."""
    payload: dict[str, object] = {"status": status}
    if score:
        payload["score"] = int(score)
    if num_watched_episodes:
        payload["num_watched_episodes"] = int(num_watched_episodes)
    headers = dict(_HEADERS)
    headers["Authorization"] = f"Bearer {access_token}"
    url = f"{API_V2_BASE}/anime/{anime_id}/my_list_status"
    try:
        resp = requests.patch(url, data=payload, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise MALError(f"Не удалось связаться с MAL: {exc}") from exc
    if resp.status_code == 401:
        raise MALAuthError("Сессия истекла — войдите в MAL заново.")
    if resp.status_code not in (200, 201):
        raise MALError(f"MAL не принял запись (код {resp.status_code}): {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError:
        return {}


def fetch_my_completed_list(
    access_token: str, session: requests.Session | None = None
) -> list[WatchedAnime]:
    """Список просмотренного авторизованного пользователя через официальный API.

    В отличие от публичного ``load.json``, работает и для приватного профиля;
    жанры и оценка приходят прямо в ответе. Листается через ``paging.next``.
    """
    sess = session or requests.Session()
    headers = dict(_HEADERS)
    headers["Authorization"] = f"Bearer {access_token}"

    next_url: str | None = f"{API_V2_BASE}/users/@me/animelist"
    params: dict | None = {
        "status": "completed",
        "limit": 1000,
        "fields": "list_status,genres",
        "nsfw": "true",
    }

    result: list[WatchedAnime] = []
    while next_url:
        try:
            resp = sess.get(next_url, params=params, headers=headers, timeout=20)
        except requests.RequestException as exc:
            raise MALError(f"Не удалось получить список из MAL: {exc}") from exc
        if resp.status_code == 401:
            raise MALAuthError("Сессия истекла — войдите в MAL заново.")
        if resp.status_code != 200:
            raise MALError(f"MAL вернул ошибку {resp.status_code} при загрузке списка.")
        try:
            data = resp.json()
        except ValueError as exc:
            raise MALError("MAL вернул неожиданный ответ при загрузке списка.") from exc

        for entry in data.get("data", []):
            node = entry.get("node", {})
            status = entry.get("list_status", {})
            aid = node.get("id")
            if not aid:
                continue
            result.append(
                WatchedAnime(
                    mal_id=aid,
                    title=node.get("title", "—"),
                    score=status.get("score", 0) or 0,
                    genres=[g.get("name", "") for g in (node.get("genres") or []) if g.get("name")],
                    image_url=(node.get("main_picture") or {}).get("medium"),
                    url=f"https://myanimelist.net/anime/{aid}",
                )
            )

        next_url = (data.get("paging") or {}).get("next")
        params = None  # ссылка next уже содержит все параметры
    return result


def fetch_anime_episodes(
    access_token: str, anime_id: int, session: requests.Session | None = None
) -> int:
    """Полное число серий тайтла (для проставления num_watched_episodes).

    Возвращает 0, если число неизвестно или запрос не удался — отметка тогда
    просто пройдёт без числа серий, а не упадёт.
    """
    sess = session or requests.Session()
    headers = dict(_HEADERS)
    headers["Authorization"] = f"Bearer {access_token}"
    try:
        resp = sess.get(
            f"{API_V2_BASE}/anime/{anime_id}",
            params={"fields": "num_episodes"},
            headers=headers,
            timeout=20,
        )
    except requests.RequestException:
        return 0
    if resp.status_code != 200:
        return 0
    try:
        return int(resp.json().get("num_episodes", 0) or 0)
    except (ValueError, TypeError):
        return 0


def fetch_my_user_name(access_token: str) -> str | None:
    """Ник авторизованного пользователя (для отображения «вы вошли как …»)."""
    headers = dict(_HEADERS)
    headers["Authorization"] = f"Bearer {access_token}"
    try:
        resp = requests.get(f"{API_V2_BASE}/users/@me", headers=headers, timeout=20)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json().get("name")
    except ValueError:
        return None


def _post_token(data: dict) -> dict:
    try:
        resp = requests.post(OAUTH_TOKEN_URL, data=data, headers=_HEADERS, timeout=20)
    except requests.RequestException as exc:
        raise MALAuthError(f"Не удалось связаться с MAL: {exc}") from exc
    if resp.status_code != 200:
        raise MALAuthError(f"MAL отклонил авторизацию (код {resp.status_code}): {resp.text[:200]}")
    try:
        token = resp.json()
    except ValueError as exc:
        raise MALAuthError("MAL вернул неожиданный ответ при авторизации.") from exc
    token["obtained_at"] = time.time()
    token["expires_at"] = time.time() + token.get("expires_in", 3600)
    return token


def _save_token(token: dict) -> None:
    with open(_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(token, f)


def load_saved_token() -> dict | None:
    """Возвращает сохранённый токен (чтобы вход переживал перезапуск), либо None."""
    if not os.path.exists(_TOKEN_FILE):
        return None
    try:
        with open(_TOKEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def clear_token() -> None:
    for path in (_TOKEN_FILE, _PENDING_FILE):
        try:
            os.remove(path)
        except OSError:
            pass
