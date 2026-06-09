"""Веб-интерфейс рекомендателя аниме (Streamlit).

Запуск:
    venv313\\Scripts\\streamlit run app.py
"""

from __future__ import annotations

import requests
import streamlit as st

import mal_client as mc
import recommender as rec

st.set_page_config(page_title="Рекомендатель аниме", page_icon="🎬", layout="wide")

st.title("🎬 Рекомендатель аниме по MyAnimeList")
st.caption(
    "Введите ник на MyAnimeList — программа возьмёт ваш список просмотренного "
    "и подберёт похожие тайтлы (гибрид: рекомендации MAL + совпадение жанров)."
)


@st.cache_data(show_spinner=False, ttl=3600)
def load_watched(username: str) -> list[mc.WatchedAnime]:
    return mc.fetch_completed_list(username)


with st.sidebar:
    st.header("Настройки")
    username = st.text_input("Ник на MyAnimeList", placeholder="например, Xinil")
    alpha = st.slider(
        "Баланс сигнала",
        min_value=0.0, max_value=1.0, value=0.6, step=0.05,
        help="0 — только совпадение жанров, 1 — только рекомендации MAL.",
    )
    final_count = st.slider("Сколько рекомендаций показать", 5, 40, 20, 5)
    top_titles = st.slider(
        "Сколько любимых тайтлов учитывать", 5, 30, 15, 1,
        help="Больше — точнее, но дольше из-за лимитов API.",
    )
    go = st.button("Подобрать рекомендации", type="primary", use_container_width=True)


def render_recommendations(recs: list[rec.Recommendation]) -> None:
    cols_per_row = 4
    for row_start in range(0, len(recs), cols_per_row):
        row = recs[row_start:row_start + cols_per_row]
        cols = st.columns(cols_per_row)
        for col, r in zip(cols, row):
            with col:
                if r.image_url:
                    st.image(r.image_url, use_container_width=True)
                st.markdown(f"**[{r.title}]({r.url})**" if r.url else f"**{r.title}**")
                st.progress(min(r.score, 1.0), text=f"Совпадение: {r.score * 100:.0f}%")
                if r.matched_genres:
                    st.caption("🏷️ " + ", ".join(r.matched_genres))
                if r.sources:
                    shown = ", ".join(r.sources[:3])
                    extra = f" и ещё {len(r.sources) - 3}" if len(r.sources) > 3 else ""
                    st.caption(f"↪ похоже на: {shown}{extra}")


if go:
    if not username.strip():
        st.warning("Сначала введите ник на MyAnimeList.")
        st.stop()

    session = requests.Session()

    # 1. Грузим список просмотренного.
    with st.spinner(f"Загружаю список «{username}»…"):
        try:
            watched = load_watched(username.strip())
        except mc.MALError as exc:
            st.error(str(exc))
            st.stop()

    if not watched:
        st.warning("В списке просмотренного (completed) пусто — рекомендовать нечего.")
        st.stop()

    st.success(f"Загружено просмотренных тайтлов: {len(watched)}")

    # Краткая сводка вкусов.
    profile = rec.build_genre_profile(watched)
    if profile:
        top_genres = sorted(profile.items(), key=lambda kv: kv[1], reverse=True)[:8]
        st.markdown("**Ваши любимые жанры:** " + ", ".join(g for g, _ in top_genres))

    # 2. Считаем рекомендации с прогресс-баром.
    progress_bar = st.progress(0.0, text="Готовлюсь…")

    def on_progress(frac: float, text: str) -> None:
        progress_bar.progress(min(frac, 1.0), text=text)

    try:
        recs = rec.recommend(
            watched,
            top_titles=top_titles,
            final_count=final_count,
            alpha=alpha,
            session=session,
            progress=on_progress,
        )
    except mc.MALError as exc:
        st.error(str(exc))
        st.stop()
    finally:
        progress_bar.empty()

    if not recs:
        st.warning(
            "Не удалось собрать рекомендации — у выбранных тайтлов их нет на MAL. "
            "Попробуйте увеличить число учитываемых тайтлов."
        )
        st.stop()

    st.subheader(f"Рекомендуем вам ({len(recs)})")
    render_recommendations(recs)
else:
    st.info("👈 Введите ник в панели слева и нажмите «Подобрать рекомендации».")
