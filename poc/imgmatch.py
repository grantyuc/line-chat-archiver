"""用 ORB 特徵點判斷兩張圖是不是同一張照片。

可以容忍縮放、裁切和重新壓縮，所以縮圖、壓縮版、原檔之間都能互相比對。
"""
import cv2
import numpy as np

from common import imread

_orb = cv2.ORB_create(3000)
_matcher = cv2.BFMatcher(cv2.NORM_HAMMING)


def features(img, max_side=1000):
    """回傳 (關鍵點, 描述子)。img 可以是路徑或灰階陣列；讀不到或沒有特徵時回傳 None。"""
    if isinstance(img, str):
        img = imread(img, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    scale = max_side / max(img.shape[:2])
    if scale < 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    kp, des = _orb.detectAndCompute(img, None)
    if des is None or len(kp) < 2:
        return None
    return kp, des


def score(fa, fb):
    """回傳 (幾何一致點數, 初步配對數)。幾何一致點 ≥ 25 通常就是同一張。"""
    if fa is None or fb is None:
        return 0, 0
    (k1, d1), (k2, d2) = fa, fb
    pairs = _matcher.knnMatch(d1, d2, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < 8:
        return 0, len(good)
    src = np.float32([k1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([k2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    return (int(mask.sum()) if mask is not None else 0), len(good)
