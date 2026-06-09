"""Гибридная логика рекомендаций.

Объединяет два сигнала:

1. **Рекомендации MAL** — для любимых тайтлов пользователя берём то, что
   MAL советует по принципу «кто смотрел это, советует то», и аккумулируем
   голоса, взвешивая их оценкой пользователя.
2. **Совпадение жанров** — строим профиль вкусов пользователя по жанрам
   (взвешенный оценками) и считаем близость каждого кандидата к этому профилю.

Итоговый ранг = ``alpha * сигнал_MAL + (1 - alpha) * сигнал_жанров``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import requests

import mal_client as mc


@dataclass
class Recommendation:
    mal_id: int
    title: str
    image_url: str | None
    url: str | None
    score: float          # итоговый гибридный балл 0..1
    rec_score: float      # вклад рекомендаций MAL 0..1
    genre_score: float    # вклад совпадения жанров 0..1
    genres: list[str]
    matched_genres: list[str]
    sources: list[str]    # названия просмотренных тайтлов, давших этого кандидата


def build_genre_profile(watched: list[mc.WatchedAnime]) -> dict[str, float]:
    """Профиль вкусов: вес каждого жанра по просмотренным тайтлам.

    Вес тайтла — оценка пользователя (неоценённые получают нейтральный вес).
    Итог нормируется так, чтобы максимальный вес жанра был равен 1.
    """
    scores = [w.score for w in watched if w.score > 0]
    neutral = (sum(scores) / len(scores)) if scores else 6.0

    weights: dict[str, float] = defaultdict(float)
    for w in watched:
        weight = w.score if w.score > 0 else neutral
        for genre in w.genres:
            weights[genre] += weight

    if not weights:
        return {}
    top = max(weights.values())
    return {g: v / top for g, v in weights.items()}


def _genre_similarity(profile: dict[str, float], genres: list[str]) -> tuple[float, list[str]]:
    """Похожесть набора жанров на профиль (величина 0..1) и список совпавших жанров."""
    if not profile or not genres:
        return 0.0, []
    matched = [g for g in genres if g in profile]
    if not matched:
        return 0.0, []
    # Средний вес совпавших жанров, слегка поощряя количество совпадений.
    avg = sum(profile[g] for g in matched) / len(matched)
    coverage = len(matched) / len(genres)
    sim = avg * (0.6 + 0.4 * coverage)
    return min(sim, 1.0), matched


def recommend(
    watched: list[mc.WatchedAnime],
    *,
    top_titles: int = 15,
    candidate_pool: int = 40,
    final_count: int = 20,
    alpha: float = 0.6,
    session: requests.Session | None = None,
    progress=None,
) -> list[Recommendation]:
    """Строит рекомендации.

    Параметры:
        watched: список просмотренных аниме пользователя.
        top_titles: сколько любимых тайтлов опросить на рекомендации MAL.
        candidate_pool: сколько лучших кандидатов догрузить жанрами для гибрида.
        final_count: сколько рекомендаций вернуть.
        alpha: вес сигнала MAL против сигнала жанров (0..1).
        progress: необязательный колбэк progress(доля_0_1, текст).
    """
    sess = session or requests.Session()
    watched_ids = {w.mal_id for w in watched}
    title_by_id = {w.mal_id: w.title for w in watched}
    profile = build_genre_profile(watched)

    # --- Шаг 1: опрашиваем рекомендации MAL по любимым тайтлам ---------------
    favourites = sorted(watched, key=lambda w: w.score, reverse=True)
    favourites = [w for w in favourites if w.mal_id][:top_titles]

    candidates: dict[int, mc.Candidate] = {}
    for i, fav in enumerate(favourites):
        if progress:
            progress(0.1 + 0.5 * (i / max(len(favourites), 1)),
                     f"Анализ рекомендаций для «{fav.title}»…")
        try:
            recs = mc.fetch_recommendations(fav.mal_id, sess)
        except mc.MALError:
            continue
        # Чем выше оценка исходного тайтла, тем больше доверия его рекомендациям.
        weight = (fav.score / 10.0) if fav.score > 0 else 0.6
        for r in recs:
            cid = r["mal_id"]
            if cid in watched_ids:
                continue
            cand = candidates.get(cid)
            if cand is None:
                cand = mc.Candidate(
                    mal_id=cid, title=r["title"],
                    image_url=r["image_url"], url=r["url"],
                )
                candidates[cid] = cand
            cand.votes += r["votes"] * weight
            cand.sources.add(fav.mal_id)

    if not candidates:
        return []

    # --- Шаг 2: догружаем жанры для лучших кандидатов ------------------------
    ranked = sorted(candidates.values(), key=lambda c: c.votes, reverse=True)
    pool = ranked[:candidate_pool]
    max_votes = max((c.votes for c in pool), default=1.0) or 1.0

    for i, cand in enumerate(pool):
        if progress:
            progress(0.6 + 0.35 * (i / max(len(pool), 1)),
                     f"Сверка жанров: «{cand.title}»…")
        try:
            cand.genres = mc.fetch_anime_genres(cand.mal_id, sess)
        except mc.MALError:
            cand.genres = []

    # --- Шаг 3: гибридный скоринг -------------------------------------------
    results: list[Recommendation] = []
    for cand in pool:
        rec_score = cand.votes / max_votes
        genre_score, matched = _genre_similarity(profile, cand.genres)
        final = alpha * rec_score + (1 - alpha) * genre_score
        results.append(
            Recommendation(
                mal_id=cand.mal_id,
                title=cand.title,
                image_url=cand.image_url,
                url=cand.url,
                score=final,
                rec_score=rec_score,
                genre_score=genre_score,
                genres=cand.genres,
                matched_genres=matched,
                sources=[title_by_id[s] for s in cand.sources if s in title_by_id],
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    if progress:
        progress(1.0, "Готово!")
    return results[:final_count]
