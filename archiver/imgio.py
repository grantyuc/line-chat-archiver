"""讀寫圖片（可處理含中文的路徑；cv2.imread 在 Windows 讀不到非 ASCII 路徑）。"""
import os

import cv2
import numpy as np


def imread(path, flags=cv2.IMREAD_COLOR):
    try:
        data = np.fromfile(path, np.uint8)
    except OSError:
        return None
    return cv2.imdecode(data, flags) if data.size else None


def imwrite(path, img, ext=None):
    ok, buf = cv2.imencode(ext or os.path.splitext(path)[1] or ".png", img)
    if ok:
        buf.tofile(path)
    return ok


def sniff(path):
    """依檔頭判斷格式：jpg/png/apng/webp/gif/mp4/aac；不認得時回傳空字串。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return ""
    if head.startswith(b"\x89PNG"):
        return "apng" if b"acTL" in head else "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:3] == b"GIF":
        return "gif"
    if head[4:8] == b"ftyp":
        return "mp4"
    if len(head) > 1 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0:
        return "aac"
    return ""
