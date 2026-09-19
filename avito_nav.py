#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avito_nav.py — модуль навигации: smart_goto с анти-блоком.

goto() с классификацией результата и лечением помех:
    BLOCK («Доступ ограничен»)  -> кнопка «Продолжить» -> капча -> solve;
    CAPTCHA (GeeTest)           -> solve_geetest -> re-classify;
    иначе                       -> бэкофф 10-20 с и повтор (до tries).

Возвращает класс итогового состояния страницы ('items' / 'block' /
'captcha' / 'empty' / 'unknown') — вызывающий решает, что делать.
"""

from __future__ import annotations

from avito_captcha import solve_geetest
from avito_classify import classify, click_block_continue
from avito_common import human_pause, log


def smart_goto(page, url: str, tries: int = 2) -> str:
    """goto() with block backoff and captcha solving; returns classification.

    On a BLOCK page: first try the «Продолжить» escape hatch (soft
    «too many requests» ban is lifted via captcha); if that fails,
    wait a random 10-20 s and retry (up to `tries` loads).
    On a CAPTCHA page: run solve_geetest() and re-classify.
    """
    c = "unknown"
    for attempt in range(1, tries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            log("smart_goto: goto error: %s" % e)
        human_pause(0.5, 1.5)
        c = classify(page)
        log("smart_goto: %s -> %s (attempt %d/%d)"
            % (url.split("?")[0], c, attempt, tries))
        if c == "block" and click_block_continue(page):
            human_pause(1, 2)
            # the captcha widget renders on top of the block text with
            # a delay — wait for it before re-classifying
            try:
                page.wait_for_selector(
                    ".geetest_btn, .geetest_box, .geetest_wrap",
                    timeout=8000)
            except Exception:
                pass
            c = classify(page)
            log("smart_goto: после «Продолжить» -> %s" % c)
            if c == "captcha" and solve_geetest(page):
                human_pause(2, 3)
                c = classify(page)
                log("smart_goto: после капчи -> %s" % c)
            if c != "block":
                break
        if c == "captcha":
            # на мягком блоке реальный виджет активируется кнопкой
            # «Продолжить»: до клика в DOM есть geetest-разметка, но
            # слайдера (.geetest_btn) ещё нет
            if click_block_continue(page):
                human_pause(1, 2)
            try:
                page.wait_for_selector(".geetest_btn", timeout=8000)
            except Exception:
                pass
            if solve_geetest(page):
                human_pause(2, 3)
                c = classify(page)
                log("smart_goto: после капчи -> %s" % c)
            if c != "captcha":
                break
            if attempt < tries:
                human_pause(4, 8)
                continue
        if c == "block" and attempt < tries:
            log("smart_goto: block page, backing off 10-20 s")
            human_pause(10, 20)
            continue
        break
    if c == "captcha":
        if solve_geetest(page):
            c = classify(page)
    return c
