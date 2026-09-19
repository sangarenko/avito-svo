#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avito_extract.py - модуль разбора выдачи SERP Авито.

extract_items(page) - карточки выдачи -> dicts {id, title, price, url,
    region, category?, is_auto}: метка авто-мусора только СТАВИТСЯ,
    выкидывает items вызывающий код (svo_parser / excel_report);
    items без парсящегося id/url пропускаются.

extract_tabs(page) - ВКЛАДКИ категорий выдачи (рубрикатор сверху SERP,
    data-marker="category[NNNN]"): у каждой вкладки-ссылки слаг и
    название; «Все категории» (hasBack) - не вкладка, а выход в
    полный рубрикатор, пропускается. Текущая вкладка добаляется из
    URL страницы (Авито уже редиректнул поиск в доминантную категорию).
    Авто-вкладки НЕ отсеиваются здесь - это делает вызывающий код
    через avito_filter.is_auto_tab (модули не смешиваем).
"""

from __future__ import annotations

import re

from avito_filter import looks_like_auto
from avito_geo import serp_slug


def _category_from_url(url: str) -> str:
    """Средний сегмент ссылки карточки = slug категории Авито."""
    try:
        parts = (url or "").split("/")
        if len(parts) >= 6 and "avito" in (parts[2] or ""):
            return parts[4]
    except Exception:
        pass
    return ""


def extract_items(page) -> list:
    """Extract ad stubs from a loaded search-results page.

    Each dict: {id, title, price, url, region, category?, is_auto}.
    `category` - slug категории Авито из ссылки карточки (заполняется,
    когда удалось вытащить); `is_auto` - метка транспортного мусора
    (looks_like_auto): extract_items ТОЛЬКО ПОМЕЧАЕТ такие items,
    выкидывает их вызывающий код (svo_parser / excel_report).
    Items without a parseable id/url are skipped. All per-field failures
    degrade to None.
    """
    items = []
    try:
        locs = page.locator('[data-marker="item"]')
        n = locs.count()
    except Exception:
        return items
    for i in range(n):
        item = locs.nth(i)
        d = {"id": None, "title": None, "price": None, "url": None,
             "region": None}
        try:
            tl = item.locator('[data-marker="item-title"]')
            if tl.count() > 0:
                d["title"] = tl.first.inner_text().strip() or None
        except Exception:
            pass
        # url: prefer the title link itself, then any link inside the item
        href = None
        for sel in ('[data-marker="item-title"]', 'a[href*="/"]'):
            try:
                loc = item.locator(sel)
                if loc.count() > 0:
                    href = loc.first.get_attribute("href")
                    if href:
                        break
            except Exception:
                continue
        if not href:
            continue
        if href.startswith("http"):
            d["url"] = href
        elif href.startswith("/"):
            d["url"] = "https://www.avito.ru" + href
        else:
            d["url"] = "https://www.avito.ru/" + href
        try:
            m = re.search(r"_(\d+)$", d["url"].split("?")[0].split("#")[0])
            if m:
                d["id"] = int(m.group(1))
        except Exception:
            pass
        if d["id"] is None:
            continue
        # price: text first, then meta itemprop=price content
        try:
            pr = item.locator('[data-marker="item-price"]')
            if pr.count() > 0:
                d["price"] = pr.first.inner_text().strip() or None
        except Exception:
            pass
        if d["price"] is None:
            try:
                meta = item.locator("meta[itemprop='price']")
                if meta.count() > 0:
                    d["price"] = meta.first.get_attribute("content") or None
            except Exception:
                pass
        # region / address
        try:
            ad = item.locator('[data-marker="item-address"]')
            if ad.count() > 0:
                d["region"] = ad.first.inner_text().strip() or None
        except Exception:
            pass
        if d["region"] is None:
            try:
                geo = item.locator(".geo-address")
                if geo.count() > 0:
                    d["region"] = geo.first.inner_text().strip() or None
            except Exception:
                pass
        # категория со страницы не приходит: средний сегмент ссылки
        # карточки = slug категории Авито; пишем только когда реально есть
        if not d.get("category"):
            cat = _category_from_url(d.get("url") or "")
            if cat:
                d["category"] = cat
        # метка «не авто» (транспорт / запчасти / водительские вакансии)
        d["is_auto"] = looks_like_auto(d.get("title") or "",
                                       d.get("url") or "",
                                       d.get("category") or "")
        items.append(d)
    return items


# ---------------------------------------------------------------------------
# ВКЛАДКИ категорий выдачи
# ---------------------------------------------------------------------------

#: селекторы ссылок вкладок рубрикатора SERP (порядок приоритета)
TAB_LINK_SELECTORS = (
    'a[data-marker^="category"]',       # category[NNNN]/clickable|current|hasBack
    '[data-marker="rubricator/list"] a',
    '[data-marker="popular-rubricator/link"]',
)


def _is_search_tab_href(href: str) -> bool:
    """Ссылка - НАСТОЯЩАЯ вкладка выдачи (а не сервисная страница).

    Вкладки выдачи: /all/<slug>?cd=1&q=...&s=104 - есть поисковый
    запрос (q=) и база /all/. Сервисные ссылки рубрикатора «Авто»-
    группы (/auto-journal, /catalog/auto-ASg..., /auction/auto,
    /dogovor-kupli-prodazhi-automobilya) q= НЕ содержат - это журналы/
    каталоги/аукционы, не результаты поиска (именно
    они давали empty/block и ломали прогон).
    """
    h = (href or "").strip()
    if not h.startswith("/"):
        return False
    if "q=" not in h:
        return False
    return h.startswith("/all/") or h.startswith("/rossiya/") \
        or h in ("/all", "/rossiya")


def extract_tabs(page) -> list:
    """Вкладки категорий выдачи на загруженной странице SERP.

    Возвращает [{slug, title, current}, ...]:
      * первая запись - ТЕКУЩАЯ вкладка (слаг из URL страницы, Авито
        уже редиректнул поиск в доминантную категорию - «Ищу работу»);
      * дальше - вкладки-ссылки рубрикатора сверху выдачи; «Все
        категории» (hasBack - выход в полный рубрикатор) пропускается;
      * авто-вкладки НЕ выкидываются - фильтрует вызывающий
        (avito_filter.is_auto_tab), чтобы модуль остался чистым
        разбором разметки.

    Отказоустойчиво: нет рубрикатора -> список из одной текущей вкладки
    (или пустой, если и слага нет - смешанная выдача).
    """
    tabs: list = []
    seen = set()

    def _add(slug: str, title: str, current: bool = False) -> None:
        s = (slug or "").strip("/")
        if not s or s in seen:
            return
        seen.add(s)
        tabs.append({"slug": s, "title": (title or "").strip(),
                     "current": current})

    # 1. текущая вкладка - из URL (после редиректа)
    try:
        cur = serp_slug(page.url or "")
    except Exception:
        cur = ""
    if cur:
        _add(cur, "", True)

    # 2. вкладки из рубрикатора SERP
    for sel in TAB_LINK_SELECTORS:
        try:
            locs = page.locator(sel)
            n = locs.count()
        except Exception:
            continue
        for i in range(n):
            try:
                a = locs.nth(i)
                marker = (a.get_attribute("data-marker") or "").strip()
                if marker.endswith("/hasBack"):
                    # «Все категории» - не вкладка выдачи, а выход в
                    # полный рубрикатор (смешанная выдача с авто-мусором)
                    continue
                href = a.get_attribute("href") or ""
                if not _is_search_tab_href(href):
                    # сервисные ссылки (журналы/каталоги/аукционы) - не вкладки
                    continue
                title = (a.inner_text() or "").strip().replace("\n", " ")
                m = re.search(r"/(?:all|rossiya|[a-z_]+)/([a-z0-9_\-]+)",
                              href or "", re.IGNORECASE)
                slug = m.group(1) if m else ""
                if not slug:
                    slug = serp_slug(href)
                if slug:
                    _add(slug, title)
            except Exception:
                continue
        if len(tabs) > 1:
            break  # рубрикатор найден и разобран - остальных селекторов не надо

    return tabs
