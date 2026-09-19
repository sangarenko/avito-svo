#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avito_classify.py — модуль классификации загруженной страницы.

classify(page) -> 'items' | 'block' | 'captcha' | 'empty' | 'unknown'
    items   — выдача с карточками;
    block   — «Доступ ограничен» / «проблема с IP» / «проверка безопасн.»;
    captcha — живой GeeTest-виджет в DOM;
    empty   — страница загрузилась, но карточек нет;
    unknown — навигационная ошибка / белый лист.

Плюс: click_block_continue (кнопка «Продолжить» на мягком бане) и
detect_login_state (детект входа в аккаунт Авито).
"""

from __future__ import annotations

import json

from avito_common import LOGIN_STATE_FILE, log, now_iso

#: CSS-маркеры, чья видимость означает залогиненного пользователя.
_LOGIN_CSS_MARKERS = [
    "[data-marker='user-avatar']",
    "[data-marker='header/avatar']",
    "[data-testid*='avatar']",
    ".user-summary",
    "button[data-marker='user-name']",
    "a[data-marker='user-name']",
]

#: имена cookie (в нижнем регистре), означающие авторизованную сессию.
_LOGIN_COOKIES = {
    "auth", "sessid", "sessionid", "avito_user", "userid", "login"}


def classify(page) -> str:
    """Classify the currently loaded page (см. докстринг модуля)."""
    try:
        content = page.content()
    except Exception:
        return "unknown"
    # NOTE: the geetest check must run BEFORE the block-text check: the
    # soft-ban page («Доступ ограничен ... нажмите Продолжить») keeps its
    # block text while the captcha widget is already rendered on top of
    # it — classifying that combined state as 'captcha' lets the solver
    # lift the ban; pure IP blocks have no geetest markers at all.
    if (".geetest" in content
            or "geetest_box" in content
            or "geetest_wrap" in content):
        # подтверждаем виджет в DOM: одна лишь ссылка на static.geetest.com
        # в скриптах нормальной страницы — НЕ капча (иначе ложный 'captcha'
        # и бессмысленный стоп прогона). Ошибка локатора — ведём себя как
        # старая проверка (conservative).
        try:
            widget = page.locator(
                ".geetest_box, .geetest_wrap, .geetest_bg, "
                ".geetest_btn").count()
        except Exception:
            widget = 1
        if widget > 0:
            return "captcha"
    if ("Доступ ограничен" in content
            or "проблема с IP" in content
            or "проверка безопасности" in content):
        return "block"
    try:
        if page.locator('[data-marker="item-title"]').count() > 0:
            return "items"
    except Exception:
        pass
    # Avito renders the listing client-side: domcontentloaded fires long
    # before the item markers exist. Wait a bit for them before declaring
    # the page empty — otherwise CSR pages get misclassified as 'empty'
    # and dropped (root cause of the "block после ретрая" spam).
    try:
        page.wait_for_selector(
            '[data-marker="item-title"]', timeout=6000)
        if page.locator('[data-marker="item-title"]').count() > 0:
            return "items"
    except Exception:
        pass
    try:
        if page.title():
            return "empty"
    except Exception:
        pass
    return "unknown"


def click_block_continue(page) -> bool:
    """On the soft-ban page («Доступ ограничен ... очень много запросов»)
    press the «Продолжить» button which leads to a captcha that lifts
    the block. Returns True when the click was made."""
    try:
        loc = page.get_by_role("button", name="Продолжить")
        if loc.count() > 0 and loc.first.is_visible():
            loc.first.click()
            log("block: нажата «Продолжить» (мягкий бан → капча)")
            return True
    except Exception:
        pass
    return False


def detect_login_state(page) -> bool:
    """Detect whether the page belongs to a logged-in Avito session.

    Tries, in order, until the first hit:
      0. FAST PATH: while on /login (or any *login* URL) the session is
         anonymous by definition — anonymous pages also carry the
         «Избранное» link and cookie noise, which must never count;
      1. a combined wait for the CSS avatar/user markers (~1.5 s);
      2. the text 'Мои объявления' anywhere (~1.5 s);
      3. known auth cookies in the browser context.
    The favorites-link heuristic was REMOVED: /favorites exists for
    anonymous users too (false positive). The result is also written to
    LOGIN_STATE_FILE as {"logged_in": bool, "ts": iso}. Any failure ->
    False (never raises).
    """
    logged = False
    try:
        try:
            # фрагмент (#login?authsrc=h) — это модалка на главной, а НЕ
            # страница /login: срезаем его до проверки пути, иначе
            # успешный вход через модалку никогда не детектится
            url = (page.url or "").lower().split("#")[0]
        except Exception:
            url = ""
        if "/login" in url or "login" in (
                url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or ""):
            logged = False
        else:
            try:
                page.wait_for_selector(
                    ", ".join(_LOGIN_CSS_MARKERS), timeout=1500)
                logged = True
            except Exception:
                pass
            if not logged:
                try:
                    page.wait_for_selector(
                        "text=Мои объявления", timeout=1500)
                    logged = True
                except Exception:
                    pass
            if not logged:
                try:
                    names = {c.get("name", "").lower()
                             for c in page.context.cookies()}
                    if names & _LOGIN_COOKIES:
                        logged = True
                except Exception:
                    pass
    except Exception:
        logged = False
    try:
        with open(LOGIN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"logged_in": logged, "ts": now_iso()}, f,
                      ensure_ascii=False)
    except OSError:
        pass
    return logged
