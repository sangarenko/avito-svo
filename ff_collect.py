#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ff_collect.py — коллектор Авито: поиск «сво по контракту» на Firefox.

Режимы (аргумент --mode):
  * search (основной) — поиск «сво по контракту» по региону
    Москва и МО, сортировка «По дате»; тег БД search_svo_kontrakt;
  * category — вход через категории: Главная → «Работа и
    подработка» → «Военный» → «Сначала из Москвы и МО» → «По
    дате»; тег БД rabota_voennyi (в расписании не используется:
    по поиску выходит в 5-6 раз больше номеров на карточку).

Ключевая механика:
  * БД пишется ПО ХОДУ сбора: каждый собранный номер попадает в
    базу сразу после находки; карточки без кнопки телефона
    фиксируются (no_phone) — следующий прогон их не трогает;
  * обход СТРОГО ПО ПОРЯДКУ: карточки сортируются по вертикальной
    позиции на странице (DOM у Авито виртуализированный и
    перемонтируется не по порядку), обработка сверху вниз, в логе
    виден порядковый номер;
  * дедуп по описанию: нормализованный заголовок уже встречался
    (в этом прогоне или в базе с номером) → карточка пропускается
    целиком, без hover; клоны пишутся в БД со статусом dup_title;
  * ускорение: круг без живых карточек (всё известное/клоны) —
    быстрая прокрутка крупным шагом; hover только для новых;
  * мягкий блок «Доступ ограничен» снимается нажатиями «Продолжить»
    и обновлением страницы (F5), капча не решается — жёсткий
    cooldown 20 минут;
  * авария не теряет данные: БД сохраняется каждые 5 номеров, при
    закрытии окна/ошибке несохранённое спасается в except-ветке;
  * SIGTERM = мягкая остановка: текущая карточка доканчивается,
    всё собранное сохраняется, итог пишется с stop_reason=stopped;
  * сперва подключение к резидентному браузеру
    (ff_browser_daemon.js — вкладка Авито открыта постоянно),
    фолбэк — свой persistent-профиль ff_profile/.

Окружение: Firefox (антидетект Win10 FF128, прокси socks5 →
Москва), сессия юзера из avito_session.json (экспорт монитором
реального Firefox), flock .parser.lock (два прогона одновременно
невозможны), телефоны из hover-попапа «Показать телефон»
(картинка → OCR) без захода в объявления, OCR-кэш по md5 картинки.

Запуск (юнит/вручную):
  DISPLAY=:1 AVITO_PROXY=socks5://127.0.0.1:10808 \
      python3 /root/avito-svo/ff_collect.py \
      [--mode search] [--max-phones 0] [--max-pages 0] [--time-budget 1500]
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import random
import re
import signal
import sys
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

try:
    from zoneinfo import ZoneInfo
    _MSK = ZoneInfo("Europe/Moscow")
except Exception:                                    # pragma: no cover
    _MSK = None

BASE = Path("/root/avito-svo")
PROFILE = str(BASE / "ff_profile")
SHOTS = BASE / "shots_ff"
SHOTS.mkdir(exist_ok=True)
SESSION_FILE = BASE / "avito_session.json"
LAST_RUN = BASE / "ff_last_run.json"
LOCK_FILE = BASE / ".parser.lock"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
      "Gecko/20100101 Firefox/128.0")
PROXY = os.environ.get("AVITO_PROXY", "socks5://127.0.0.1:10808")

LOGIN_COOKIES = {"auth", "sessid", "sessionid", "avito_user", "userid",
                 "login", "sess", "avito_user_id"}

#: URL категории «Военный» (из истории реального Firefox юзера)
VOENNYI_URL = ("https://www.avito.ru/moskva_i_mo/vakansii/"
               "voennyi-ASgBAgICAUTUzBHq6oYD")

#: режим поиска: запрос по умолчанию и регион выдачи
SEARCH_QUERY_DEFAULT = "сво по контракту"
REGION_URL = "https://www.avito.ru/moskva_i_mo"
#: теги источников в БД: сбор через категорию / через поиск
TAG_CATEGORY = "rabota_voennyi"
TAG_SEARCH = "search_svo_kontrakt"

PHONE_RE = re.compile(
    r"(?:\+7|8)[\s\(\-]*\d{3}[\s\)\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
AD_ID_RE = re.compile(r"(\d{8,})")

INIT_JS = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "Object.defineProperty(navigator,'platform',{get:()=>'Win32'});"
)
FF_PREFS = {
    "general.platform.override": "Win32",
    "privacy.trackingprotection.enabled": False,
    "browser.safebrowsing.malware.enabled": False,
    "browser.safebrowsing.phishing.enabled": False,
    "toolkit.telemetry.enabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "media.peerconnection.enabled": False,
    "browser.shell.checkDefaultBrowser": False,
}

#: OCR-кэш: md5 картинки-номера -> телефон (клоны одного рекрутёра
#: дают байт-в-байт одинаковые картинки — tesseract достаточно 1 раз)
_OCR_CACHE: dict = {}

#: мягкая остановка (SIGTERM от кнопки «Остановить парсер»)
_STOP = {"flag": False}

#: ЖЁСТКИЙ блок: «Доступ ограничен» не снялся за 3 попытки —
#: страница останавливается сразу, без многочасовых ретраев;
#: остывает ~20 мин (до этого новые попытки не делаются —
#: следующий плановый час всё равно повторит прогон)
_BLOCK_HARD = {"ts": 0.0}
_BLOCK_COOLDOWN = 20 * 60


def _on_sigterm(signum, frame) -> None:
    _STOP["flag"] = True


_t0 = time.time()
_shot_n = 0


def LOG(msg: str) -> None:
    print("[%6.1fs] %s" % (time.time() - _t0, msg), flush=True)


def shot(page, label: str) -> None:
    global _shot_n
    _shot_n += 1
    try:
        page.screenshot(path=str(SHOTS / ("%02d_%s.png" % (_shot_n, label))))
        LOG("[shot] %s" % label)
    except Exception as e:
        LOG("[shot] %s FAIL %s" % (label, str(e)[:60]))


def pause(a: float, b: float) -> None:
    time.sleep(random.uniform(a, b))


def now_msk() -> datetime:
    """Время МСК (как весь пайплайн — эксель/бот/журналы)."""
    if _MSK is not None:
        return datetime.now(_MSK)
    return datetime.now(timezone.utc) + timedelta(hours=3)


# ---------------------------------------------------------------- сессия

def norm_expires(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return -1.0
    if f > 1e11:          # moz_cookies отдаёт миллисекунды
        f = f / 1000.0
    return f if f > 0 else -1.0


def add_session_cookies(ctx) -> bool:
    """Вставить куки юзера; True только если есть куки логина."""
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        LOG("avito_session.json не читается: %s" % e)
        return False
    cookies = state.get("cookies", [])
    names = {(c.get("name") or "").lower() for c in cookies}
    if not (names & LOGIN_COOKIES):
        LOG("в сессии нет кук логина (вход не экспортирован)")
        return False
    ok = 0
    for c in cookies:
        cc = dict(c)
        cc["expires"] = norm_expires(cc.get("expires"))
        ss = cc.get("sameSite")
        if ss not in ("Strict", "Lax", "None"):
            cc.pop("sameSite", None)
        if cc.get("sameSite") == "None" and not cc.get("secure"):
            cc.pop("sameSite", None)
        cc.setdefault("path", "/")
        try:
            ctx.add_cookies([cc])
            ok += 1
        except Exception:
            pass
    LOG("куки юзера: добавлено %d" % ok)
    return True


def login_ok(page) -> bool:
    try:
        cks = page.context.cookies("https://www.avito.ru")
        names = {c["name"].lower() for c in cks}
        ok = bool(names & LOGIN_COOKIES)
        LOG("проверка входа по кукам: %s (%s)"
            % ("OK" if ok else "НЕТ",
               sorted(names & LOGIN_COOKIES)[:4] or "-"))
        return ok
    except Exception as e:
        LOG("проверка входа fail: %s" % e)
        return False


# ---------------------------------------------------------------- браузер

def resident_endpoint() -> str:
    """ws-эндпоинт резидентного браузера ('' — если его нет).

    Демон ff_browser_daemon.js держит Firefox открытым ПОСТОЯННО с
    вкладкой Авито (Авито флагал IP из-за браузера, который раньше
    поднимался и закрывался на каждый прогон). Heartbeat обязан быть
    свежим (< 180 c), иначе считаем демона мёртвым — фолбэк на свой
    persistent-профиль.
    """
    try:
        hb = BASE / "browser.heartbeat"
        ws = BASE / "browser.ws"
        if not (ws.exists() and hb.exists()):
            return ""
        if time.time() - hb.stat().st_mtime > 180:
            return ""
        ep = ws.read_text(encoding="utf-8").strip()
        return ep if ep.startswith("ws://") else ""
    except Exception:
        return ""


def launch(pw):
    """Сперва РЕЗИДЕНТНЫЙ браузер (вкладка Авито живёт постоянно).

    Подключаемся (playwright.connect) и открываем СВОЙ контекст-вкладку
    в уже работающем браузере; по завершению закрывается только он
    (ctx.close() в finally главного блока) — сам браузер и вкладка-демон
    остаются жить. Фолбэк — свой persistent-профиль, как раньше.
    """
    kwargs = dict(
        viewport={"width": 1280, "height": 800},
        screen={"width": 1280, "height": 900},
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        user_agent=UA,
    )
    px = PROXY.strip()
    if px:
        try:
            host, port = px.split("://", 1)[1].rsplit(":", 1)
            import socket
            s = socket.create_connection((host, int(port)), timeout=4)
            s.close()
            LOG("прокси: %s" % px)
        except Exception as e:
            LOG("!!! прокси %s недоступен (%s) — выходим" % (px, e))
            raise SystemExit(3)
    ep = resident_endpoint()
    if ep:
        try:
            browser = pw.firefox.connect(ep, timeout=20000)
            ctx = browser.new_context(**kwargs)
            ctx.add_init_script(INIT_JS)
            LOG("подключён к резидентному браузеру — вкладка Авито живёт "
                "постоянно, закрою только свою вкладку прогона")
            return ctx
        except Exception as e:
            LOG("резидентный браузер не подключился (%s) — свой Firefox"
                % str(e)[:120])
    launch_kw = dict(kwargs)
    launch_kw["headless"] = False
    if px:
        launch_kw["proxy"] = {"server": px}
    try:
        ctx = pw.firefox.launch_persistent_context(
            PROFILE, firefox_user_prefs=FF_PREFS, **launch_kw)
    except TypeError:
        ctx = pw.firefox.launch_persistent_context(PROFILE, **launch_kw)
    ctx.add_init_script(INIT_JS)
    LOG("Firefox запущен (профиль %s)" % PROFILE)
    return ctx


def dismiss_popups(page) -> None:
    for text in ("Хорошо", "Понятно", "Принять", "Всё верно"):
        try:
            loc = page.locator("button", has_text=text)
            for i in range(min(loc.count(), 3)):
                el = loc.nth(i)
                if el.is_visible():
                    el.click(timeout=2000)
                    LOG("закрыл попап «%s»" % text)
                    time.sleep(0.3)
                    return
        except Exception:
            pass


def page_state(page) -> str:
    """Классификация страницы (items/block/captcha/other)."""
    try:
        if page.locator("[data-marker='item']").count():
            return "items"
        t = (page.title() or "").lower()
        if "доступ ограничен" in t or "проблема с ip" in t:
            return "block"
        try:
            body = page.locator("body").inner_text()[:2500].lower()
        except Exception:
            body = ""
        if "доступ ограничен" in body:
            return "block"
        if page.locator(".geetest_btn, .geetest_radar").count():
            return "captcha"
        return "other"
    except Exception:
        return "other"


def goto(page, url: str, settle: float = 3.0, tries: int = 2) -> str:
    """goto + классификация; блок → бэкофф и повтор."""
    c = "other"
    for attempt in range(1, tries + 1):
        LOG("goto %s (попытка %d)" % (url.split("?")[0], attempt))
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            LOG("goto error: %s" % str(e)[:100])
        time.sleep(settle)
        dismiss_popups(page)
        c = page_state(page)
        LOG("страница: %s items=%d url=%s"
            % (c, page.locator("[data-marker='item']").count(),
               page.url[:100]))
        if c == "block" and attempt < tries:
            LOG("блок — пауза 20-35с и повтор")
            pause(20, 35)
            continue
        break
    return c


# ------------------------------------------------------------------- флоу

def step_home_to_rabota(page) -> bool:
    """Главная → плитка «Работа и подработка» → SERP вакансий."""
    c = goto(page, "https://www.avito.ru/")
    shot(page, "home")
    if not login_ok(page):
        return False
    try:
        tile = page.locator("a[href*='/vakansii']").filter(
            has_text=re.compile("Работа")).first
        LOG("клик плитка «Работа и подработка»...")
        tile.click(timeout=8000)
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        time.sleep(3)
        dismiss_popups(page)
    except Exception as e:
        LOG("плитка fail (%s) — прямой goto SERP" % str(e)[:70])
        goto(page, "https://www.avito.ru/moskva_i_mo/vakansii")
    if page.locator("[data-marker='item']").count() == 0:
        LOG("SERP вакансий пуст — прямой goto")
        goto(page, "https://www.avito.ru/moskva_i_mo/vakansii")
    shot(page, "vakansii")
    return page.locator("[data-marker='item']").count() > 0


def step_category_voennyi(page) -> bool:
    """Клик категории «Военный» (ссылка в панели; JS-клик)."""
    def _click_voennyi_link() -> str | None:
        try:
            return page.evaluate(
                """() => { const a = document.querySelector(
                    "a[href*='/voennyi']");
                    if (a) { a.click(); return a.getAttribute('href'); }
                    return null; }""")
        except Exception:
            return None

    href = _click_voennyi_link()
    if not href:
        LOG("ссылка «Военный» не в DOM — перезагружаю SERP целиком")
        goto(page, "https://www.avito.ru/moskva_i_mo/vakansii")
        time.sleep(1.5)
        href = _click_voennyi_link()
    if href:
        LOG("клик «Военный» (href=%s...)" % href[:60])
        try:
            page.wait_for_url(re.compile("voennyi"), timeout=20000)
        except Exception:
            pass
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        time.sleep(3)
        dismiss_popups(page)
    else:
        LOG("ссылка «Военный» опять не найдена — прямой goto категории")
        goto(page, VOENNYI_URL)

    if "voennyi" not in (page.url or ""):
        LOG("!!! категория не применилась (url=%s)" % page.url[:80])
        return False
    shot(page, "voennyi")
    LOG("SERP «Военный»: items=%d url=%s"
        % (page.locator("[data-marker='item']").count(), page.url[:100]))
    return page.locator("[data-marker='item']").count() > 0


def _try_search_input(page, query: str) -> bool:
    """Фолбэк: вбить запрос в строку поиска на главной Авито."""
    try:
        goto(page, "https://www.avito.ru/")
        inp = None
        for sel in ("input[data-marker='search-form/input']",
                    "input[placeholder*='поиск']",
                    "input[placeholder*='Поиск']",
                    "input[type='search']",
                    "input[data-marker='search-input']"):
            try:
                l = page.locator(sel)
                if l.count() and l.first.is_visible():
                    inp = l.first
                    break
            except Exception:
                continue
        if inp is None:
            LOG("строка поиска на главной не найдена")
            return False
        LOG("вбиваю запрос в строку поиска (фолбэк)")
        inp.click(timeout=4000)
        pause(0.3, 0.7)
        inp.fill(query)
        pause(0.4, 0.9)
        page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        time.sleep(3)
        dismiss_popups(page)
        return page.locator("[data-marker='item']").count() > 0
    except Exception as e:
        LOG("поиск через строку fail: %s" % str(e)[:70])
        return False


def step_search_query(page, query: str) -> bool:
    """Режим ПОИСКА: вход через запрос, НЕ через категорию.

    Прямой URL /moskva_i_mo?q=<запрос> — выдача по всем рубрикам
    региона (вакансии «сво по контракту» живут в т.ч. в «Другое»);
    фолбэк — строка поиска на главной. Дальше тот же конвейер:
    «По дате» → последовательный обход → hover-телефоны.
    """
    url = "%s?q=%s" % (REGION_URL, urllib.parse.quote_plus(query))
    c = goto(page, url)
    n = page.locator("[data-marker='item']").count()
    LOG("SERP поиска «%s»: %s · items=%d · url=%s"
        % (query, c, n, page.url[:110]))
    if n == 0:
        LOG("выдача поиска пустая — фолбэк через строку поиска")
        if _try_search_input(page, query):
            shot(page, "search")
            return True
        LOG("!!! поиск не дал выдачи (url=%s)" % page.url[:80])
        shot(page, "search_empty")
        return False
    shot(page, "search")
    return True


def _toggle_state(page):
    """Состояние тумблера «Сначала из Москвы и МО»: True/False/None.

    None = тумблер не найден/не распознан. Возвращает aria-checked
    role=switch-контейнера (наблюдается 'true'/'false'), фолбэк —
    признаки включённости в имени класса.
    """
    tog = page.locator("text=Сначала из Москвы")
    if not tog.count():
        return None
    try:
        raw = tog.first.evaluate(
            """e => { let b = e;
                for (let i = 0; i < 4 && b; i++) {
                  b = b.parentElement;
                  if (!b) break;
                  const r = b.getAttribute('role');
                  if (r === 'switch' ||
                      /switch|toggle/i.test(b.className || '')) {
                    return (r === 'switch' ?
                            b.getAttribute('aria-checked') :
                            b.className.toString().slice(0, 100));
                  }
                }
                return 'text'; }""")
    except Exception:
        return None
    s = str(raw).strip().lower()
    if s == "true":
        return True
    if s == "false":
        return False
    if s == "text":
        return None
    # фолбэк: класс-контейнер без role=switch
    return ("checked" in s or "active" in s or "-on" in s
            or "enabled" in s)


def ensure_moscow_first(page, allow_click: bool = True,
                        why: str = "") -> bool:
    """Галочка «Сначала из Москвы и Московской области» = ВКЛ.

    Кликаем ТОЛЬКО когда выключен: клик по включённому тумблеру
    его ВЫКЛЮЧАЕТ (безусловный клик гасил галочку,
    оставленную предыдущим прогоном — юзер видел «не нажал
    с Москвы и Московской области»). После клика состояние
    ПЕРЕЧИТЫВАЕМ и подтверждаем; неподтвердилось — один повтор.
    """
    tag = " (%s)" % why if why else ""
    try:
        state = _toggle_state(page)
        if state is None:
            LOG("тумблер «Сначала из Москвы...» не найден%s — "
                "пропускаю (категория уже moskva_i_mo)" % tag)
            return True
        if state:
            LOG("тумблер «Сначала из Москвы и МО»: УЖЕ ВКЛ — "
                "не трогаю%s" % tag)
            return True
        if not allow_click:
            LOG("!!! тумблер «Сначала из Москвы и МО» ВЫКЛ%s — "
                "клик запрещён на этом шаге" % tag)
            return False
        LOG("тумблер «Сначала из Москвы и МО» ВЫКЛ — включаю...%s"
            % tag)
        tog = page.locator("text=Сначала из Москвы")
        tog.first.click(timeout=5000)
        time.sleep(2.2)
        state2 = _toggle_state(page)
        if state2:
            LOG("тумблер: ПОДТВЕРЖДЕНО ВКЛ (url=%s)" % page.url[:120])
            shot(page, "toggle")
            return True
        # первый клик иногда съедается ре-рендером — один повтор
        LOG("тумблер: не подтвердился (%s) — повторный клик" % state2)
        tog.first.click(timeout=5000)
        time.sleep(2.2)
        state3 = _toggle_state(page)
        LOG("тумблер после повтора: %s (url=%s)"
            % ("ВКЛ — ок" if state3 else "ВСЁ ЕЩЁ ВЫКЛ!",
               page.url[:120]))
        shot(page, "toggle")
        return bool(state3)
    except Exception as e:
        LOG("тумблер fail: %s" % str(e)[:80])
        return False


def step_toggle_moscow_first(page) -> None:
    """Шаг 3: галочка «Сначала из Москвы и МО» — включить/не трогать."""
    ensure_moscow_first(page, allow_click=True, why="шаг 3, до сортировки")


def step_sort_by_date(page) -> None:
    """Сортировка «По дате»."""
    try:
        ctl = page.locator("[data-marker='sort/title']")
        if not ctl.count():
            ctl = page.locator("[data-marker*='sort']")
        ctl.first.click(timeout=5000)
        time.sleep(1.0)
        it = page.locator(":text-is('По дате')")
        if it.count():
            it.first.click(timeout=5000)
            time.sleep(2.5)
            LOG("сортировка «По дате»: url=%s" % page.url[:120])
        else:
            LOG("!!! пункт «По дате» не найден")
            page.keyboard.press("Escape")
    except Exception as e:
        LOG("сортировка fail: %s" % str(e)[:80])
    shot(page, "sorted")
    # сортировка могла сбросить галочку Москвы — вернуть ВКЛ
    ensure_moscow_first(page, allow_click=True, why="после «По дате»")


def hide_old_phone_imgs(page) -> None:
    """Спрятать старые картинки-номера (попапы прошлого шага)."""
    try:
        page.evaluate(
            """() => document.querySelectorAll(
                "img[data-marker*='phone']")
                .forEach(e => { e.style.display = 'none'; })""")
    except Exception:
        pass


def close_foreign_tabs(page) -> int:
    """Закрыть чужие вкладки (реклама открывает yoomoney и пр.).

    Реклама в попапах Авито иногда открывает НОВУЮ вкладку и пере-
    хватывает фокус окна — карточки Авито остаются в фоновой вкладке
    и hover по ним падает с таймаутом (замечено на поиске —
    вкладка yoomoney.ru). Закрываем всё не-Авито, основную вкладку
    возвращаем на передний план. Возвращает число закрытых вкладок.
    """
    n = 0
    try:
        for p in list(page.context.pages):
            if p is page:
                continue
            try:
                if "avito.ru" not in (p.url or ""):
                    p.close(timeout=2000)
                    n += 1
            except Exception:
                pass
        if n:
            LOG("закрыты чужие вкладки: %d (реклама) — вкладку Авито "
                "на передний план" % n)
        try:
            page.bring_to_front()
        except Exception:
            pass
    except Exception:
        pass
    return n


def block_modal_present(page) -> bool:
    """Мягкий блок «Доступ ограничен: проблема с IP» — модалка ПОВЕРХ
    выдачи (карточки остаются в DOM, поэтому page_state тут не годит-
    ся: он видит items и выходит). Модалка перекрывает список — все
    hover/клик падают с таймаутом (находка на поиске после ~5
    номеров подряд). Модалка живёт в portal-контейнере В КОНЦЕ DOM,
    поэтому смотрим и хвост текста страницы."""
    try:
        txt = page.locator("body").inner_text().lower()
    except Exception:
        return False
    return ("доступ ограничен" in txt[:3000]
            or "доступ ограничен" in txt[-4000:])


def dismiss_block(page, allow_reload: bool = True) -> bool:
    """Снять мягкий блок: «Продолжить» ×5 + обновление страницы (F5).

    Мягкий блок снимается обычными нажатиями «Продолжить» и
    ПРОСТЫМ ОБНОВЛЕНИЕМ страницы; капчу НЕ решаем — осталась после
    обновления → страницу останавливаем.
    Не снялся — ЖЁСТКИЙ флаг (cooldown): повтор не раньше 20 минут.
    """
    try:
        if time.time() - _BLOCK_HARD["ts"] < _BLOCK_COOLDOWN:
            return False            # недавно НЕ снялся — не жжём время
        if not block_modal_present(page):
            return True
        LOG("!!! «Доступ ограничен» (проблема с IP) — жму «Продолжить»")
        shot(page, "block")
        reloads = 0
        for attempt in range(1, 6):
            # после F5 (предыдущая итерация) блок мог уйти сам
            if attempt > 1 and not block_modal_present(page):
                LOG("блок снят — продолжаю обход")
                dismiss_popups(page)
                return True
            try:
                btn = page.locator("button", has_text="Продолжить")
                if btn.count():
                    btn.first.click(timeout=3000)
                else:
                    page.keyboard.press("Escape")
            except Exception:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
            pause(5, 9)
            if not block_modal_present(page):
                try:
                    if page.locator(".geetest_btn, .geetest_radar").count():
                        LOG("!!! после блока вылезла капча — страницу "
                            "останавливаю (ждём следующий прогон)")
                        _BLOCK_HARD["ts"] = time.time()
                        return False
                except Exception:
                    pass
                LOG("блок снят (попытка %d) — продолжаю обход" % attempt)
                dismiss_popups(page)
                return True
            # клики не берут — ОБНОВЛЯЕМ страницу (обычное
            # обновление снимает блок), затем снова «Продолжить»
            if allow_reload and attempt in (2, 4):
                reloads += 1
                LOG("блок держится — обновляю страницу (F5 №%d)" % reloads)
                try:
                    page.reload(timeout=45000)
                    pause(3, 5)
                except Exception:
                    pass
        LOG("!!! блок НЕ снялся за 5 попыток (клики + F5) — страницу "
            "останавливаю (cooldown %d мин)" % (_BLOCK_COOLDOWN // 60))
        _BLOCK_HARD["ts"] = time.time()
        return False
    except Exception as e:
        LOG("dismiss_block fail: %s" % str(e)[:60])
        return False


def norm_phone_txt(s: str) -> str | None:
    digits = re.sub(r"\D", "", s or "")
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    return None


def norm_title(s: str) -> str:
    """Нормализованное описание для дедупа (регистр/пунктуация)."""
    return re.sub(r"[^0-9a-zа-яё]+", " ", (s or "").lower()).strip()


def card_data(card) -> dict:
    d = {"title": "", "url": "", "price": "", "location": ""}
    try:
        t = card.locator("[data-marker='item-title']").first
        d["title"] = t.inner_text().replace("\n", " ").strip()[:140]
        href = t.get_attribute("href") or ""
        if href:
            href = href.split("?")[0]
            d["url"] = href if href.startswith("http") \
                else "https://www.avito.ru" + href
    except Exception:
        pass
    for marker, key in (("item-price", "price"),
                        ("item-location", "location")):
        try:
            loc = card.locator("[data-marker='%s']" % marker)
            if loc.count():
                d[key] = loc.first.inner_text().replace("\n", " ").strip()
        except Exception:
            pass
    try:
        sn = card.locator("[data-marker='item-snippet']")
        if sn.count():
            d["description"] = sn.first.inner_text() \
                .replace("\n", " ").strip()[:300]
    except Exception:
        pass
    m = AD_ID_RE.search(d["url"] or "")
    d["id"] = int(m.group(1)) if m else 0
    return d


def load_done_ids() -> set:
    """ID объявлений, которые уже просматривали за телефоном.

    Считаем просмотренными: с телефоном в БД и со статусами
    no_phone/error/dup_title (чтобы плановые прогоны не тыкали их
    заново). login_required НЕ считаем — вход есть, надо добрать.
    """
    ids = set()
    try:
        from avito_db import DB
        from avito_common import DB_PATH
        db = DB(DB_PATH)
        rows = db.con.execute(
            """SELECT id FROM ads
               WHERE (phone IS NOT NULL AND phone<>'')
                  OR (phone_status IS NOT NULL
                      AND phone_status NOT IN ('', 'login_required'))"""
        ).fetchall()
        db.con.close()
        # id в базе может лежать строкой (старые строки) — нормализуем
        ids = {int(r[0]) for r in rows if r[0] is not None
               and str(r[0]).isdigit()}
    except Exception as e:
        LOG("load_done_ids fail: %s" % e)
    return ids


def load_need_descr() -> set:
    """ID объявлений категории без описания (сниппета) в БД.

    Бэкfill: карточки монтируются при любом проходе — сниппет
    дописывается «попутно», без отдельного обхода.
    """
    ids = set()
    try:
        from avito_db import DB
        from avito_common import DB_PATH
        db = DB(DB_PATH)
        rows = db.con.execute(
            """SELECT id FROM ads
               WHERE category='rabota_voennyi'
                 AND (description IS NULL OR description='')""").fetchall()
        db.con.close()
        ids = {int(r[0]) for r in rows if r[0] is not None
               and str(r[0]).isdigit()}
    except Exception as e:
        LOG("load_need_descr fail: %s" % e)
    return ids


def save_descriptions(updates: dict) -> int:
    """Дописать сниппеты-описания к уже существующим строкам БД."""
    if not updates:
        return 0
    n = 0
    try:
        from avito_db import DB
        from avito_common import DB_PATH
        db = DB(DB_PATH)
        for ad_id, descr in updates.items():
            if not descr:
                continue
            db.con.execute(
                "UPDATE ads SET description=? WHERE id=?",
                (descr[:300], ad_id))
            n += 1
        db.con.commit()
        db.con.close()
    except Exception as e:
        LOG("save_descriptions fail: %s" % e)
    return n


def load_db_titles() -> set:
    """Нормализованные описания, для которых телефон уже собран.

    Плюс описания-клоны (dup_title) — их тоже не тыкаем повторно.
    """
    titles = set()
    try:
        from avito_db import DB
        from avito_common import DB_PATH
        db = DB(DB_PATH)
        rows = db.con.execute(
            """SELECT title FROM ads
               WHERE (phone IS NOT NULL AND phone<>'')
                  OR phone_status = 'dup_title'""").fetchall()
        db.con.close()
        for r in rows:
            t = norm_title(r[0] or "")
            if t:
                titles.add(t)
    except Exception as e:
        LOG("load_db_titles fail: %s" % e)
    return titles


_SNIPPET_MARKERS = ("item-snippet", "item-description", "item-text",
                     "item-subtitle", "item-params", "item-details")


def collect_ad_links(page, dump_markers: bool = False) -> list:
    """Смонтированные сейчас объявления, СОРТИРОВКА ПО ПОЗИЦИИ.

    DOM у Авито виртуализированный: при обратной прокрутке карточки
    перемонтируются и_APPENDятся в конец DOM — порядок DOM ≠ порядок
    на экране. Поэтому берём вертикальную координату каждой карточки
    (rect.top + scrollY) и сортируем по ней: обход строго сверху вниз.

    Заодно читаем СНИППЕТ карточки (серые строки условий/обязанностей
    под зарплатой — «Описание» в экселе) — без ховера, из того же
    JS-прохода, бесплатно; идёт на бэкfill описаний в БД.
    dump_markers=True — один раз залогировать все data-marker первой
    карточки (самодиагностика разметки).
    """
    ads = []
    try:
        raw = page.evaluate(
            r"""() => {
              const SNIP = %s;
              const links = Array.from(
                document.querySelectorAll("a[data-marker='item-title']"));
              const out = [];
              for (const a of links) {
                const r = a.getBoundingClientRect();
                let sn = '';
                const root = a.closest("[data-marker='item']");
                if (root) {
                  for (const m of SNIP) {
                    const e = root.querySelector(
                      "[data-marker='" + m + "']");
                    const t = e ? (e.innerText || '')
                                  .replace(/\s+/g, ' ').trim() : '';
                    if (t) { sn = t.slice(0, 300); break; }
                  }
                }
                out.push([a.getAttribute('href') || '',
                          (a.innerText || '').replace(/\s+/g, ' ')
                              .trim().slice(0, 120),
                          Math.round(r.top + window.scrollY), sn]);
              }
              return out.filter(x => x[0]);
            }""" % json.dumps(_SNIPPET_MARKERS))
        seen = set()
        for href, title, y, sn in raw:
            href = href.split("?")[0]
            m = AD_ID_RE.search(href)
            if not m:
                continue
            ad_id = int(m.group(1))
            if ad_id in seen:
                continue
            seen.add(ad_id)
            url = href if href.startswith("http") \
                else "https://www.avito.ru" + href
            ads.append({"id": ad_id, "url": url, "title": title,
                        "y": int(y or 0), "snippet": (sn or "")[:300]})
        ads.sort(key=lambda a: a["y"])
        if dump_markers and ads:
            try:
                marks = page.evaluate(
                    r"""() => {
                      const root = document.querySelector(
                        "[data-marker='item']");
                      if (!root) return [];
                      return Array.from(root.querySelectorAll('[data-marker]'))
                        .slice(0, 25)
                        .map(e => [e.getAttribute('data-marker'),
                                   (e.innerText || '')
                                   .replace(/\s+/g, ' ').trim()
                                   .slice(0, 90)]);
                    }""")
                LOG("разметка карточки: %s"
                    % "; ".join("%s=%r" % (mk, tx)
                                for mk, tx in marks))
            except Exception:
                pass
    except Exception as e:
        LOG("collect_ad_links fail: %s" % e)
    return ads


# ------- скролл: список Авито может жить в собственном overflow-контейнере
#: (тогда window.scrollY всегда 0 и window.scrollBy не работает);
#: JS-сниппет находит скролл-родителя карточки и возвращает его.
_SCROLL_JS = """() => {
    const it = document.querySelector("[data-marker='item']");
    let sc = null, el = it;
    while (el && el !== document.body) {
        el = el.parentElement;
        if (!el) break;
        const st = getComputedStyle(el);
        if (/(auto|scroll)/.test(st.overflowY)
                || /(auto|scroll)/.test(st.overflow)) {
            if (el.scrollHeight > el.clientHeight + 100) { sc = el; break; }
        }
    }
    return sc ? 'container' : 'window';
}"""


def _scroll_mode(page) -> str:
    try:
        return page.evaluate(_SCROLL_JS)
    except Exception:
        return "window"


def _scroll_by(page, dy: int) -> None:
    """Прокрутить ВНИЗ на dy: контейнер списка либо window."""
    try:
        page.evaluate(
            """([dy]) => {
                const it = document.querySelector("[data-marker='item']");
                let sc = null, el = it;
                while (el && el !== document.body) {
                    el = el.parentElement;
                    if (!el) break;
                    const st = getComputedStyle(el);
                    if (/(auto|scroll)/.test(st.overflowY)
                            || /(auto|scroll)/.test(st.overflow)) {
                        if (el.scrollHeight > el.clientHeight + 100)
                            { sc = el; break; }
                    }
                }
                if (sc) { sc.scrollTop += dy; }
                else { window.scrollBy(0, dy); }
            }""", [dy])
    except Exception:
        try:
            page.evaluate("dy => window.scrollBy(0, dy)", dy)
        except Exception:
            pass


def page_pos(page) -> dict:
    """Позиция прокрутки с учётом контейнера списка: {y, vh, h, mode}.

    Если список живёт в своём overflow-контейнере, window.scrollY
    всегда 0 — берём scrollTop/высоты контейнера. При сбое — «мы
    вверху бесконечной страницы» (низ не сработает ложно)."""
    try:
        return page.evaluate(
            """() => {
                const it = document.querySelector("[data-marker='item']");
                let sc = null, el = it;
                while (el && el !== document.body) {
                    el = el.parentElement;
                    if (!el) break;
                    const st = getComputedStyle(el);
                    if (/(auto|scroll)/.test(st.overflowY)
                            || /(auto|scroll)/.test(st.overflow)) {
                        if (el.scrollHeight > el.clientHeight + 100)
                            { sc = el; break; }
                    }
                }
                if (sc) {
                    return {y: Math.round(sc.scrollTop),
                            vh: Math.round(sc.clientHeight),
                            h: Math.round(sc.scrollHeight),
                            mode: 'container'};
                }
                return {y: Math.round(window.scrollY),
                        vh: Math.round(window.innerHeight),
                        h: Math.round(
                            document.documentElement.scrollHeight),
                        mode: 'window'};
            }""")
    except Exception:
        return {"y": -1, "vh": 0, "h": 10 ** 9, "mode": "?"}


def scroll_down(page, dy: int) -> None:
    """Прокрутить страницу вниз (колесо над центром списка; надёжный
    фолбэк — JS-прокрутка контейнера списка / window)."""
    try:
        before = page_pos(page)["y"]
        page.mouse.move(640, 400)          # центр списка, не левый край
        page.mouse.wheel(0, dy)
        time.sleep(0.25)
        if page_pos(page)["y"] != before:
            return
    except Exception:
        pass
    _scroll_by(page, dy)
    time.sleep(0.2)


def scroll_top(page) -> None:
    """Наверх страницы (и контейнера списка, и window)."""
    try:
        page.evaluate(
            """() => {
                const it = document.querySelector("[data-marker='item']");
                let el = it;
                while (el && el !== document.body) {
                    el = el.parentElement;
                    if (!el) break;
                    const st = getComputedStyle(el);
                    if (/(auto|scroll)/.test(st.overflowY)
                            || /(auto|scroll)/.test(st.overflow)) {
                        if (el.scrollHeight > el.clientHeight + 100)
                            { el.scrollTop = 0; }
                    }
                }
                window.scrollTo(0, 0);
            }""")
        time.sleep(0.8)
    except Exception:
        pass


def scroll_to_bottom(page, max_steps: int = 50) -> bool:
    """Докрутить страницу до низа (для пагинации внизу SERP).

    Проверяет флаг мягкой остановки на каждом шаге — кнопка
    «Остановить» не ждёт долгого доскролла."""
    for _ in range(max_steps):
        if _STOP["flag"]:
            return False
        pos = page_pos(page)
        if pos["y"] + pos["vh"] >= pos["h"] - 300:
            return True
        scroll_down(page, 1300)
        time.sleep(0.35)
    return False


def click_show_more(page) -> bool:
    """Кнопка «Показать ещё» — только НИЖЕ вьюпорта в основном столбце
    (кнопки-омонимы в сайдбаре/попапах не трогаем). True — если кликнули."""
    try:
        clicked = page.evaluate(
            """() => {
                const pos = document.querySelector(
                    "[data-marker='item']");
                let sc = null, el = pos;
                while (el && el !== document.body) {
                    el = el.parentElement;
                    if (!el) break;
                    const st = getComputedStyle(el);
                    if (/(auto|scroll)/.test(st.overflowY)
                            || /(auto|scroll)/.test(st.overflow)) {
                        if (el.scrollHeight > el.clientHeight + 100)
                            { sc = el; break; }
                    }
                }
                const top = sc ? sc.scrollTop : window.scrollY;
                const vh = sc ? sc.clientHeight : window.innerHeight;
                const els = Array.from(
                    document.querySelectorAll('button, a'));
                for (const b of els) {
                    const t = (b.innerText || '').trim();
                    if (!/^(Показать ещё|Показать больше|Загрузить ещё)/
                            .test(t)) continue;
                    const r = b.getBoundingClientRect();
                    const absY = r.top + (sc ? sc.scrollTop
                                             : window.scrollY);
                    if (r.width < 200) continue;       // мелочь в сайдбаре
                    if (absY < top + vh * 0.5) continue;  // выше — не то
                    b.click();
                    return t;
                }
                return null;
            }""")
        if clicked:
            time.sleep(1.2)
            LOG("клик «%s» — догружаю" % clicked)
            return True
    except Exception:
        pass
    return False


def locate_card(page, ad_id: int):
    """Карточка по ID; скролл ВНИЗ до неё (обход идёт по порядку).

    Так как обработка строго сверху вниз, нужная карточка почти
    всегда в текущем «окне» виртуализации или чуть ниже. Вверх
    смотрим только маленьким фолбэком (перемонтирование).
    """
    sel = "[data-marker='item']:has(a[href*='_%d'])" % ad_id
    loc = page.locator(sel)
    if loc.count():
        return loc.first
    for dy in (700, 700, 900, 900, 1200, 1200, 1500):
        _scroll_by(page, dy)
        time.sleep(0.9)
        loc = page.locator(sel)
        if loc.count():
            return loc.first
    for dy in (-600, -900):
        _scroll_by(page, dy)
        time.sleep(1.0)
        loc = page.locator(sel)
        if loc.count():
            return loc.first
    return None


def try_phone_from_card(page, card, i: int) -> str | None:
    """Hover → «Показать телефон» → картинка/текст → номер.

    OCR-кэш: у клонов одного рекрутёра картинка-номер байт-в-байт
    одинаковая — распознаём один раз, дальше берём из кэша.
    """
    hide_old_phone_imgs(page)
    try:
        card.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    try:
        card.hover(timeout=5000)
    except Exception as e:
        # ховеру мешает мягкий блок «Доступ ограничен» (модалка
        # поверх списка) или чужая вкладка — снимаем и пробуем снова
        if not dismiss_block(page):
            return "BLOCKED"
        close_foreign_tabs(page)
        try:
            card.hover(timeout=5000)
        except Exception:
            LOG("item[%d]: hover fail (после чистки): %s"
                % (i, str(e)[:60]))
            return "HOVER_FAIL"
        LOG("item[%d]: hover ок после снятия блока/чистки вкладок" % i)
    pause(1.4, 2.0)
    # кнопка телефона в карточке (появляется при hover)
    btn = None
    try:
        ipb = card.locator("[data-marker^='item-phone-button']")
        if ipb.count() and ipb.first.is_visible():
            btn = ipb.first
    except Exception:
        pass
    if btn is None:
        for txt in ("Показать телефон", "Позвонить"):
            try:
                loc = page.locator("button:has-text('%s')" % txt)
                for j in range(min(loc.count(), 8)):
                    el = loc.nth(j)
                    if el.is_visible():
                        btn = el
                        break
                if btn:
                    break
            except Exception:
                pass
    if btn is None:
        # возможно номер уже показан текстом в попапе
        try:
            txt = card.inner_text()[:600]
            m = PHONE_RE.search(txt)
            if m:
                ph = norm_phone_txt(m.group(0))
                if ph:
                    LOG("item[%d]: телефон текстом в карточке" % i)
                    return ph
        except Exception:
            pass
        LOG("item[%d]: кнопки телефона нет (пропуск)" % i)
        return None
    try:
        btn.click(timeout=5000)
    except Exception as e:
        LOG("item[%d]: клик кнопки fail %s" % (i, str(e)[:50]))
        return None
    pause(2.3, 3.0)
    # 1) номер картинкой (с md5-кэшем — клоны не гоняем через OCR)
    try:
        imgs = page.locator("img[data-marker*='phone']")
        for j in range(imgs.count()):
            el = imgs.nth(j)
            try:
                if not el.is_visible():
                    continue
                src = el.get_attribute("src") or ""
                if not src.startswith("data:image"):
                    continue
                data = base64.b64decode(src.split(",", 1)[1])
                key = hashlib.md5(data).hexdigest()
                if key in _OCR_CACHE:
                    ph = _OCR_CACHE[key]
                    if ph:
                        LOG("item[%d]: телефон по кэшу картинки %s"
                            % (i, ph))
                        return ph
                    continue
                (SHOTS / ("ocr_%d_%d.png" % (i, j))).write_bytes(data)
                from avito_cards import ocr_phone_image
                ph = ocr_phone_image(data)
                _OCR_CACHE[key] = ph
                if ph:
                    LOG("item[%d]: телефон OCR %s" % (i, ph))
                    return ph
            except Exception:
                continue
    except Exception:
        pass
    # 2) номер текстом в попапе
    try:
        for sel in ("[data-marker*='phone']", "[role='dialog']",
                    "[class*='popup']"):
            loc = page.locator(sel)
            for j in range(min(loc.count(), 10)):
                el = loc.nth(j)
                try:
                    if not el.is_visible():
                        continue
                    m = PHONE_RE.search(el.inner_text()[:400])
                    if m:
                        ph = norm_phone_txt(m.group(0))
                        if ph:
                            LOG("item[%d]: телефон текстом %s" % (i, ph))
                            return ph
                except Exception:
                    continue
    except Exception:
        pass
    # не блок ли помешал? (не пишем ложный no_phone в базу)
    if block_modal_present(page):
        return "BLOCKED"
    return None


def close_popup(page) -> None:
    try:
        page.mouse.move(6, 200)
        time.sleep(0.5)
        page.keyboard.press("Escape")
        time.sleep(0.3)
    except Exception:
        pass


# --------------------------------------------------------------------- БД

def save_to_db(rows: list, write_runs: bool = True,
               pages: int = 1, scanned: int = 0,
               unique: int = 0, dup_rows: list | None = None,
               no_rows: list | None = None,
               category: str = TAG_CATEGORY) -> dict:
    """rows: [{id,title,url,price,location,phone}] -> статистика.

    dup_rows — клоны (описание уже встречалось): пишутся БЕЗ номера
    со статусом dup_title (следующий прогон пропустит и их).
    no_rows — карточки БЕЗ кнопки телефона: статус no_phone —
    следующий прогон их не тыкает заново (ускорение повторов).
    category — тег источника в БД (категория «Военный» / поиск).
    write_runs=False — промежуточное сохранение (сразу после
    очередного номера); строка в runs одна на прогон — в финале.
    """
    out = {"new": 0, "old": 0, "saved": 0}
    if not rows and not dup_rows and not no_rows:
        return out
    try:
        from avito_db import DB
        from avito_common import DB_PATH
        db = DB(DB_PATH)
        for r in rows:
            res = db.upsert_ad({
                "id": r["id"],
                "title": r["title"],
                "price": r.get("price"),
                "url": r["url"],
                "region": "Москва и Московская область",
                "category": category,
            })
            upd = {"phone": r["phone"], "phone_status": "ok"}
            if r.get("description"):
                upd["description"] = r["description"][:300]
            db.update_card(r["id"], upd)
            out["new" if res == "new" else "old"] += 1
            out["saved"] += 1
        for r in (dup_rows or []):
            db.upsert_ad({
                "id": r["id"],
                "title": r["title"],
                "price": r.get("price"),
                "url": r["url"],
                "region": "Москва и Московская область",
                "category": category,
            })
            db.update_card(r["id"], {"phone_status": "dup_title"})
        for r in (no_rows or []):
            db.upsert_ad({
                "id": r["id"],
                "title": r["title"],
                "price": r.get("price"),
                "url": r["url"],
                "region": "Москва и Московская область",
                "category": category,
            })
            upd = {"phone_status": "no_phone"}
            if r.get("description"):
                upd["description"] = r["description"][:300]
            db.update_card(r["id"], upd)
        if write_runs:
            try:
                db.con.execute(
                    """INSERT INTO runs(ts, pages, found, new, cards,
                       phones_ok, stop_reason, login_state)
                       VALUES(?, ?, ?, ?, ?, ?, 'ff', 1)""",
                    (now_msk().isoformat(timespec="seconds"),
                     max(pages, 1), scanned, out["new"],
                     out["saved"] + len(dup_rows or [])
                     + len(no_rows or []), unique))
                db.con.commit()
            except Exception:
                pass
        db.con.close()
        LOG("БД: сохранено %d (новых %d)%s"
            % (out["saved"], out["new"],
               "" if write_runs else " [промежуточно]"))
    except Exception as e:
        LOG("БД fail: %s" % e)
    return out


# ------------------------------------------------------------ сбор

def new_state() -> dict:
    return {
        "done_ids": set(),      # просмотрено (в прогоне или раньше в БД)
        "seen_titles": set(),   # описания, встреченные в этом прогоне
        "db_titles": set(),     # описания с номером из БД (+клоны)
        "all_ids": set(),       # все id, когда-либо виденные в DOM
        "seen_phones": set(),   # уникальные номера прогона
        "rows": [],             # карточки с телефоном (для БД)
        "dup_rows": [],         # клоны по описанию (для БД, без номера)
        "no_rows": [],          # карточки без кнопки телефона (no_phone)
        "saved_idx": 0,         # сколько строк уже сохранено в БД
        "saved_dup_idx": 0,
        "saved_no_idx": 0,
        "category": TAG_CATEGORY,   # тег источника в БД
        "db_new": 0,            # накоплено «новых» строк в БД
        "scanned": 0,           # карточек наведено мышью
        "no_phone": 0,          # карточек без кнопки телефона
        "dups": 0,              # пропущено клонов по описанию
        "need_descr": set(),    # id в БД без описания (бэкfill)
        "descr_updates": {},    # id -> сниппет, ждёт записи в БД
        "descr_saved": 0,       # сколько описаний дописано в БД
    }


def st_line(st: dict, page_no: int) -> str:
    return ("страница %d · просмотрено %d · номеров %d · клонов "
            "пропущено %d · без телефона %d"
            % (page_no, st["scanned"], len(st["seen_phones"]),
               st["dups"], st["no_phone"]))


def flush_state(st: dict, final: bool = False, pages: int = 1) -> None:
    """Сохранить в БД ещё не сохраненное (авария не теряет данные).

    final=True — со строкой runs (одна на прогон, в финале).
    """
    unsaved = st["rows"][st["saved_idx"]:]
    unsaved_dups = st["dup_rows"][st["saved_dup_idx"]:]
    unsaved_no = st["no_rows"][st["saved_no_idx"]:]
    if not unsaved and not unsaved_dups and not unsaved_no \
            and not st["descr_updates"] and not final:
        return
    sv = save_to_db(
        unsaved, write_runs=final, pages=max(pages, 1),
        scanned=st["scanned"], unique=len(st["seen_phones"]),
        dup_rows=unsaved_dups, no_rows=unsaved_no,
        category=st.get("category") or TAG_CATEGORY)
    st["saved_idx"] = len(st["rows"])
    st["saved_dup_idx"] = len(st["dup_rows"])
    st["saved_no_idx"] = len(st["no_rows"])
    st["db_new"] += sv["new"]
    # бэкfill описаний (сниппетов) к уже собранным строкам
    if st["descr_updates"]:
        n = save_descriptions(st["descr_updates"])
        st["descr_saved"] += n
        if n:
            LOG("описания: дописано %d (всего за прогон %d)"
                % (n, st["descr_saved"]))
        st["descr_updates"] = {}


def process_ad(page, st: dict, ad: dict, idx: int, total: int,
               page_no: int) -> None:
    """Одна карточка: дедуп по описанию → hover → телефон."""
    ad_id = ad["id"]
    nt = norm_title(ad["title"])
    # ДЕДУП ПО ОПИСАНИЮ: уже встречалось (в прогоне или в базе с
    # номером) → телефон не собираем вовсе, карточку пропускаем
    if nt and (nt in st["seen_titles"] or nt in st["db_titles"]):
        st["done_ids"].add(ad_id)
        st["dups"] += 1
        st["dup_rows"].append({
            "id": ad_id, "title": ad["title"] or "",
            "url": ad["url"], "price": "", "location": "",
        })
        if st["dups"] <= 5 or st["dups"] % 15 == 0:
            LOG("№%d/%d · стр.%d: КЛОН — описание уже встречалось, "
                "телефон не собираем: %s"
                % (idx, total, page_no, (ad["title"] or "")[:60]))
        # клоны пишем в БД пачками — следующий прогон их пропустит
        if len(st["dup_rows"]) - st["saved_dup_idx"] >= 25:
            flush_state(st)
        return
    card = locate_card(page, ad_id)
    if card is None:
        st["done_ids"].add(ad_id)
        LOG("№%d/%d · стр.%d: карточка не найдена — пропуск"
            % (idx, total, page_no))
        return
    LOG("№%d/%d · стр.%d: %s"
        % (idx, total, page_no, (ad["title"] or "")[:70]))
    ph = try_phone_from_card(page, card, ad_id)
    if ph in ("HOVER_FAIL", "BLOCKED"):
        # ховеру мешал блок/оверлей — в БД НЕ пишем (не no_phone!),
        # карточку в этом прогоне пропускаем, следующий попробует
        st["done_ids"].add(ad_id)
        st["hover_fails"] = st.get("hover_fails", 0) + 1
        return
    if ph and ph not in st["seen_phones"]:
        shot(page, "phone_%d" % ad_id)      # скрин только новых номеров
    close_popup(page)
    st["scanned"] += 1
    st["done_ids"].add(ad_id)
    if nt:
        st["seen_titles"].add(nt)
    if not ph:
        st["no_phone"] += 1
        # фиксируем «без телефона» в БД — следующий прогон
        # не тыкает эту карточку заново (ускорение повторов)
        st["no_rows"].append({
            "id": ad_id, "title": ad["title"] or "",
            "url": ad["url"], "price": "", "location": "",
            "description": (ad.get("snippet") or "")[:300],
        })
        if len(st["no_rows"]) - st["saved_no_idx"] >= 25:
            flush_state(st)
        return
    d = card_data(card)
    d["id"] = ad_id
    d["url"] = d["url"] or ad["url"]
    d["title"] = d["title"] or ad["title"]
    d["phone"] = ph
    d["description"] = (ad.get("snippet") or d.get("description")
                        or "")[:300]
    st["rows"].append(d)
    if ph not in st["seen_phones"]:
        st["seen_phones"].add(ph)
        LOG(">>> СОБРАН %d: %s (№%d/%d, %s)"
            % (len(st["seen_phones"]), ph, idx, total,
               (d["title"] or "")[:50]))
        pause(2.5, 4.5)
        # КАЖДЫЙ номер пишется в БД СРАЗУ — база растёт по ходу
        # сбора, а не по окончанию парсера (краш/закрытие не теряет)
        flush_state(st)
    else:
        LOG("№%d/%d: номер %s уже записан (клон рекрутёра)"
            % (idx, total, ph))
        pause(0.8, 1.6)


def harvest_page(page, st: dict, deadline: float,
                 max_phones: int, max_cards: int,
                 page_no: int = 1) -> str:
    """ОДНА страница SERP: последовательный обход сверху вниз.

    Список виртуализированный: в DOM живёт только «окно» вокруг
    прокрутки, при обратной прокрутке карточки перемонтируются не
    по порядку. Поэтому каждый круг берём смонтированные карточки,
    СОРТИРУЕМ ПО ВЕРТИКАЛЬНОЙ ПОЗИЦИИ и обрабатываем непросмотрен-
    ные строго по порядку; затем докручиваем вниз — Авито догрузит
    следующую порцию. Порядок обхода = порядок на экране.
    Возврат: done/limit/cards_limit/time/stopped/stuck.
    """
    no_progress = 0
    prev_key = None
    rounds = 0
    seq = 0                    # порядковый номер обрабатываемой карточки
    MAX_ROUNDS = 600           # предохранитель от вечного цикла
    while time.time() < deadline and rounds < MAX_ROUNDS:
        if _STOP["flag"]:
            return "stopped"
        if time.time() - _BLOCK_HARD["ts"] < _BLOCK_COOLDOWN:
            return "blocked"     # жёсткий блок — страницу останавливаем
        rounds += 1
        # реклама открывает чужие вкладки (yoomoney и пр.) и
        # крадёт фокус — держим только вкладки Авито, нашу — спереди
        close_foreign_tabs(page)
        # 1) смонтированные карточки ПО ПОЗИЦИИ на экране
        ads = collect_ad_links(page, dump_markers=(rounds == 1))
        total_now = len(ads)
        new_ads = []
        for a in ads:
            st["all_ids"].add(a["id"])
            if a["id"] not in st["done_ids"]:
                new_ads.append(a)
            # бэкfill: у карточки из БД пусто описание — запомним
            # сниппет (запись партиями в flush_state)
            if a["id"] in st["need_descr"] and a.get("snippet"):
                st["descr_updates"][a["id"]] = a["snippet"]
                st["need_descr"].discard(a["id"])
        # 2) обработать непросмотренные — строго по порядку
        scanned_before = st["scanned"]
        if new_ads:
            no_progress = 0
            for ad in new_ads:
                if _STOP["flag"]:
                    return "stopped"
                if time.time() >= deadline:
                    return "time"
                if max_phones and len(st["seen_phones"]) >= max_phones:
                    return "limit"
                if max_cards and st["scanned"] >= max_cards:
                    return "cards_limit"
                seq += 1
                process_ad(page, st, ad, seq, max(total_now, seq),
                           page_no)
        # 3) позиция/прогресс: ключ включает и позицию прокрутки —
        # «застоем» считается только когда НЕ двигается ВООБЩЕ
        # (позиция стоит на месте и новых карточек нет); движение по
        # уже обработанной зоне застоями не считается (там до 15к px)
        pos = page_pos(page)
        key = (len(st["all_ids"]), pos["h"], pos["y"])
        at_bottom = pos["y"] + pos["vh"] >= pos["h"] - 500
        # 4) докрутить вниз — Авито догрузит следующую порцию.
        #    круг прошёл БЕЗ hover (все карточки известные или
        #    клоны) — БЫСТРАЯ прокрутка: крупнее шаг, короче пауза;
        #    появилась живая карточка — обычный «человеческий» темп
        if st["scanned"] > scanned_before:
            scroll_down(page, random.randint(700, 1100))
            pause(0.7, 1.5)
        else:
            scroll_down(page, random.randint(1500, 1900))
            pause(0.25, 0.5)
        # 5) страница кончилась? (внизу и новых карточек нет;
        # последний шанс — кнопка «Показать ещё» ниже вьюпорта)
        if key == prev_key:
            no_progress += 1
        else:
            no_progress = 0
        prev_key = key
        if rounds % 10 == 0:
            LOG("... доскролл: %s (низ: %s, высота %d, режим %s)"
                % (st_line(st, page_no),
                   "да" if at_bottom else "нет", pos["h"],
                   pos.get("mode", "?")))
            # мягкий блок мог выскочить прямо во время обхода —
            # модалка «Доступ ограничен» перекрывает список
            if not dismiss_block(page):
                return "blocked"
        if at_bottom and no_progress >= 2:
            if click_show_more(page):
                no_progress = 0
                continue
            LOG("низ страницы %d: карточек %d, просмотрено %d"
                % (page_no, len(st["all_ids"]), st["scanned"]))
            return "done"
        if no_progress >= 12:
            LOG("!!! список не двигается 12 кругов — доскролл до низа "
                "и проверю пагинацию")
            scroll_to_bottom(page)
            return "done" if click_show_more(page) else "stuck"
    if _STOP["flag"]:
        return "stopped"
    if time.time() >= deadline:
        return "time"
    return "stuck"


def goto_next_page(page, page_no: int) -> bool:
    """Клик «Следующая страница» в пагинации внизу SERP.

    Пагинация монтируется только у низа страницы (виртуализация) —
    сначала доскролл до низа, потом ищем кнопку."""
    def _find():
        for sel in ("[data-marker='pagination-button-next']",
                    "[data-marker='pagination-next']"):
            try:
                l = page.locator(sel)
                if l.count():
                    return l.first
            except Exception:
                pass
        for txt in ("Следующая страница", "Следующая"):
            try:
                l = page.locator("a, button").filter(
                    has_text=re.compile("^\\s*%s\\s*$" % txt))
                for j in range(min(l.count(), 6)):
                    if l.nth(j).is_visible():
                        return l.nth(j)
            except Exception:
                continue
        try:
            l = page.locator("a[href*='p=%d']" % (page_no + 1))
            if l.count():
                return l.first
        except Exception:
            pass
        return None

    loc = _find()
    if loc is None:
        LOG("пагинация не смонтирована — доскролл до низа")
        scroll_to_bottom(page)
        loc = _find()
    if loc is None:
        LOG("страница %d: «Следующая» не найдена — категория вся" % page_no)
        return False
    try:
        try:
            loc.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        loc.click(timeout=5000)
    except Exception as e:
        LOG("клик «Следующая» fail (%s) — JS-клик" % str(e)[:50])
        try:
            loc.evaluate("el => el.click()")
        except Exception as e2:
            LOG("JS-клик тоже fail: %s" % str(e2)[:60])
            return False
    time.sleep(2.0)
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    time.sleep(2.5)
    dismiss_popups(page)
    state = page_state(page)
    LOG("перейдена страница %d: items=%d (%s) url=%s"
        % (page_no + 1, page.locator("[data-marker='item']").count(),
           state, page.url[:110]))
    return state == "items"


# -------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-phones", type=int, default=0,
                    help="0 = без лимита (все разные номера)")
    ap.add_argument("--max-cards", type=int, default=0,
                    help="0 = без лимита")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="0 = пока в пагинации есть «Следующая»")
    ap.add_argument("--time-budget", type=int, default=1500,
                    help="бюджет прогона, сек (по умолчанию 25 мин)")
    ap.add_argument("--mode", choices=("category", "search"),
                    default="category",
                    help="category — через категорию «Военный»; "
                         "search — через строку поиска Авито")
    ap.add_argument("--query", default=SEARCH_QUERY_DEFAULT,
                    help="текст запроса (для --mode search)")
    ap.add_argument("--category-tag", default="",
                    help="тег источника в БД (по умолчанию по режиму)")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _on_sigterm)

    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        LOG("!! другой парсер уже работает (.parser.lock)")
        return 2

    result = {
        "ok": False,
        "source": ("ff_search" if args.mode == "search"
                   else "ff_voennyi"),
        "query": (args.query if args.mode == "search" else ""),
        "max_phones": args.max_phones,
        "max_pages": args.max_pages,
        "time_budget": args.time_budget,
        "phones": [],
        "rows": [],
        "found": 0,
        "new": 0,
        "pages": 0,
        "scanned": 0,
        "total_ads": 0,
        "no_phone": 0,
        "dups": 0,
        "stop_reason": "unknown",
        "time": now_msk().isoformat(timespec="seconds"),
    }

    st = new_state()
    if args.category_tag:
        st["category"] = args.category_tag
    elif args.mode == "search":
        st["category"] = TAG_SEARCH
    pages_done = 0

    def _progress_json(stop: str | None = None) -> None:
        result["phones"] = sorted(st["seen_phones"])
        result["rows"] = st["rows"]
        result["found"] = st["scanned"]
        result["pages"] = pages_done
        result["scanned"] = st["scanned"]
        result["total_ads"] = len(st["all_ids"])
        result["no_phone"] = st["no_phone"]
        result["dups"] = st["dups"]
        result["new"] = st["db_new"]
        result["descriptions"] = st["descr_saved"]
        if stop:
            result["stop_reason"] = stop
        _write_json(result)

    try:
        with sync_playwright() as pw:
            ctx = launch(pw)
            if not add_session_cookies(ctx):
                result["stop_reason"] = "no_session"
                LOG("!!! нет сессии — сначала вход юзера в Firefox")
                ctx.close()
                return _finish(result)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                # 1-2. Выдача: категория «Военный» ИЛИ поиск по запросу
                if args.mode == "search":
                    if not step_search_query(page, args.query):
                        result["stop_reason"] = "no_serp"
                        return _finish(result)
                else:
                    # 1. Главная → Работа
                    if not step_home_to_rabota(page):
                        result["stop_reason"] = "no_serp"
                        return _finish(result)
                    # 2. Категория «Военный»
                    if not step_category_voennyi(page):
                        result["stop_reason"] = "no_category"
                        return _finish(result)
                # 3. Галочка «Сначала из Москвы и МО» (на поиске её
                # может не быть — регион уже в URL, это не ошибка)
                step_toggle_moscow_first(page)
                # 4. Сортировка «По дате»
                step_sort_by_date(page)

                # 5. ПОСЛЕДОВАТЕЛЬНЫЙ ОБХОД ВСЕЙ КАТЕГОРИИ
                deadline = time.time() + args.time_budget
                done_ids = load_done_ids()
                if done_ids:
                    LOG("уже просмотрено раньше (телефон/клон/нет "
                        "телефона): %d — пропустим" % len(done_ids))
                st["done_ids"] |= done_ids
                st["db_titles"] = load_db_titles()
                if st["db_titles"]:
                    LOG("описаний с номером в базе: %d — их клоны "
                        "пропустим" % len(st["db_titles"]))
                st["need_descr"] = load_need_descr()
                if st["need_descr"]:
                    LOG("строк без описания в базе: %d — допишем "
                        "сниппеты попутно" % len(st["need_descr"]))
                LOG("=== СБОР (%s) ПО ПОРЯДКУ: бюджет %d сек, "
                    "лимит номеров: %s, тег БД: %s ==="
                    % ("поиск «%s»" % args.query
                       if args.mode == "search"
                       else "категория «ВОЕННЫЙ»",
                       args.time_budget,
                       "нет" if not args.max_phones else args.max_phones,
                       st["category"]))

                stop = "done"
                page_no = 1
                while True:
                    scroll_top(page)
                    if page_no > 1:
                        # фильтр «Сначала из Москвы и МО» должен держаться
                        # ВКЛ и на следующих страницах пагинации
                        ensure_moscow_first(
                            page, allow_click=True,
                            why="стр.%d перед обходом" % page_no)
                    r = harvest_page(page, st, deadline,
                                     args.max_phones, args.max_cards,
                                     page_no=page_no)
                    pages_done = page_no
                    _progress_json()
                    flush_state(st)
                    LOG("=== СТРАНИЦА %d ГОТОВА (%s): %s ==="
                        % (page_no, r, st_line(st, page_no)))
                    if r in ("limit", "time", "cards_limit", "stopped",
                             "blocked"):
                        stop = r
                        break
                    if args.max_pages and page_no >= args.max_pages:
                        stop = "pages_limit"
                        break
                    if not goto_next_page(page, page_no):
                        stop = "done"
                        break
                    if args.mode == "search":
                        ok_url = "q=" in (page.url or "")
                    else:
                        ok_url = "voennyi" in (page.url or "")
                    if not ok_url:
                        LOG("!!! ушёл с выдачи (url=%s)" % page.url[:80])
                        stop = "redirect"
                        break
                    page_no += 1

                # 6. финальное сохранение
                flush_state(st, final=True, pages=pages_done)
                result["ok"] = True
                _progress_json(stop)
                shot(page, "final")
            except Exception:
                LOG("ФАТАЛ:\n%s" % traceback.format_exc())
                # СПАСЕНИЕ ДАННЫХ: закрытие окна/сбой не теряет номера
                flush_state(st, final=True, pages=max(pages_done, 1))
                _progress_json("error")
                result["stop_reason"] = "error"
            finally:
                try:
                    # прокрученная сессия (Авито ротирует куки при
                    # активности) — сохраняем, чтобы не устарела
                    ctx.storage_state(path=str(SESSION_FILE))
                    LOG("сессия обновлена и сохранена -> %s" % SESSION_FILE)
                except Exception:
                    pass
                try:
                    ctx.close()
                except Exception:
                    pass
    finally:
        lock.close()
    return _finish(result)


def _finish(result: dict) -> int:
    _write_json(result)
    LOG("ИТОГ: ok=%s страниц=%s просмотрено=%s номеров=%d (%s) "
        "описаний=%d stop=%s"
        % (result.get("ok"), result.get("pages"),
           result.get("scanned"),
           len(result.get("phones") or []),
           ", ".join(result.get("phones") or []),
           result.get("descriptions") or 0,
           result["stop_reason"]))
    return 0 if result.get("ok") else 1


def _write_json(result: dict) -> None:
    try:
        tmp = str(LAST_RUN) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        os.replace(tmp, LAST_RUN)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
