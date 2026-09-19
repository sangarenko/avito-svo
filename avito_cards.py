#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avito_cards.py — модуль карточек объявлений: продавец + телефон.

process_card() открывает карточку, собирает продавца (имя/тип/счётчики —
для «Вакансий компании») и, если want_phone, жмёт «Позвонить» и читает
номер (текстом или OCR с картинки-попапа). Звонки никогда не
совершаются: внутри попапа ничего не кликается.

Анонимный Авито номера не отдаёт -> status='login_required' — это
ОЖИДАЕМОЕ чистое состояние, не ошибка (номера появятся после /войти).
"""

from __future__ import annotations

import base64
import io
import os
import re
import time

from avito_browser import save_session
from avito_captcha import solve_geetest
from avito_classify import classify, click_block_continue, \
    detect_login_state
from avito_common import log, shot, human_pause

#: Selectors for the «Показать телефон» button on card pages (in priority
#: order, first visible match wins).
PHONE_BUTTON_SELECTORS = [
    "button[data-marker='item-phone-button/card']",
    "button[data-marker='item-phone-button']",
    "button[data-marker^='item-phone-button']",
    "button[data-marker*='phone-button']",
    "a[data-marker='item-phone-button/card']",
    "button:has-text('Показать телефон')",
    "button:has-text('Позвонить')",
    "button:has-text('Показать')",
]

#: Substrings that indicate an auth wall / login prompt in dialogs.
AUTH_TEXTS = [
    "Войдите", "войдите", "войти", "Войти",
    "Авториз", "авториз",
    "Зарегистр", "зарегистр",
    "чтобы позвонить", "Чтобы позвонить",
    "Log in", "log in",
]

#: Phone number pattern: +7 / 7 / 8 + 10 digits with loose separators.
PHONE_RE = re.compile(
    r"(?:\+?\s?7|8)[\s\-()]?\d{3}[\s\-()]?\d{3}[\s\-()]?\d{2}[\s\-()]?\d{2}")

#: селектор картинки с номером в попапе
PHONE_IMG_SELECTOR = "img[data-marker='phone-popup/phone-image']"

#: максимум телефонов за один сбор (сессию) — env юнита (20 на сервере)
MAX_PHONES_PER_SESSION = int(
    os.environ.get("AVITO_MAX_PHONES", "5") or 5)

try:
    import shutil as _shutil
    _TESSERACT = _shutil.which("tesseract")
except Exception:
    _TESSERACT = None


def _norm_phone(s: str):
    """Normalize a matched phone string to +7XXXXXXXXXX (or None)."""
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return None


# ---- OCR: Авито отдаёт номер КАРТИНКОЙ в попапе «Позвонить» -------------
#
# В попапе после клика «Позвонить» номер приходит как
# <img data-marker="phone-popup/phone-image" src="data:image/png;base64,...">
# — текстом его не скопировать. Читаем tesseract-ом (проверено на живом
# попапе: scale=3, thresh=150 распознаёт номер безошибочно).


def _ocr_prepare(png_bytes: bytes, scale: int = 3, thresh: int = 150):
    """RGBA-картинка Авито (прозрачный фон) -> белый фон -> ч/б ->
    апскейл -> бинаризация. Возвращает PIL-изображение или None."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(png_bytes))
        if im.mode in ("RGBA", "LA", "PA"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            bg.alpha_composite(im)
            im = bg.convert("L")
        else:
            im = im.convert("L")
        if scale > 1:
            im = im.resize((im.width * scale, im.height * scale),
                           Image.LANCZOS)
        return im.point(lambda p: 255 if p > thresh else 0)
    except Exception:
        return None


def ocr_phone_image(png_bytes: bytes):
    """Распознать номер с PNG-картинки попапа «Позвонить» (tesseract).

    Прогоняет комбинации (scale, thresh), возвращает первый валидный
    +7XXXXXXXXXX или None. Проверено на реальном номере
    «8 958 603-97-02»: (3,150) читает верно, остальные — запасные.
    """
    if not _TESSERACT or not png_bytes:
        return None
    import subprocess
    import tempfile
    for scale, thresh in ((3, 150), (3, 120), (3, 180), (4, 180), (5, 150)):
        im = _ocr_prepare(png_bytes, scale=scale, thresh=thresh)
        if im is None:
            continue
        tmp = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".png",
                                             delete=False) as tf:
                im.save(tf, format="PNG")
                tmp = tf.name
            r = subprocess.run(
                [_TESSERACT, tmp, "stdout", "--psm", "7",
                 "-c", "tessedit_char_whitelist=0123456789 -()"],
                capture_output=True, timeout=30)
            raw = r.stdout.decode("utf-8", "replace").strip()
            if raw:
                phone = _norm_phone(raw)
                if phone:
                    log("phone: OCR %s (scale=%d thresh=%d raw=%r)"
                        % (phone, scale, thresh, raw))
                    return phone
        except Exception:
            continue
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return None


def get_phone(page, verbose: bool = False):
    """Click «Показать телефон» on a card page and read the popup.

    Returns (status, phone) with status in:
      'ok'             - phone extracted (text in the popup OR OCR of the
                         popup image — Avito renders numbers as PNG) and
                         normalized to +7XXXXXXXXXX. No call is ever
                         placed: nothing inside the popup is clicked.
      'login_required' - auth wall / login redirect or silent no-op while
                         not logged in;
      'no_button'      - no phone button on the card (messenger-only ad);
      'error'          - button existed but nothing happened while logged
                         in (screenshot saved).
    """
    # 1. find the button
    btn = None
    for sel in PHONE_BUTTON_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=800)
            btn = page.locator(sel).first
            if verbose:
                log("phone: button found via %s" % sel)
            break
        except Exception:
            continue
    if btn is None:
        for ms in ("[data-marker='item-messenger-button']",
                   "button:has-text('Написать')"):
            try:
                page.wait_for_selector(ms, timeout=800)
                log("phone: no phone button, messenger-only ad")
                return ("no_button", None)
            except Exception:
                continue
        log("phone: no phone button found")
        return ("no_button", None)

    # 2. login state BEFORE the click (cheap cookie/marker probe)
    logged = detect_login_state(page)
    if verbose:
        log("phone: logged_in=%s" % logged)

    # 3. click
    try:
        btn.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    human_pause(0.3, 0.8)
    try:
        btn.click(timeout=5000)
    except Exception:
        try:
            btn.evaluate("el => el.click()")
        except Exception as e:
            log("phone: click failed: %s" % e)
            shot(page, "phone_click_fail")
            return ("error", None)

    # 4. poll for phone popup / auth wall / login redirect
    deadline = time.time() + 6.0
    while time.time() < deadline:
        # 4a. номер КАРТИНКОЙ: img[data-marker='phone-popup/phone-image']
        #     (data:image/png;base64). Внутри попапа ничего не кликаем —
        #     звонок не инициируется, только читаем картинку и закрываем.
        try:
            loc = page.locator(PHONE_IMG_SELECTOR)
            if loc.count():
                src = loc.first.get_attribute("src") or ""
                png = None
                if src.startswith("data:image"):
                    png = base64.b64decode(src.split(",", 1)[-1])
                elif src.startswith("http"):
                    png = loc.first.screenshot()
                if png:
                    phone = ocr_phone_image(png)
                    if phone:
                        log("phone: %s [OCR с картинки]" % phone)
                        return ("ok", phone)
        except Exception:
            pass
        blob = ""
        try:
            texts = page.locator(
                "[role='dialog'], [data-marker*='phone-popup'], .popup"
            ).all_inner_texts()
            blob = "\n".join(t for t in texts if t)
        except Exception:
            pass
        if blob:
            m = PHONE_RE.search(blob)
            if m:
                phone = _norm_phone(m.group(0))
                if phone:
                    log("phone: %s" % phone)
                    return ("ok", phone)
            if any(t in blob for t in AUTH_TEXTS):
                log("phone: auth wall detected")
                return ("login_required", None)
        try:
            if page.locator("[data-marker*='auth']").count() > 0 \
                    and page.locator("[data-marker*='auth']").first \
                    .is_visible():
                return ("login_required", None)
        except Exception:
            pass
        if "/login" in (page.url or "") or "/blocked" in (page.url or ""):
            return ("login_required", None)
        time.sleep(0.4)

    # 5. nothing appeared
    if not logged:
        log("phone: silent no-op while anonymous -> login_required")
        return ("login_required", None)
    log("phone: no popup while logged in -> error")
    shot(page, "phone_timeout")
    return ("error", None)


def extract_seller(page) -> dict:
    """Extract seller info from a card page; every field degrades to None.

    Returns dict(name, type, url, rating, reviews, avito_ads). url is
    absolutized to https://www.avito.ru/...
    """
    out = {"name": None, "type": None, "url": None,
           "rating": None, "reviews": None, "avito_ads": None}

    # name + url from the seller link (href /brands/xxx?shopId=...)
    try:
        a = page.locator("a[data-marker='seller-link/link']")
        if a.count() > 0:
            href = a.first.get_attribute("href") or ""
            if href:
                out["url"] = (href if href.startswith("http")
                              else "https://www.avito.ru" + href)
            txt = a.first.inner_text().strip()
            if txt:
                out["name"] = txt
    except Exception:
        pass
    if out["name"] is None:
        try:
            n = page.locator("[data-marker='seller-info/name']")
            if n.count() > 0:
                out["name"] = n.first.inner_text().strip() or None
        except Exception:
            pass

    # seller type: «Частное лицо» / «Компания»
    try:
        lab = page.locator("[data-marker='seller-info/label']")
        if lab.count() > 0:
            t = lab.first.inner_text().strip()
            if t:
                out["type"] = t
    except Exception:
        pass

    # seller section text for counters
    sec = ""
    for sel in ("[data-marker='seller-info']", "[data-marker='item/seller']",
                "div[class*='seller']"):
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                sec = loc.first.inner_text() or ""
                if sec:
                    break
        except Exception:
            continue
    wide = ""
    if not sec:
        try:
            wide = page.locator("body").inner_text() or ""
        except Exception:
            wide = ""

    def _count(hay: str):
        if not hay:
            return
        m = re.search(r"(\d+)\s*(?:объявлен|объявл)", hay)
        if m and out["avito_ads"] is None:
            out["avito_ads"] = int(m.group(1))
        m = re.search(r"(\d+)\s*отз", hay)
        if m and out["reviews"] is None:
            out["reviews"] = int(m.group(1))

    _count(sec)
    _count(wide)

    # rating: explicit marker first, then 'N,N' near a star in the section
    try:
        r = page.locator("[data-marker='seller-info/rating']")
        if r.count() > 0:
            m = re.search(r"\d[.,]\d", r.first.inner_text() or "")
            if m:
                out["rating"] = m.group(0).replace(",", ".")
    except Exception:
        pass
    if out["rating"] is None and sec:
        m = re.search(r"\d[.,]\d", sec)
        if m:
            out["rating"] = m.group(0).replace(",", ".")
    return out


def process_card(context, ad: dict, db, verbose: bool = False,
                 want_phone: bool = True) -> dict:
    """Scrape one ad card page: seller info + phone, and persist to DB.

    Flow: open page -> classify (block -> pause+reload, captcha ->
    solve_geetest) -> extract_seller -> get_phone -> update DB ->
    save session when a phone was obtained -> close page.

    want_phone=False: карточка открывается ТОЛЬКО ради продавца
    (имя/тип/счётчики — для счётчика вакансий компании); «Позвонить»
    не жмём, phone_status в БД НЕ пишем — объявление остаётся в
    очереди pending и телефон будет запрошен позже (когда лимит
    сессии позволит / после входа).

    Returns {id, status, phone, seller_name, seller_type, seller_url,
    seller_rating, seller_reviews, seller_avito_ads, class}.
    """
    result = {"id": ad.get("id"), "status": "error", "phone": None,
              "seller_name": None, "seller_type": None, "seller_url": None,
              "seller_rating": None, "seller_reviews": None,
              "seller_avito_ads": None, "class": "unknown"}
    page = context.new_page()
    try:
        try:
            page.goto(ad["url"], wait_until="domcontentloaded",
                      timeout=45000)
        except Exception as e:
            log("card %s: goto error %s" % (ad.get("id"), e))
        human_pause(0.8, 1.8)
        c = classify(page)
        if c in ("block", "captcha"):
            # мягкий бан: escape через «Продолжить» -> виджет -> слайдер
            # (как в smart_goto; раньше тупо ждали reload и скрепили
            # страницу блокa как карточку)
            if c == "block":
                click_block_continue(page)
            try:
                page.wait_for_selector(
                    ".geetest_btn, .geetest_box, .geetest_wrap",
                    timeout=8000)
            except Exception:
                pass
            c = classify(page)
            if c == "captcha" and solve_geetest(
                    page, shots_prefix="card_gt_%s" % ad.get("id")):
                human_pause(2, 3)
                c = classify(page)
            if c in ("block", "captcha"):
                human_pause(15, 25)
                try:
                    page.reload(wait_until="domcontentloaded",
                                timeout=45000)
                except Exception:
                    pass
                c = classify(page)
        result["class"] = c
        if c not in ("items", "empty"):
            # страница НЕ карточка (блок/капча не снялись, пустая
            # навигация): ничего не скрепим и НЕ пишем статус в БД —
            # иначе живое объявление получит фиктивный no_button и
            # выпадет из очереди навсегда. Останется pending → ретрай.
            log("card %s: класс %s — карточка не загрузилась, статус "
                "не пишу (будет ретрай)" % (ad.get("id"), c))
            return result

        seller = extract_seller(page)
        for k in ("name", "type", "url", "rating", "reviews", "avito_ads"):
            result["seller_" + k] = seller.get(k)

        phone = None
        if want_phone:
            status, phone = get_phone(page, verbose=verbose)
            result["status"] = status
            result["phone"] = phone

            # login state file is refreshed by detect_login_state() itself
            detect_login_state(page)
        else:
            # лимит телефонов исчерпан: продавца собрали, телефон
            # не запрашивали — статус в БД НЕ пишем (останется pending)
            result["status"] = "skipped_phone"

        # seller-пишем ТОЛЬКО реально собранные значения: NULL-ами
        # прежние данные не затираем (guard как у phone)
        fields = {}
        for k in ("name", "type", "url", "rating", "reviews",
                  "avito_ads"):
            v = seller.get(k)
            if v is not None:
                fields["seller_" + k] = v
        if want_phone:
            fields["phone_status"] = result["status"]
            if phone:  # never overwrite an existing phone with NULL
                fields["phone"] = phone
        if fields:
            db.update_card(ad["id"], fields)
        if phone:
            save_session(context)
    except Exception as e:
        log("card %s: error %s" % (ad.get("id"), e))
        shot(page, "card_err_%s" % ad.get("id"))
        result["status"] = "error"
    finally:
        try:
            page.close()
        except Exception:
            pass
    return result
