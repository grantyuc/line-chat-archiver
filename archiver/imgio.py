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


def is_animated(path, kind):
    """GIF、APNG 一律當成動圖；WebP 看檔頭有沒有動畫（ANIM 區塊）。"""
    if kind in ("gif", "apng"):
        return True
    if kind != "webp":
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return False
    # VP8X 區塊第一個位元組的第 2 位元是動畫旗標；後面跟著 ANIM 區塊
    return head[12:16] == b"VP8X" and bool(head[20] & 0x02) or b"ANIM" in head


MP4_BOXES = (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip", b"pnot")   # MP4／MOV 開頭可能的區塊


def head_hex(path, n=12):
    """檔頭的前幾個位元組（報告中說明認不得的檔案用）。"""
    try:
        with open(path, "rb") as f:
            return f.read(n).hex(" ")
    except OSError:
        return ""


def sniff(path):
    """依檔頭判斷格式：jpg/png/apng/webp/gif/mp4/webm/aac/amr/ogg；不認得時回傳空字串。"""
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
    if head[4:8] in MP4_BOXES:
        return "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if len(head) > 1 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0:
        return "aac"
    if head.startswith(b"#!AMR"):
        return "amr"
    if head[:4] == b"OggS":
        return "ogg"
    return ""
