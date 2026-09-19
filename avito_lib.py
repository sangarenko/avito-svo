#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avito_lib.py — совместимый шим над модульной структурой.

Библиотека разбита на модули:

    avito_common   — пути, логирование, паузы, время МСК
    avito_geo      — город/регион: URL поиска, вкладки, канонизация
    avito_filter   — авто-мусор: фильтр объявлений + запрет вкладок
    avito_db       — SQLite: ads / sellers / runs
    avito_browser  — Chromium-контексты, сессия, прокси
    avito_classify — классификация страниц (items/block/captcha/empty)
    avito_captcha  — GeeTest v4 слайдер-солвер
    avito_extract  — выдача SERP: items + вкладки категорий
    avito_cards    — карточки: продавец + телефон (OCR)
    avito_nav      — smart_goto навигация с анти-блоком

Этот файл НЕ содержит логики: он реэкспортирует прежнее API, чтобы
excel_report.py и старые импорты продолжали работать без правок.
Новый код импортирует модули напрямую.

Self-test солвера сохранён: python3 avito_lib.py [bg.png slice.png]
"""

from __future__ import annotations

import sys

from avito_common import (  # noqa: F401
    BASE_DIR, DB_PATH, SESSION_FILE, LOGIN_STATE_FILE, LAST_RUN_FILE,
    SHOTS_DIR, PARSER_LOG, PLAYWRIGHT_AVAILABLE, sync_playwright,
    msk_now, now_iso, rnd, human_pause, log, shot, save_last_run,
)
from avito_geo import (  # noqa: F401
    SEARCH_QUERY, SEARCH_URL, RUBRICATOR_URL, ALL_RUSSIA_BASE,
    CITY_SLUGS, canonical_search_url, tab_url, serp_slug,
    clean_query_params,
)
from avito_filter import (  # noqa: F401
    AUTO_URL_SLUGS, AUTO_TAB_SLUGS, AUTO_TITLE_MARKERS,
    looks_like_auto, is_auto_tab,
)
from avito_db import DB, ADS_EXTRA  # noqa: F401
from avito_browser import (  # noqa: F401
    UA, VIEWPORT, LAUNCH_ARGS,
    load_storage, save_session, make_context, close_context,
    _LAUNCHED_BROWSERS,
)
from avito_classify import classify, click_block_continue, \
    detect_login_state  # noqa: F401
from avito_captcha import (  # noqa: F401
    GT_HANDLE_SELECTORS, GT_REFRESH_SELECTORS,
    captcha_solves, solve_gap_x, solve_geetest, human_drag,
)
from avito_extract import extract_items, extract_tabs  # noqa: F401
from avito_cards import (  # noqa: F401
    PHONE_BUTTON_SELECTORS, AUTH_TEXTS, PHONE_RE, PHONE_IMG_SELECTOR,
    MAX_PHONES_PER_SESSION, ocr_phone_image,
    get_phone, extract_seller, process_card,
)
from avito_nav import smart_goto  # noqa: F401


__all__ = [
    "BASE_DIR", "DB_PATH", "SESSION_FILE", "LOGIN_STATE_FILE",
    "LAST_RUN_FILE", "SHOTS_DIR", "SEARCH_URL", "PARSER_LOG",
    "UA", "VIEWPORT",
    "LAUNCH_ARGS", "PHONE_BUTTON_SELECTORS", "AUTH_TEXTS",
    "PLAYWRIGHT_AVAILABLE",
    "msk_now", "now_iso", "rnd", "human_pause", "log", "shot",
    "save_last_run", "looks_like_auto",
    "DB",
    "load_storage", "save_session", "make_context", "detect_login_state",
    "classify", "extract_items", "extract_tabs", "captcha_solves",
    "solve_gap_x", "solve_geetest", "human_drag",
    "get_phone", "extract_seller", "process_card", "smart_goto",
    "PHONE_IMG_SELECTOR", "ocr_phone_image", "MAX_PHONES_PER_SESSION",
    "SEARCH_QUERY", "RUBRICATOR_URL", "ALL_RUSSIA_BASE", "CITY_SLUGS",
    "canonical_search_url", "tab_url", "serp_slug", "clean_query_params",
    "AUTO_TAB_SLUGS", "is_auto_tab", "close_context",
]


if __name__ == "__main__":
    # прежний контракт самотеста солвера: avito_lib.py [bg slice]
    from avito_captcha import _self_test
    sys.exit(_self_test())
