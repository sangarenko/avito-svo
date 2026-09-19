#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avito_browser.py — модуль Chromium-контекстов парсера.

Контексты двух видов:
    * СВЕЖИЙ анонимный (use_session=False) — для поисковых страниц:
      Авито банит по сессии, первая загрузка в свежем контексте почти
      всегда проходит (каждая страница выдачи открывается своим
      контекстом — см. svo_parser);
    * С СЕССИЕЙ (use_session=True) — для карточек/входа: storage_state
      из avito_session.json (появляется после /войти).

Прокси читается из env AVITO_PROXY (socks5://127.0.0.1:10808 на сервере),
недоступный прокси молча отключается.
"""

from __future__ import annotations

import json
import os

from avito_common import SESSION_FILE, log

#: Modern Chrome user agent.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
#: Browser viewport.
VIEWPORT = {"width": 1366, "height": 900}

#: Chromium launch args. --no-sandbox is required because the server runs
#: headed Chromium as root; --disable-dev-shm-usage for small VPS /dev/shm.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]


def load_storage() -> dict | None:
    """Load the saved playwright storage_state dict, or None if unusable."""
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "cookies" in data:
                return data
    except (OSError, ValueError) as e:
        log("load_storage: %s" % e)
    return None


def save_session(context) -> None:
    """Dump context storage_state to SESSION_FILE (never raises)."""
    try:
        context.storage_state(path=SESSION_FILE)
        log("session saved -> %s" % SESSION_FILE)
    except Exception as e:
        log("save_session failed: %s" % e)


#: keeps launched Browser objects referenced so Python GC never closes them
_LAUNCHED_BROWSERS = []


def make_context(pw, headless: bool = False, use_session: bool = True):
    """Launch Chromium and return a BrowserContext tuned for Avito.

    pw is the object returned by sync_playwright().start(). When a saved
    storage state exists it is reused (logged-in session), otherwise a
    fresh context is created. The context gets a Chrome UA, 1366x900
    viewport, ru-RU locale and the Moscow timezone.
    """
    browser = pw.chromium.launch(headless=headless, args=LAUNCH_ARGS)
    _LAUNCHED_BROWSERS.append(browser)
    kwargs = dict(viewport=VIEWPORT, user_agent=UA,
                  locale="ru-RU", timezone_id="Europe/Moscow")
    _px = os.environ.get("AVITO_PROXY", "").strip()
    if _px:
        try:
            _hp = _px.split("://", 1)[1].rsplit(":", 1)
            import socket as _socket
            _s = _socket.create_connection((_hp[0], int(_hp[1])), timeout=3)
            _s.close()
        except Exception:
            _px = ""
    if _px:
        kwargs["proxy"] = {"server": _px}
        log("browser context: proxy %s" % _px)
    storage = load_storage() if use_session else None
    if storage:
        try:
            context = browser.new_context(storage_state=storage, **kwargs)
            log("browser context: reused saved session (%s)" % SESSION_FILE)
            return context
        except Exception as e:
            log("storage_state failed (%s), fresh context" % e)
    context = browser.new_context(**kwargs)
    log("browser context: fresh (no session)")
    return context


def close_context(context) -> None:
    """Полностью закрыть контекст И его браузер (никогда не бросает).

    Браузер закрываем обязательно: свежий контекст под каждую страницу
    = новый Chromium на страницу, незакрытые = утечка целых браузеров.
    """
    if context is None:
        return
    try:
        browser = context.browser
    except Exception:
        browser = None
    try:
        context.close()
    except Exception:
        pass
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
