"""估計兩張聊天截圖之間畫面捲動了多少（實機測試 5 驗證過的方法）。"""
import time

import cv2

LOW_SCORE = 0.8


def _locate(src, dst, y0, band, lo=None, hi=None):
    """src 中從 y0 開始的水平帶，在 dst 中的位置。只接受位移在 [lo, hi] 之間（None 表示不限）。"""
    tpl = src[y0:y0 + band]
    if tpl.std() < 2:          # 幾乎是空白背景，無法定位
        return None, 0.0
    first = 0 if lo is None else max(0, y0 + lo)
    last = dst.shape[0] - band if hi is None else min(dst.shape[0] - band, y0 + hi)
    if last < first:
        return None, 0.0
    res = cv2.matchTemplate(dst[first:last + band], tpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    return first + loc[1] - y0, float(score)


GOOD_DIFF = 4.0      # 重疊區平均灰階差在此以下，視為位置正確（只剩 JPEG 雜訊）


def find_offset(a, b, roi, expect=None):
    """估計聊天區從 a 到 b 往下移了幾像素。回傳 (dy, 分數)；dy=None 表示無法判定。

    roi = (上緣, 下緣, 頂部緩衝)。先用多條水平帶各自找出候選位移，再比較每個候選（含「沒有動」）
    整個重疊區的差異，取差異最小的；只比水平帶可能被有規律的背景（桌布花紋）騙到。
    expect 為預期位移：先只在預期值附近找（捲得遠、重疊區小時全範圍的搜尋可能找不到），
    找到的位置重疊區幾乎一致就直接採用，否則再加上全範圍的候選。
    """
    top, bottom, buffer = roi
    ga = cv2.resize(cv2.cvtColor(a[top:bottom], cv2.COLOR_BGR2GRAY), None, fx=0.5, fy=0.5,
                    interpolation=cv2.INTER_AREA)
    gb = cv2.resize(cv2.cvtColor(b[top:bottom], cv2.COLOR_BGR2GRAY), None, fx=0.5, fy=0.5,
                    interpolation=cv2.INTER_AREA)
    skip = buffer // 2
    if _overlap_diff(ga, gb, 0, skip) < 1.0:                 # 兩張幾乎一樣：沒有動
        return 0, 1.0
    scores = {}                                             # 候選位移 -> 最高分數

    def add(candidates):
        for dy, score in candidates:
            if score >= LOW_SCORE and score > scores.get(dy, 0):
                scores[dy] = score
        if not scores:
            return None
        return min((_overlap_diff(ga, gb, dy, skip), -score, dy) for dy, score in scores.items())

    best = None
    if expect is not None:
        tol = max(60, abs(expect) // 4)
        best = add(_candidates(ga, gb, skip, (expect - tol) // 2, (expect + tol) // 2))
    if best is None or best[0] > GOOD_DIFF:
        best = add(_candidates(ga, gb, skip))
    if best is None:
        return None, 0.0
    _, neg_score, dy = best
    return dy * 2, -neg_score


def _overlap_diff(ga, gb, dy, skip):
    """a 往下移 dy 後和 b 的重疊區（避開頂端 skip 像素）的平均灰階差；重疊太小時為無限大。"""
    h = ga.shape[0]
    if dy >= 0:
        pa, pb = ga[skip:h - dy], gb[skip + dy:h]
    else:
        pa, pb = ga[skip - dy:h], gb[skip:h + dy]
    if pa.shape[0] < h // 10:
        return float("inf")
    return float(cv2.absdiff(pa, pb).mean())


def _candidates(ga, gb, skip, lo=None, hi=None):
    """每條水平帶找到的 (位移, 分數)（半解析度）。正向用 a 的帶去 b 找，反向用 b 的帶回 a 找。

    不限範圍時，正向只用 a 的上半部、反向只用 b 的下半部（往上捲時這兩塊一定還在畫面上）；
    限定範圍時，用所有可能落在重疊區的帶。頂端 skip 像素內有浮動日期條，不用。
    """
    h = ga.shape[0]
    band = max(20, h // 6)
    step = max(1, band // 2)
    found = []
    for y0 in range(skip, (h // 2 if lo is None else h) - band + 1, step):
        dy, score = _locate(ga, gb, y0, band, lo, hi)
        if dy is not None:
            found.append((dy, score))
    for y0 in range(h - band, (h // 2 if lo is None else skip) - 1, -step):
        dy, score = _locate(gb, ga, y0, band, None if hi is None else -hi, None if lo is None else -lo)
        if dy is not None:
            found.append((-dy, score))
    return found


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

