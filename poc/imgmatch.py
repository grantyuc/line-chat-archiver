"""判斷兩張圖是不是同一張照片。

分兩關：
1. ORB 特徵點＋幾何一致性（可容忍縮放、裁切、重新壓縮），快速找出候選。
2. 依找到的幾何關係把一張圖疊到另一張上，切成小區塊逐塊比對像素。
   兩張同一個 App 的截圖共用狀態列、按鈕等介面元素，第一關會誤判，
   整張圖的相關係數也偏高；但內容區的區塊對不上，逐塊比對可以分辨。
"""
import cv2
import numpy as np

from common import imread

MAX_SIDE = 1000
SAME_PHOTO = 0.85  # 內容一致的區塊比例達此值視為同一張

_orb = cv2.ORB_create(3000)
_matcher = cv2.BFMatcher(cv2.NORM_HAMMING)


def load(img, max_side=MAX_SIDE):
    """讀成灰階並縮到長邊不超過 max_side。img 可以是路徑或陣列。"""
    if isinstance(img, str):
        img = imread(img, cv2.IMREAD_GRAYSCALE)
    elif img is not None and img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if img is None:
        return None
    scale = max_side / max(img.shape[:2])
    if scale < 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return img


def features(img):
    """回傳 (關鍵點, 描述子)；讀不到或沒有特徵時回傳 None。"""
    img = load(img)
    if img is None:
        return None
    kp, des = _orb.detectAndCompute(img, None)
    if des is None or len(kp) < 2:
        return None
    return kp, des


def score(fa, fb):
    """回傳 (幾何一致點數, 初步配對數, 單應矩陣或 None)。矩陣把 a 的座標對應到 b。"""
    if fa is None or fb is None:
        return 0, 0, None
    (k1, d1), (k2, d2) = fa, fb
    pairs = _matcher.knnMatch(d1, d2, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < 8:
        return 0, len(good), None
    src = np.float32([k1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([k2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    h, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    if mask is None:
        return 0, len(good), None
    return int(mask.sum()), len(good), h


def similarity(a, b, h, grid=8):
    """把 a 依單應矩陣 h 疊到 b 上，回傳「內容一致的區塊比例」（0～1）。

    重疊區切成 grid×grid 塊，只看有紋理的塊，計算每塊的正規化相關係數；
    相關係數 ≥ 0.7 的塊算一致。同一張照片幾乎每塊都一致；兩張只有介面相同
    的截圖，內容區的塊會不一致。
    """
    a, b = load(a), load(b)
    if a is None or b is None or h is None:
        return 0.0
    size = (b.shape[1], b.shape[0])
    warped = cv2.warpPerspective(a, h, size)
    mask = cv2.warpPerspective(np.full(a.shape, 255, np.uint8), h, size)
    mask = cv2.erode(mask, np.ones((5, 5), np.uint8)) > 0
    ys, xs = np.nonzero(mask)
    if len(ys) < 0.02 * mask.size:
        return 0.0
    # 先模糊，抵消縮圖放大後的模糊與 JPEG 雜訊
    warped = cv2.GaussianBlur(warped, (5, 5), 0).astype(np.float32)
    target = cv2.GaussianBlur(b, (5, 5), 0).astype(np.float32)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    agree = total = 0
    for i in range(grid):
        for j in range(grid):
            sy = slice(y0 + (y1 - y0) * i // grid, y0 + (y1 - y0) * (i + 1) // grid)
            sx = slice(x0 + (x1 - x0) * j // grid, x0 + (x1 - x0) * (j + 1) // grid)
            m = mask[sy, sx]
            if m.size == 0 or m.mean() < 0.9:
                continue
            x, y = warped[sy, sx][m], target[sy, sx][m]
            if y.std() < 8:          # 幾乎沒有紋理的塊（純色背景）不計
                continue
            x, y = x - x.mean(), y - y.mean()
            denom = float(np.sqrt((x * x).sum() * (y * y).sum()))
            total += 1
            agree += denom > 0 and float((x * y).sum()) / denom >= 0.7
    return agree / total if total >= 4 else 0.0
