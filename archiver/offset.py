"""估計兩張聊天截圖之間畫面捲動了多少（實機測試 5 驗證過的方法）。"""
import time

import cv2

LOW_SCORE = 0.8


def _locate(src, dst, y0, band):
    tpl = src[y0:y0 + band]
    if tpl.std() < 2:          # 幾乎是空白背景，無法定位
        return None, 0.0
    res = cv2.matchTemplate(dst, tpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    return loc[1] - y0, float(score)


def find_offset(a, b, roi):
    """估計聊天區從 a 到 b 往下移了幾像素。回傳 (dy, 分數)；dy=None 表示無法判定。

    roi = (上緣, 下緣, 頂部緩衝)。正向用 a 上半部的水平帶去 b 找，反向用 b 下半部的帶回 a 找，
    取分數最高者；為了速度在一半解析度上計算。
    """
    top, bottom, buffer = roi
    ga = cv2.resize(cv2.cvtColor(a[top:bottom], cv2.COLOR_BGR2GRAY), None, fx=0.5, fy=0.5,
                    interpolation=cv2.INTER_AREA)
    gb = cv2.resize(cv2.cvtColor(b[top:bottom], cv2.COLOR_BGR2GRAY), None, fx=0.5, fy=0.5,
                    interpolation=cv2.INTER_AREA)
    h = ga.shape[0]
    band = max(20, h // 6)
    step = max(1, band // 2)
    best = (None, 0.0)
    for y0 in range(buffer // 2, h // 2 - band + 1, step):
        dy, score = _locate(ga, gb, y0, band)
        if score > best[1]:
            best = (dy, score)
    for y0 in range(h - band, h // 2 - 1, -step):
        dy, score = _locate(gb, ga, y0, band)
        if score > best[1]:
            best = (None if dy is None else -dy, score)
    dy, score = best
    return (None if dy is None else dy * 2), score


def is_still(a, b, roi):
    dy, score = find_offset(a, b, roi)
    if dy == 0 and score >= 0.9:
        return True
    top, bottom, _ = roi
    return float(cv2.absdiff(a[top:bottom], b[top:bottom]).mean()) < 0.5


def wait_still(device, roi, timeout=8.0):
    """連續截圖直到相鄰兩張沒有位移。回傳 (PNG 位元組, 影像)。逾時則回傳最後一張。"""
    t0 = time.time()
    png, prev = device.screencap()
    while time.time() - t0 < timeout:
        png, cur = device.screencap()
        if is_still(prev, cur, roi):
            return png, cur
        prev = cur
    return png, prev

