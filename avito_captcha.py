#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avito_captcha.py — модуль капчи: солвер GeeTest v4 «слайдер».

Метод (проверен на живых капчах Авито):
    * bg 300x200 + slice 80x80 RGBA — CSS background-images на
      .geetest_bg / .geetest_slice_bg, картинки на static.geetest.com;
    * карты краёв Собеля, у слайса края зануляются вне маски alpha>40;
    * masked NCC: скользим окном слайса по фону, ищем резкий
      унимодальный пик (score ~0.5-0.58) -> x щели;
    * драг человечный: cubic ease-out, 25-35 шагов, джиттер,
      перелёт 4-12 px и коррекция назад — слайдер Авито ест только
      такие траектории.

Self-test:
    python3 avito_captcha.py <bg.png> <slice.png>   # живые скрины
    python3 avito_captcha.py                        # синтетика
"""

from __future__ import annotations

import base64
import io
import math
import random
import time
import urllib.request

import numpy as np
from PIL import Image

from avito_browser import UA
from avito_classify import classify
from avito_common import log, shot, human_pause

#: Selectors for the GeeTest slider handle (in priority order).
GT_HANDLE_SELECTORS = [".geetest_btn", ".geetest_slider_button",
                       "div[class*='geetest_btn']"]

#: Selectors for the GeeTest "refresh captcha" button.
GT_REFRESH_SELECTORS = [".geetest_refresh_1", ".geetest_refresh",
                         "button[class*='geetest_refresh']",
                         "a[class*='refresh']"]

#: число реально решённых капч за жизнь процесса (для статистики)
_CAPTCHA_SOLVES = []


def captcha_solves() -> int:
    """Сколько капч реально решено в этом процессе (все вызовы)."""
    return len(_CAPTCHA_SOLVES)


#: JS: collect bg/slice image URLs and element rects of the GeeTest widget.
_GT_INFO_JS = r"""
() => {
    const out = {bgUrl: null, sliceUrl: null, bgRect: null,
                 sliceRect: null, sliceStyleTop: null};
    const urlOf = (el) => {
        if (!el) return null;
        let bi = '';
        try { bi = window.getComputedStyle(el).backgroundImage || ''; } catch (e) {}
        if (!bi || bi === 'none') {
            const st = el.getAttribute ? el.getAttribute('style') : null;
            if (st) {
                const m2 = st.match(/url\(["']?([^"')]+)["']?\)/i);
                if (m2) return m2[1];
            }
            return null;
        }
        const m = bi.match(/url\(["']?([^"')]+)["']?\)/i);
        return m ? m[1] : null;
    };
    const rectOf = (el) => {
        if (!el) return null;
        try {
            const b = el.getBoundingClientRect();
            return {x: b.left, y: b.top, w: b.width, h: b.height};
        } catch (e) { return null; }
    };
    const bgEl = document.querySelector('.geetest_bg');
    const sliceBgEl = document.querySelector('.geetest_slice_bg');
    const sliceEl = document.querySelector('.geetest_slice');
    out.bgUrl = urlOf(bgEl);
    out.sliceUrl = urlOf(sliceBgEl);
    out.bgRect = rectOf(bgEl);
    out.sliceRect = rectOf(sliceEl);
    if (sliceEl && sliceEl.style) out.sliceStyleTop = sliceEl.style.top || null;
    if (!out.bgUrl || !out.sliceUrl) {
        const cands = [];
        document.querySelectorAll('div').forEach((el) => {
            const u = urlOf(el);
            if (u && /static\.geetest\.com|gee4|geetest/i.test(u)) {
                cands.push({u: u, cls: String(el.className || '')});
            }
        });
        for (const c of cands) {
            if (/slice/i.test(c.cls)) {
                if (!out.sliceUrl) out.sliceUrl = c.u;
            } else {
                if (!out.bgUrl) out.bgUrl = c.u;
            }
        }
    }
    return out;
}
"""

#: JS: fetch an image inside the page context and return it as a data URL.
_GT_FETCH_JS = r"""
async (url) => {
    const r = await fetch(url, {credentials: 'include'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const b = await r.blob();
    return await new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(fr.result);
        fr.onerror = () => rej(fr.error);
        fr.readAsDataURL(b);
    });
}
"""


def _sobel(gray: "np.ndarray") -> "np.ndarray":
    """Sobel edge magnitude of a 2-D float array.

    Kernels: Kx = [[-1,0,1],[-2,0,2],[-1,0,1]], Ky = Kx.T; borders are
    padded by edge replication. Returns the unsigned gradient magnitude.
    """
    kx = np.array([[-1.0, 0.0, 1.0],
                   [-2.0, 0.0, 2.0],
                   [-1.0, 0.0, 1.0]])
    ky = kx.T
    h, w = gray.shape
    p = np.pad(gray, ((1, 1), (1, 1)), mode="edge")
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    for i in range(3):
        for j in range(3):
            gx += kx[i, j] * p[i:i + h, j:j + w]
            gy += ky[i, j] * p[i:i + h, j:j + w]
    return np.hypot(gx, gy)


def solve_gap_x(bg_img: Image.Image, slice_img: Image.Image,
                slice_top: int | None = None, top_n: int = 0):
    """Locate the GeeTest slider gap via masked normalized cross-correlation.

    Method (validated previously: sharp unimodal peak, score ~0.5-0.58):
      1. Sobel edge maps of the bg (300x200) and of the slice (80x80 RGBA);
         slice edges are zeroed outside the alpha>40 mask so only the puzzle
         piece contributes.
      2. Slide the full-size slice window over the bg edge map; for every
         (x, y) compute NCC = sum(bg_win*slice_win) /
         sqrt(sum(bg_win^2 over mask) * sum(slice_win^2)).
      3. y range: slice_top +- 12 px when the piece y is known from the DOM,
         otherwise a coarse scan 0..bg_h-slice_h with step 4.
      4. The best x is refined sub-pixel with a parabola over its two
         x-neighbours and rounded back to integer pixels.

    Args:
        bg_img:    background PIL image (300x200).
        slice_img: slice PIL image (80x80, RGBA with the piece opaque).
        slice_top: y of the piece inside the bg (from .geetest_slice style
                   top / DOM rect), or None for a full vertical scan.
        top_n:     when > 0, log the top-N (score, x, y) candidates - used
                   to verify the peak is sharp and unimodal.

    Returns:
        (best_x, score, best_y); best_x is the bg x where the *slice image
        left edge* must align so that the piece fills the gap. The drag
        distance therefore is best_x minus the current slice x (see
        solve_geetest).
    """
    bg_gray = np.asarray(bg_img.convert("L"), dtype=np.float64)
    H, W = bg_gray.shape

    has_alpha = ("A" in slice_img.getbands()
                 or (slice_img.mode == "P" and "transparency" in slice_img.info))
    if has_alpha:
        sl = slice_img.convert("RGBA")
        alpha = np.asarray(sl.split()[3], dtype=np.uint8)
        mask = alpha > 40
        sl_gray = np.asarray(sl.convert("L"), dtype=np.float64)
    else:  # no alpha: use the whole square as the mask
        sl_gray = np.asarray(slice_img.convert("L"), dtype=np.float64)
        mask = np.ones(sl_gray.shape, dtype=bool)

    h, w = sl_gray.shape
    if h > H or w > W or h < 3 or w < 3:
        return (0, 0.0, 0)

    sl_edges = _sobel(sl_gray)
    sl_edges = np.where(mask, sl_edges, 0.0)
    bg_edges = _sobel(bg_gray)
    mask_f = mask.astype(np.float64)

    sl_flat = sl_edges.ravel()
    mask_flat = mask_f.ravel()
    sl_sum2 = float(np.dot(sl_flat, sl_flat))
    if sl_sum2 <= 1e-12:
        return (0, 0.0, 0)

    if slice_top is not None:
        y0 = max(0, min(int(slice_top), H - h))
        y_lo = max(0, y0 - 12)
        y_hi = min(H - h, y0 + 12)
        y_step = 1
    else:
        y_lo, y_hi, y_step = 0, H - h, 4
    if y_hi < y_lo:
        y_hi = y_lo

    def ncc(x: int, y: int) -> float:
        win = bg_edges[y:y + h, x:x + w]
        num = float(np.dot(win.ravel(), sl_flat))
        den = math.sqrt(float(np.dot((win * win).ravel(), mask_flat)) * sl_sum2)
        return num / den if den > 1e-12 else 0.0

    best_score, best_x, best_y = -2.0, 0, 0
    cands = []
    for y in range(y_lo, y_hi + 1, y_step):
        for x in range(0, W - w + 1):
            s = ncc(x, y)
            if top_n:
                cands.append((s, x, y))
            if s > best_score:
                best_score, best_x, best_y = s, x, y

    # sub-pixel refinement: parabola through (x-1, x, x+1) scores
    if 0 < best_x < W - w:
        try:
            s_m = ncc(best_x - 1, best_y)
            s_p = ncc(best_x + 1, best_y)
            denom = s_m - 2.0 * best_score + s_p
            if abs(denom) > 1e-12:
                shift = 0.5 * (s_m - s_p) / denom
                if abs(shift) <= 1.0:
                    best_x += shift
        except Exception:
            pass

    if top_n:
        cands.sort(key=lambda t: t[0], reverse=True)
        pretty = ", ".join("x=%d y=%d s=%.4f" % (x, y, s)
                           for s, x, y in cands[:top_n])
        log("solve_gap_x top-%d candidates: %s" % (top_n, pretty))

    return (int(round(best_x)), float(best_score), int(best_y))


def _fetch_image(page, url: str) -> Image.Image:
    """Download a GeeTest image and decode it with PIL.

    Primary path: fetch() inside the page context (blob -> data URL), so
    the request carries the browser's headers/cookies. Data URLs are
    decoded directly.
    """
    if not url:
        raise ValueError("empty image url")
    if url.startswith("data:"):
        b64 = url.split(",", 1)[1] if "," in url else ""
        return Image.open(io.BytesIO(base64.b64decode(b64)))
    data = page.evaluate(_GT_FETCH_JS, url)
    if not isinstance(data, str) or not data.startswith("data:"):
        raise RuntimeError("page fetch returned non-data-url: %r"
                           % (str(data)[:80],))
    b64 = data.split(",", 1)[1] if "," in data else ""
    return Image.open(io.BytesIO(base64.b64decode(b64)))


def _download_image_fallback(url: str) -> Image.Image:
    """Stdlib fallback when the in-page fetch fails (e.g. CORS)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": "https://www.avito.ru/"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return Image.open(io.BytesIO(r.read()))


def human_drag(page, start_x: float, start_y: float, distance: float) -> None:
    """Drag the mouse from (start_x, start_y) by `distance` px, human-like.

    Cubic ease-out main move in 25-35 steps with +-2 px jitter, an
    overshoot of 4-12 px, then 6 correction steps easing back to the exact
    distance. mouse.down() before and mouse.up() after the move.
    """
    n = random.randint(25, 35)
    overshoot = random.uniform(4, 12)
    target1 = distance + overshoot
    page.mouse.move(start_x, start_y)
    human_pause(0.1, 0.3)
    page.mouse.down()
    try:
        for i in range(1, n + 1):
            t = i / n
            ease = 1.0 - (1.0 - t) ** 3  # cubic ease-out
            x = start_x + target1 * ease + random.uniform(-2, 2)
            y = start_y + random.uniform(-2, 2)
            page.mouse.move(x, y, steps=1)
            time.sleep(random.uniform(0.01, 0.04))
        for j in range(1, 7):  # correction: ease back from overshoot
            t = j / 6.0
            x = start_x + target1 + (distance - target1) * t \
                + random.uniform(-1, 1)
            y = start_y + random.uniform(-1, 1)
            page.mouse.move(x, y, steps=1)
            time.sleep(random.uniform(0.02, 0.05))
    finally:
        page.mouse.up()


def _gt_widget_gone(page) -> bool:
    """True when the GeeTest widget is gone from the DOM or invisible."""
    try:
        if page.locator(".geetest_box, div[class*='geetest_btn']").count() == 0:
            return True
    except Exception:
        return True
    try:
        if not page.locator("div[class*='geetest_btn']").first.is_visible():
            return True
    except Exception:
        pass
    return False


def solve_geetest(page, max_tries: int = 3, shots_prefix: str = "gt") -> bool:
    """Solve the GeeTest v4 slider captcha on the current page.

    For each attempt:
      1. wait for the slider handle (.geetest_btn and friends);
      2. read bg/slice image URLs + element rects from the widget DOM;
      3. fetch both images inside the page context (data URLs) and decode;
      4. solve_gap_x() -> gap x (+ score, y);
      5. drag the handle human-like over the computed distance;
      6. success = widget gone or items visible; otherwise click the
         refresh button and retry.

    Screenshots of every attempt are stored in SHOTS_DIR.
    Returns True when the captcha was solved.
    """
    for attempt in range(1, max_tries + 1):
        log("geetest: attempt %d/%d" % (attempt, max_tries))
        try:
            handle = None
            for sel in GT_HANDLE_SELECTORS:
                try:
                    page.wait_for_selector(sel, timeout=5000)
                    handle = page.locator(sel).first
                    break
                except Exception:
                    continue
            if handle is None:
                # nothing to solve (or a false-positive captcha classify)
                if classify(page) != "captcha":
                    log("geetest: no handle and no captcha -> nothing to do")
                    return True
                log("geetest: slider handle not found")
                return False

            # image urls + rects (retry briefly while the widget loads)
            info = {}
            for _ in range(3):
                info = page.evaluate(_GT_INFO_JS) or {}
                if info.get("bgUrl") and info.get("sliceUrl"):
                    break
                time.sleep(1.0)
            bg_url, slice_url = info.get("bgUrl"), info.get("sliceUrl")
            if not bg_url or not slice_url:
                log("geetest: could not read bg/slice image urls")
                shot(page, "%s_noimg%d" % (shots_prefix, attempt))
                return False

            try:
                bg_img = _fetch_image(page, bg_url)
                slice_img = _fetch_image(page, slice_url)
            except Exception as e:
                log("geetest: in-page fetch failed (%s), urllib fallback"
                    % e)
                bg_img = _download_image_fallback(bg_url)
                slice_img = _download_image_fallback(slice_url)
            log("geetest: bg %s/%s, slice %s/%s"
                % (bg_img.size, bg_img.mode, slice_img.size, slice_img.mode))
            if bg_img.size[0] < slice_img.size[0]:
                log("geetest: bg smaller than slice - swapped urls?")
                bg_img, slice_img = slice_img, bg_img

            bg_rect = info.get("bgRect") or {}
            slice_rect = info.get("sliceRect") or {}
            scale = 1.0
            if bg_rect.get("w") and bg_img.width:
                scale = float(bg_rect["w"]) / float(bg_img.width)

            # vertical hint: piece top relative to the bg element
            slice_top = None
            if slice_rect.get("y") is not None and bg_rect.get("y") is not None:
                slice_top = int((slice_rect["y"] - bg_rect["y"]) / (scale or 1.0))

            gap_x, score, gap_y = solve_gap_x(bg_img, slice_img,
                                              slice_top=slice_top)
            log("geetest: gap x=%d y=%d score=%.3f" % (gap_x, gap_y, score))
            if score < 0.25:
                log("geetest: weak match, refreshing captcha")
                shot(page, "%s_weak%d" % (shots_prefix, attempt))
                _gt_click_refresh(page)
                time.sleep(2.0)
                continue

            handle_box = handle.bounding_box()
            if not handle_box:
                log("geetest: no handle bounding box")
                shot(page, "%s_nohbox%d" % (shots_prefix, attempt))
                _gt_click_refresh(page)
                time.sleep(2.0)
                continue
            start_x = handle_box["x"] + handle_box["width"] / 2.0
            start_y = handle_box["y"] + handle_box["height"] / 2.0

            # Drag distance: the gap position in viewport coords minus the
            # current slice position. gap viewport x = bg_rect.x +
            # gap_x*scale; when the slice rect is unknown assume the piece
            # starts at the left edge of the bg area.
            gap_vp_x = float(bg_rect.get("x", 0.0)) + gap_x * scale
            if slice_rect.get("x") is not None:
                cur_vp_x = float(slice_rect["x"])
            else:
                cur_vp_x = float(bg_rect.get("x", 0.0))
            distance = gap_vp_x - cur_vp_x
            max_dist = float(bg_rect.get("w") or bg_img.width * scale)
            distance = max(5.0, min(distance, max_dist))
            log("geetest: drag distance=%.1f px (scale=%.3f)"
                % (distance, scale))

            human_pause(0.2, 0.6)
            human_drag(page, start_x, start_y, distance)

            time.sleep(2.5)
            if _gt_widget_gone(page) or classify(page) == "items":
                log("geetest: SOLVED")
                _CAPTCHA_SOLVES.append(1)
                shot(page, "%s_ok%d" % (shots_prefix, attempt))
                return True
            log("geetest: not solved, refreshing")
            shot(page, "%s_fail%d" % (shots_prefix, attempt))
            _gt_click_refresh(page)
            time.sleep(2.0)
        except Exception as e:
            log("geetest: attempt error: %s" % e)
            shot(page, "%s_err%d" % (shots_prefix, attempt))
    return False


def _gt_click_refresh(page) -> None:
    """Click the GeeTest refresh button (best effort, never raises)."""
    for sel in GT_REFRESH_SELECTORS:
        try:
            page.locator(sel).first.click(timeout=2000)
            return
        except Exception:
            continue


# ============================================================================
# Self-test (прежний контракт avito_lib.py)
# ============================================================================

def _self_test() -> int:
    import sys
    if len(sys.argv) >= 3:
        # real GeeTest captures passed as arguments
        bg = Image.open(sys.argv[1])
        sl = Image.open(sys.argv[2])
        print("bg=%s/%s slice=%s/%s" % (bg.size, bg.mode, sl.size, sl.mode))
        res = solve_gap_x(bg, sl, None, top_n=3)
        print("solve_gap_x -> (x, score, y) = %r" % (res,))
        return 0 if res[1] >= 0.3 else 1

    # synthetic smoke test: cut a piece out of a noisy gradient, darken the
    # hole, embed the piece into an 80x80 RGBA canvas and try to find it.
    rng = np.random.default_rng(42)
    W, H, S = 300, 200, 80
    gx, gy, pw, ph, ox, oy = 150, 60, 60, 60, 10, 12
    tex = np.cumsum(np.cumsum(rng.random((H, W)), axis=0), axis=1)
    tex = (tex / tex.max() * 255.0).astype(np.uint8)
    piece = tex[gy:gy + ph, gx:gx + pw]
    bg = tex.copy()
    bg[gy:gy + ph, gx:gx + pw] = (piece.astype(np.int32) * 35 // 100
                                  ).astype(np.uint8)
    sl_arr = np.zeros((S, S, 4), dtype=np.uint8)
    sl_arr[oy:oy + ph, ox:ox + pw, 0] = piece
    sl_arr[oy:oy + ph, ox:ox + pw, 1] = piece
    sl_arr[oy:oy + ph, ox:ox + pw, 2] = piece
    sl_arr[oy:oy + ph, ox:ox + pw, 3] = 255
    x, score, y = solve_gap_x(Image.fromarray(bg),
                              Image.fromarray(sl_arr), None)
    exp_x, exp_y = gx - ox, gy - oy
    ok = abs(x - exp_x) <= 2 and score >= 0.25
    print("synthetic self-test: expected x=%d y=%d, got x=%d y=%d "
          "score=%.3f -> %s" % (exp_x, exp_y, x, y, score,
                                "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
