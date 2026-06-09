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


@st.cache_data(show_spinner=False, ttl=86400)
def load_genre_map() -> dict[str, int]:
    """Соответствие «жанр → id» (меняется крайне редко — кэшируем на сутки)."""
    return mc.fetch_genre_id_map()


@st.cache_data(show_spinner=False, ttl=86400)
def cached_genre_search(genre_id: int, order_by: str, start_date: str | None, sfw: bool) -> list[dict]:
    """Кэшированный поиск кандидатов по жанру (переживает перезагрузку и повтор)."""
    return mc.search_anime_by_genre(genre_id, order_by=order_by, start_date=start_date, sfw=sfw)


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
    final_count = st.slider("Сколько рекомендаций показать", 5, 40, 20, 5)
    st.caption("Рекомендации строятся по вашим любимым жанрам с поправкой на новизну.")

    show_nsfw = st.checkbox(
        "Показывать 18+ (NSFW)",
        help="Включает взрослый контент и жанры вроде Hentai в подборе и фильтрах.",
    )
    _nsfw_genres = {"Hentai", "Erotica"}
    try:
        _genre_options = sorted(
            g for g in load_genre_map() if show_nsfw or g not in _nsfw_genres
        )
    except mc.MALError:
        _genre_options = []
    if _genre_options:
        exclude_genres = st.multiselect(
            "Исключить жанры/демографию", _genre_options,
            help="Тайтлы с этими тегами не предлагать (например, Shounen, Ecchi).",
        )
        extra_genres = st.multiselect(
            "Добавить жанры в поиск", _genre_options,
            help="Искать ещё и по этим жанрам, даже если их мало в просмотренном.",
        )
    else:
        exclude_genres, extra_genres = [], []

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
        auth_url = mc.build_auth_url(cfg["client_id"], cfg["redirect_uri"])
        # target="_blank" — открываем MAL в НОВОЙ вкладке. На Streamlit Cloud
        # приложение работает в iframe с песочницей, которая блокирует переход
        # в верхнем окне (target="_top"/"_self" → клик «молчит»). Новая вкладка
        # не ограничена песочницей: пользователь авторизуется там, MAL вернёт
        # его на адрес приложения с ?code=, и вход завершится в этой же вкладке.
        st.markdown(
            f'<a href="{auth_url}" target="_blank" rel="noopener" '
            f'style="display:inline-block;box-sizing:border-box;width:100%;'
            f'text-align:center;padding:0.5rem 1rem;background-color:#ff4b4b;'
            f'color:#ffffff;border-radius:0.5rem;font-weight:600;'
            f'text-decoration:none;">🔑 Войти через MyAnimeList (новая вкладка)</a>',
            unsafe_allow_html=True,
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


def explain(r: rec.Recommendation) -> str:
    """Короткое пояснение «почему показано» из данных ранжирования (без API)."""
    parts: list[str] = []
    if r.matched_genres:
        parts.append("🏷️ совпало по жанрам: " + ", ".join(r.matched_genres[:4]))
    if r.year and r.year >= rec.CURRENT_YEAR - rec.FRESH_YEARS:
        parts.append(f"🆕 свежее ({r.year})")
    elif r.year:
        parts.append(f"📅 {r.year}")
    if r.mal_score >= 7.5:
        parts.append(f"⭐ высокая оценка ({r.mal_score:.1f})")
    return " · ".join(parts) if parts else "по вашим жанрам"


def render_recommendations(recs: list[rec.Recommendation], *, interactive: bool = True) -> None:
    """Рисует карточки. interactive=False — лёгкое превью без кнопок (во время анализа)."""
    can_mark = interactive and "mal_token" in st.session_state
    cols_per_row = 6
    for row_start in range(0, len(recs), cols_per_row):
        row = recs[row_start:row_start + cols_per_row]
        cols = st.columns(cols_per_row)
        for col, r in zip(cols, row):
            with col:
                if r.image_url:
                    st.image(r.image_url, use_container_width=True)
                year = f" · {r.year}" if r.year else ""
                title = f"**[{r.title}]({r.url})**" if r.url else f"**{r.title}**"
                st.markdown(title + f"<br><span style='color:gray;font-size:0.8em'>{year}</span>"
                            if year else title, unsafe_allow_html=True)
                st.progress(min(r.score, 1.0), text=f"Совпадение: {r.score * 100:.0f}%")
                st.caption(explain(r))

                if can_mark:
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

    # Новый анализ — сбрасываем прошлый результат.
    st.session_state.pop("recs", None)

    # 1. Грузим список просмотренного: свой — через API, чужой — по нику.
    spinner_text = f"Загружаю список «{uname}»…" if uname else "Загружаю ваш список…"
    with st.spinner(spinner_text):
        try:
            watched = load_watched(uname) if uname else load_my_watched(get_access_token())
        except mc.MALError as exc:
            st.error(str(exc))
            st.stop()

    if not watched:
        st.warning("В списке просмотренного (completed) пусто — рекомендовать нечего.")
        st.stop()

    profile = rec.build_genre_profile(watched)
    top_genres = [g for g, _ in sorted(profile.items(), key=lambda kv: kv[1], reverse=True)[:8]]

    genre_map = load_genre_map()

    # 2. Прогрессивный подбор: тайтлы появляются по мере опроса жанров.
    progress_bar = st.progress(0.0, text="Готовлюсь…")
    preview = st.empty()
    final: list[rec.Recommendation] = []
    try:
        for partial, frac, text in rec.recommend_iter(
            watched,
            genre_id_map=genre_map,
            search_fn=cached_genre_search,
            final_count=final_count,
            extra_genres=extra_genres,
            exclude_genres=exclude_genres,
            sfw=not show_nsfw,
        ):
            final = partial
            progress_bar.progress(min(frac, 1.0), text=text)
            with preview.container():
                if partial:
                    st.caption("Подбираю рекомендации…")
                    render_recommendations(partial, interactive=False)
    except mc.MALError as exc:
        st.error(str(exc))
        st.stop()
    finally:
        progress_bar.empty()
        preview.empty()

    # Сохраняем результат в сессии: держится до следующего запуска анализа.
    st.session_state["recs"] = final
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
        "Не удалось собрать рекомендации по жанрам. Возможно, в списке слишком "
        "мало тайтлов с жанрами — попробуйте другой профиль или повторите позже."
    )
else:
    st.info("👈 Введите ник в панели слева и нажмите «Подобрать рекомендации».")
