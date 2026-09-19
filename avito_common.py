#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avito_common.py — общие константы и маленькие хелперы парсера Авито.

Модульная структура парсера:
    avito_common   — пути, логирование, паузы, время МСК      (этот файл)
    avito_geo      — город/регион: URL поиска, вкладки, канонизация
    avito_filter   — авто-мусор: фильтр объявлений + запрет вкладок
    avito_db       — SQLite: ads / sellers / runs
    avito_browser  — Chromium-контексты, сессия, прокси
    avito_classify — классификация страниц (items/block/captcha/empty)
    avito_captcha  — GeeTest v4 слайдер-солвер
    avito_extract  — выдача SERP: items + вкладки категорий
    avito_cards    — карточки: продавец + телефон (OCR)
    avito_nav      — smart_goto навигация с анти-блоком
    avito_lib      — совместимый шим: реэкспорт всего старого API

Зависимости: только stdlib. numpy/Pillow/playwright живут в своих
модулях, чтобы этот импортировался где угодно.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    _MSK = ZoneInfo("Europe/Moscow")
except Exception:  # pragma: no cover - missing tzdata
    _MSK = None

# ============================================================================
# Пути (env AVITO_BASE_DIR перекрывает корень, как и раньше)
# ============================================================================

#: Корень проекта на сервере.
BASE_DIR = os.environ.get("AVITO_BASE_DIR", "/root/avito-svo")
try:
    os.makedirs(BASE_DIR, exist_ok=True)
except OSError as _e:  # pragma: no cover - e.g. no permissions locally
    print("[avito_common] WARNING: cannot create BASE_DIR=%s: %s"
          % (BASE_DIR, _e), file=sys.stderr)

#: SQLite-база: ads / sellers / runs.
DB_PATH = os.path.join(BASE_DIR, "avito.db")
#: playwright storage_state (cookies + localStorage).
SESSION_FILE = os.path.join(BASE_DIR, "avito_session.json")
#: json с последним детектом входа.
LOGIN_STATE_FILE = os.path.join(BASE_DIR, "login_state.json")
#: json с итогом последнего прогона парсера.
LAST_RUN_FILE = os.path.join(BASE_DIR, "last_run.json")
#: папка отладочных скриншотов.
SHOTS_DIR = os.path.join(BASE_DIR, "shots")
try:
    os.makedirs(SHOTS_DIR, exist_ok=True)
except OSError as _e:  # pragma: no cover
    print("[avito_common] WARNING: cannot create SHOTS_DIR=%s: %s"
          % (SHOTS_DIR, _e), file=sys.stderr)

#: Живой журнал прогонов: log() дублирует сюда каждую строку, веб-форма
#: читает tail для прогресса сбора (svo_parser обрезает файл в начале
#: каждого прогона).
PARSER_LOG = os.path.join(BASE_DIR, "parser.log")

# playwright опционален на этапе импорта: модуль импортируем и без него
# (чистые тесты солвера на любой машине).
try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - local venv without playwright
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False


# ============================================================================
# Время (МСК)
# ============================================================================


def msk_now() -> datetime:
    """Current time in the Europe/Moscow timezone (naive local fallback)."""
    if _MSK is not None:
        return datetime.now(_MSK)
    # fallback: сервер без tzdata живёт в UTC — даём хотя бы верное
    # московское время, чтобы first_seen-строки не расходились на 3 часа
    return datetime.utcnow() + timedelta(hours=3)  # pragma: no cover


def now_iso() -> str:
    """Moscow time as an ISO-8601 string (used for DB timestamps)."""
    return msk_now().isoformat(timespec="seconds")


# ============================================================================
# Человеческие паузы и логирование
# ============================================================================


def rnd(a: float, b: float) -> float:
    """Uniform random float in [a, b]."""
    return random.uniform(a, b)


def human_pause(a: float, b: float) -> None:
    """Sleep a random [a, b] seconds - mimics a slow human."""
    time.sleep(rnd(a, b))


def log(msg: str) -> None:
    """Print '[YYYY-MM-DD HH:MM:SS] msg' to stdout, unbuffered.

    Строка также дописывается в PARSER_LOG — живой прогресс сбора
    оттуда читает веб-форма (tail). Сбой записи молча игнорируется.
    """
    line = "[%s] %s" % (msk_now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(PARSER_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def shot(page, prefix: str):
    """Save a debug screenshot into SHOTS_DIR; NEVER raises.

    Returns the saved path or None on failure.
    """
    try:
        ts = msk_now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SHOTS_DIR, "%s_%s_%03d.png"
                            % (prefix, ts, random.randint(0, 999)))
        try:
            page.screenshot(path=path)
        except TypeError:  # older playwright without path kwarg support
            data = page.screenshot()
            with open(path, "wb") as f:
                f.write(data)
        return path
    except Exception as e:  # never raise from a debug helper
        try:
            log("shot(%s) failed: %s" % (prefix, e))
        except Exception:
            pass
        return None


def save_last_run(d: dict) -> None:
    """Persist small run metadata (ts, stop_reason, ...) to LAST_RUN_FILE."""
    try:
        with open(LAST_RUN_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log("save_last_run failed: %s" % e)
