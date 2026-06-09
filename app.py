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

def read_config() -> dict:
    """Конфиг MAL: сначала Streamlit Secrets (для хостинга), затем .env/окружение."""
    cfg = mc.load_config()
    try:
        secret_map = {
            "MAL_CLIENT_ID": "client_id",
            "MAL_CLIENT_SECRET": "client_secret",
            "MAL_REDIRECT_URI": "redirect_uri",
        }
        for secret_key, cfg_key in secret_map.items():
            if secret_key in st.secrets:
                cfg[cfg_key] = st.secrets[secret_key]
    except Exception:
        # Файла секретов нет (обычный локальный запуск) — это нормально.
        pass
    return cfg


cfg = read_config()


# --- Обработка возврата с авторизации MAL (?code=...&state=...) --------------
# Токен живёт только в session_state текущего посетителя и на диск не пишется,
# поэтому на общем хостинге пользователи не делят один вход.
_qp = st.query_params
if "code" in _qp and "mal_token" not in st.session_state:
    if not cfg["client_id"]:
        st.error("Не задан MAL_CLIENT_ID — авторизация невозможна.")
        st.stop()
    try:
        token = mc.exchange_code_for_token(
            cfg["client_id"], cfg["client_secret"], _qp["code"],
            cfg["redirect_uri"], _qp.get("state"),
        )
        st.session_state["mal_token"] = token
    except mc.MALError as exc:
        st.error(f"Не удалось войти в MyAnimeList: {exc}")
    st.query_params.clear()
    st.rerun()


st.title("🎬 Рекомендатель аниме по MyAnimeList")
st.caption(
    "Введите ник на MyAnimeList — программа возьмёт ваш список просмотренного "
    "и подберёт похожие тайтлы (гибрид: рекомендации MAL + совпадение жанров)."
)


@st.cache_data(show_spinner=False, ttl=3600)
def load_watched(username: str) -> list[mc.WatchedAnime]:
    return mc.fetch_completed_list(username)


@st.cache_data(show_spinner=False, ttl=3600)
def load_my_watched(access_token: str) -> list[mc.WatchedAnime]:
    return mc.fetch_my_completed_list(access_token)


def get_access_token() -> str | None:
    """Действующий access-токен авторизованного пользователя (с автообновлением)."""
    token = st.session_state.get("mal_token")
    if not token:
        return None
    access, fresh = mc.valid_access_token(token, cfg["client_id"], cfg["client_secret"])
    st.session_state["mal_token"] = fresh
    return access


logged_in = "mal_token" in st.session_state

with st.sidebar:
    st.header("Настройки")
    username = st.text_input(
        "Ник на MyAnimeList",
        placeholder="ваш список (вы вошли)" if logged_in else "например, Xinil",
        help=(
            "Вы вошли — оставьте пустым, чтобы использовать свой список "
            "(работает и с приватным профилем). Укажите ник, чтобы получить "
            "рекомендации по списку другого человека."
            if logged_in else
            "Профиль на MyAnimeList должен быть публичным."
        ),
    )
    alpha = st.slider(
        "Баланс сигнала",
        min_value=0.0, max_value=1.0, value=0.6, step=0.05,
        help="0 — только совпадение жанров, 1 — только рекомендации MAL.",
    )
    final_count = st.slider("Сколько рекомендаций показать", 5, 40, 20, 5)
    use_all = st.checkbox(
        "Учесть все просмотренные тайтлы",
        help="Опрашивать рекомендации MAL по всему списку, а не только по топу.",
    )
    if use_all:
        st.warning(
            "⚠️ Это займёт время: на каждый просмотренный тайтл идёт отдельный "
            "запрос к API (~0.5 c из-за лимитов). Сотни тайтлов — это несколько "
            "минут ожидания."
        )
    top_titles = st.slider(
        "Сколько любимых тайтлов учитывать", 5, 30, 15, 1,
        help="Больше — точнее, но дольше из-за лимитов API.",
        disabled=use_all,
    )
    go = st.button("Подобрать рекомендации", type="primary", use_container_width=True)

    st.divider()
    st.subheader("MyAnimeList")
    if not cfg["client_id"]:
        st.info(
            "Чтобы отмечать тайтлы как просмотренные прямо в MAL, добавьте "
            "`MAL_CLIENT_ID` в файл `.env` (см. `.env.example`)."
        )
    elif "mal_token" in st.session_state:
        name = st.session_state.get("mal_username")
        if name is None:
            try:
                name = mc.fetch_my_user_name(get_access_token())
            except mc.MALError:
                name = None
            st.session_state["mal_username"] = name or ""
        st.success(f"Вы вошли как **{name}**" if name else "Вы вошли в MyAnimeList")
        if st.button("Выйти", use_container_width=True):
            for key in ("mal_token", "mal_username"):
                st.session_state.pop(key, None)
            st.rerun()
    else:
        st.caption("Войдите, чтобы отмечать аниме просмотренными прямо в своём списке.")
        st.link_button(
            "🔑 Войти через MyAnimeList",
            mc.build_auth_url(cfg["client_id"], cfg["redirect_uri"]),
            use_container_width=True,
        )


SCORE_OPTIONS = list(range(0, 11))  # 0 — без оценки


def _mark_watched(r: rec.Recommendation) -> None:
    """Записывает тайтл в список MAL как просмотренный и убирает его из выдачи."""
    score = st.session_state.get(f"score_{r.mal_id}", 0)
    try:
        token = get_access_token()
        episodes = mc.fetch_anime_episodes(token, r.mal_id)
        mc.update_list_status(token, r.mal_id, status=mc.LIST_STATUS_COMPLETED,
                              score=score or None, num_watched_episodes=episodes or None)
    except mc.MALAuthError as exc:
        st.session_state.pop("mal_token", None)
        st.error(f"{exc}")
        return
    except mc.MALError as exc:
        st.error(f"Не удалось записать в MAL: {exc}")
        return
    st.session_state["recs"] = [x for x in st.session_state.get("recs", []) if x.mal_id != r.mal_id]
    note = f" с оценкой {score}" if score else ""
    st.toast(f"«{r.title}» отмечено просмотренным на MAL{note}.", icon="✅")


def render_recommendations(recs: list[rec.Recommendation]) -> None:
    logged_in = "mal_token" in st.session_state
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

                if logged_in:
                    sc, btn = st.columns([1, 1])
                    sc.selectbox(
                        "Оценка", SCORE_OPTIONS, key=f"score_{r.mal_id}",
                        format_func=lambda x: "— оценка" if x == 0 else f"⭐ {x}",
                        label_visibility="collapsed",
                    )
                    btn.button(
                        "✅ Смотрел", key=f"mark_{r.mal_id}", use_container_width=True,
                        on_click=_mark_watched, args=(r,),
                    )


if go:
    uname = username.strip()
    if not uname and not logged_in:
        st.warning("Введите ник на MyAnimeList или войдите слева.")
        st.stop()

    session = requests.Session()

    # 1. Грузим список просмотренного: свой — через API, чужой — по нику.
    spinner_text = f"Загружаю список «{uname}»…" if uname else "Загружаю ваш список…"
    with st.spinner(spinner_text):
        try:
            if uname:
                watched = load_watched(uname)
            else:
                watched = load_my_watched(get_access_token())
        except mc.MALError as exc:
            st.error(str(exc))
            st.stop()

    if not watched:
        st.warning("В списке просмотренного (completed) пусто — рекомендовать нечего.")
        st.stop()

    # Краткая сводка вкусов.
    profile = rec.build_genre_profile(watched)
    top_genres = []
    if profile:
        top_genres = [g for g, _ in sorted(profile.items(), key=lambda kv: kv[1], reverse=True)[:8]]

    # 2. Считаем рекомендации с прогресс-баром.
    progress_bar = st.progress(0.0, text="Готовлюсь…")

    def on_progress(frac: float, text: str) -> None:
        progress_bar.progress(min(frac, 1.0), text=text)

    effective_top = len(watched) if use_all else top_titles
    try:
        recs = rec.recommend(
            watched,
            top_titles=effective_top,
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

    # Сохраняем результат в сессии, чтобы он переживал перерисовки при отметках.
    st.session_state["recs"] = recs
    st.session_state["watched_count"] = len(watched)
    st.session_state["top_genres"] = top_genres


# --- Отрисовка из состояния сессии ------------------------------------------
recs = st.session_state.get("recs")
if recs:
    st.success(f"Загружено просмотренных тайтлов: {st.session_state.get('watched_count', 0)}")
    if st.session_state.get("top_genres"):
        st.markdown("**Ваши любимые жанры:** " + ", ".join(st.session_state["top_genres"]))
    st.subheader(f"Рекомендуем вам ({len(recs)})")
    if "mal_token" not in st.session_state and cfg["client_id"]:
        st.caption("👈 Войдите через MyAnimeList слева, чтобы отмечать тайтлы просмотренными.")
    render_recommendations(recs)
elif recs == []:
    st.warning(
        "Не удалось собрать рекомендации — у выбранных тайтлов их нет на MAL. "
        "Попробуйте увеличить число учитываемых тайтлов."
    )
else:
    st.info("👈 Введите ник в панели слева и нажмите «Подобрать рекомендации».")
