"""Жанровая логика рекомендаций с учётом новизны.

Кандидаты ищутся по любимым жанрам пользователя (Jikan-поиск) и ранжируются по
двум множителям:

1. **Совпадение жанров** — близость набора жанров кандидата к профилю вкусов
   пользователя (профиль взвешен оценками просмотренного).
2. **Новизна** — год выхода: свежие тайтлы получают приоритет, но классика не
   вытесняется полностью.

Итог: ``score = совпадение_жанров * вес_новизны``.

Функция :func:`recommend_iter` — генератор: отдаёт промежуточные результаты по
мере опроса жанров, чтобы интерфейс показывал тайтлы прямо во время анализа.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass

import mal_client as mc

# Год для расчёта новизны. Это обычный код приложения (не workflow), поэтому
# datetime доступен.
CURRENT_YEAR = datetime.date.today().year

# Множитель новизны: самый старый тайтл получает RECENCY_FLOOR, самый свежий — 1.
RECENCY_FLOOR = 0.5
# С какого года считаем «совсем старым» (нормировка новизны).
RECENCY_BASE_YEAR = 2005
# Сколько последних лет считаем «свежими» для отдельного запроса.
FRESH_YEARS = 3

# Множитель качества по оценке MAL: ниже QUALITY_LOW → минимум, выше QUALITY_HIGH → 1.
QUALITY_FLOOR = 0.5
QUALITY_LOW = 6.0
QUALITY_HIGH = 8.5


@dataclass
class Recommendation:
    mal_id: int
    title: str
    image_url: str | None
    url: str | None
    score: float          # итоговый балл 0..1
    genre_score: float    # совпадение жанров 0..1
    recency: float        # вес новизны 0..1
    genres: list[str]
    matched_genres: list[str]
    year: int | None
    mal_score: float = 0.0  # оценка тайтла на MAL (для пояснения)


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
    """Похожесть набора жанров на профиль (0..1) и список совпавших жанров."""
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


def _recency_weight(year: int | None) -> float:
    """Вес новизны: свежие тайтлы выше, старые не обнуляются (>= RECENCY_FLOOR)."""
    if not year:
        return RECENCY_FLOOR + (1 - RECENCY_FLOOR) * 0.3  # год неизвестен — умеренно
    span = max(CURRENT_YEAR - RECENCY_BASE_YEAR, 1)
    norm = (year - RECENCY_BASE_YEAR) / span
    norm = min(max(norm, 0.0), 1.0)
    return RECENCY_FLOOR + (1 - RECENCY_FLOOR) * norm


def _quality_weight(score: float) -> float:
    """Вес качества по оценке MAL: плохие тайтлы не всплывают только из-за новизны."""
    if not score:
        return QUALITY_FLOOR + (1 - QUALITY_FLOOR) * 0.4  # без оценки — умеренно
    norm = (score - QUALITY_LOW) / max(QUALITY_HIGH - QUALITY_LOW, 0.1)
    norm = min(max(norm, 0.0), 1.0)
    return QUALITY_FLOOR + (1 - QUALITY_FLOOR) * norm


def _score_candidates(
    candidates: dict[int, dict], profile: dict[str, float]
) -> list[Recommendation]:
    """Считает баллы и возвращает кандидатов, отсортированных по убыванию.

    Балл = совпадение_жанров * вес_новизны * вес_качества — баланс релевантности,
    свежести и качества.
    """
    out: list[Recommendation] = []
    for c in candidates.values():
        sim, matched = _genre_similarity(profile, c["genres"])
        if sim <= 0:
            continue
        recency = _recency_weight(c.get("year"))
        quality = _quality_weight(c.get("score") or 0)
        out.append(
            Recommendation(
                mal_id=c["mal_id"],
                title=c["title"],
                image_url=c["image_url"],
                url=c["url"],
                score=sim * recency * quality,
                genre_score=sim,
                recency=recency,
                genres=c["genres"],
                matched_genres=matched,
                year=c.get("year"),
                mal_score=float(c.get("score") or 0),
            )
        )
    out.sort(key=lambda r: r.score, reverse=True)
    return out


def recommend_iter(
    watched: list[mc.WatchedAnime],
    *,
    genre_id_map: dict[str, int],
    search_fn,
    final_count: int = 20,
    top_genres_count: int = 5,
    extra_genres: list[str] | tuple[str, ...] = (),
    exclude_genres: list[str] | tuple[str, ...] = (),
):
    """Генератор рекомендаций по жанрам с учётом новизны.

    Параметры:
        watched: список просмотренного (для профиля и исключения).
        genre_id_map: соответствие «жанр → id» (из mal_client.fetch_genre_id_map).
        search_fn: функция search_fn(genre_id, order_by, start_date) -> list[dict]
            поиска кандидатов по жанру (можно обернуть в кэш на стороне приложения).
        final_count: сколько рекомендаций отдавать.
        top_genres_count: по скольким любимым жанрам искать.
        extra_genres: жанры, которые искать дополнительно (даже если их мало в
            просмотренном); они же добавляются в профиль с высоким весом.
        exclude_genres: жанры/демография, тайтлы с которыми не предлагать.

    Отдаёт кортежи (partial_results, доля_0_1, текст_прогресса). Последний
    кортеж содержит финальный отсортированный список.
    """
    profile = build_genre_profile(watched)
    exclude = set(exclude_genres)
    watched_ids = {w.mal_id for w in watched}

    # Добавленные жанры считаем сильным предпочтением, чтобы их совпадения весили.
    for g in extra_genres:
        profile[g] = max(profile.get(g, 0.0), 1.0)

    if not profile or not genre_id_map:
        yield [], 1.0, "Недостаточно данных: нет жанров в просмотренном."
        return

    # Сначала добавленные жанры, затем любимые из профиля; исключённые не ищем.
    ranked = [g for g, _ in sorted(profile.items(), key=lambda kv: kv[1], reverse=True)]
    ordered = list(dict.fromkeys(list(extra_genres) + ranked))
    search_genres = [
        g for g in ordered if g in genre_id_map and g not in exclude
    ][:top_genres_count + len(extra_genres)]

    if not search_genres:
        yield [], 1.0, "Не удалось сопоставить жанры с каталогом."
        return

    # По каждому жанру — два запроса: популярное за всё время и популярное за
    # последние годы (свежее). Так в пул попадают и классика, и новинки, а вес
    # новизны/качества затем балансирует итог.
    fresh_since = f"{CURRENT_YEAR - FRESH_YEARS}-01-01"
    plan: list[tuple[str, int, str | None, str]] = []
    for g in search_genres:
        gid = genre_id_map[g]
        plan.append((g, gid, None, "популярные"))
        plan.append((g, gid, fresh_since, "свежие"))

    candidates: dict[int, dict] = {}
    total = len(plan)
    partial: list[Recommendation] = []

    for i, (gname, gid, start_date, label) in enumerate(plan):
        text = f"Жанр «{gname}» — {label}…"
        try:
            results = search_fn(gid, "members", start_date)
        except mc.MALError:
            results = []
        for c in results:
            cid = c["mal_id"]
            if cid in watched_ids or cid in candidates:
                continue
            if exclude and exclude.intersection(c["genres"]):
                continue
            candidates[cid] = c

        partial = _score_candidates(candidates, profile)[:final_count]
        yield partial, (i + 1) / total, text

    if not plan:
        yield [], 1.0, "Готово."
