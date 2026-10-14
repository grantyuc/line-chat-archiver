"""模擬手機：把一段聊天排版成很長的畫面，依目前捲動位置產生截圖和 uiautomator XML。

截圖和 XML 的結構比照實機（1080x2340、聊天區 y=278～2143、元件 id 同 LINE 26.14）。
"""
import hashlib
import re
import xml.etree.ElementTree as ET

import os

import cv2
import numpy as np

from archiver.media import CHATS, STICKERS
from tests import xmlgen

W, H = 1080, 2340
TOP, BOTTOM = 278, 2143
GAP = 22
BG = (231, 236, 240)
CHAT_ID = "u" + "ab" * 16


def _seed(*parts):
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def jpg(img, q=90):
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])[1].tobytes()


def make_sticker(seed, w=259, h=224):
    """透明背景上的幾個色塊，模擬貼圖 PNG（BGRA）。"""
    r = np.random.default_rng(seed)
    img = np.zeros((h, w, 4), np.uint8)
    for _ in range(6):
        color = tuple(int(v) for v in r.integers(0, 255, 3)) + (255,)
        cv2.ellipse(img, (int(r.integers(40, w - 40)), int(r.integers(40, h - 40))),
                    (int(r.integers(15, 60)), int(r.integers(15, 60))), int(r.integers(0, 180)), 0, 360, color, -1)
    return img


def composite(bgra, size, bg=BG):
    w, h = size
    img = cv2.resize(bgra, (w, h), interpolation=cv2.INTER_AREA).astype(np.float32)
    alpha = img[:, :, 3:] / 255
    out = img[:, :, :3] * alpha + np.array(bg, np.float32) * (1 - alpha)
    return out.astype(np.uint8)


def texture(seed, w, h):
    r = np.random.default_rng(seed)
    img = cv2.resize(r.integers(0, 255, (max(2, h // 40), max(2, w // 40), 3), dtype=np.uint8), (w, h),
                     interpolation=cv2.INTER_CUBIC)
    for _ in range(12):
        cv2.circle(img, (int(r.integers(0, w)), int(r.integers(0, h))), int(r.integers(8, 60)),
                   tuple(int(v) for v in r.integers(0, 255, 3)), -1)
    return img


class FakeChat:
    """items：[{kind, text, time, self, sender}]，由舊到新；kind 為 date/text/sticker/photo/video。"""

    def __init__(self, items, fling=0.04):
        self.items, self.fling = [], fling
        y = 40
        prev_other = False
        for i, item in enumerate(items):
            it = dict(item, idx=i)
            if it["kind"] == "date":
                height = 62
            elif it["kind"] == "text":
                lines = it["text"].count("\n") + 1
                height = 90 + 56 * (lines - 1)
            elif it["kind"] == "sticker":
                height = 392
            elif it["kind"] == "photo":
                height = 840 if i % 3 == 0 else 517
            else:
                height = 500
            show_avatar = it["kind"] != "date" and not it.get("self") and not prev_other
            it["avatar"] = show_avatar
            prev_other = it["kind"] != "date" and not it.get("self")
            it["y1"], it["y2"] = y, y + height
            y += height + GAP
            self.items.append(it)
        self.total = y + 40
        self.view = BOTTOM - TOP
        self.offset = max(0, self.total - self.view)     # 一開始停在最新訊息
        self.files = {}                                  # 手機上的檔案：路徑 -> 內容
        self._make_media()

    def _make_media(self):
        """產生聊天快取與貼圖快取，並讓畫面上的媒體由它們縮放而來（和實機一樣）。"""
        number = 400000
        for it in self.items:
            if it["kind"] in ("photo", "video"):
                x1, x2 = self._x_span(it)
                w, h = x2 - x1, it["y2"] - it["y1"]
                base = texture(_seed(it["idx"], "base"), 1200, int(1200 * h / w))
                thumb = cv2.resize(base, (500, int(500 * h / w)), interpolation=cv2.INTER_AREA)
                it["display"] = cv2.resize(thumb, (w, h), interpolation=cv2.INTER_LINEAR)
                number += 7
                it["number"] = str(number)
                folder = f"{CHATS}/{CHAT_ID}/messages/{number}"
                self.files[folder + ".thumb"] = jpg(thumb, 80)
                if it["kind"] == "video":
                    self.files[folder] = b"\x00\x00\x00\x18ftypmp42" + bytes(2000)
                else:
                    self.files[folder] = jpg(base, 85)
                    if it["idx"] % 3 == 0:
                        self.files[folder + ".original"] = jpg(cv2.resize(base, (2400, base.shape[0] * 2)), 92)
            elif it["kind"] == "sticker":
                x1, x2 = self._x_span(it)
                sticker = make_sticker(_seed(it["idx"], "sticker"))
                it["display"] = composite(sticker, (x2 - x1, it["y2"] - it["y1"]))
                it["sticker_id"] = str(5000000 + it["idx"])
                self.files[f"{STICKERS}/1000/{it['sticker_id']}"] = cv2.imencode(".png", sticker)[1].tobytes()
        # 其他聊天室的照片和其他貼圖，當作干擾
        for k in range(30):
            self.files[f"{CHATS}/c{'cd' * 16}/messages/{300000 + k}.thumb"] = jpg(texture(_seed("other", k), 500, 375))
        for k in range(40):
            self.files[f"{STICKERS}/2000/{6000000 + k}"] = cv2.imencode(".png", make_sticker(_seed("st", k)))[1].tobytes()
            self.files[f"{STICKERS}/2000/{6000000 + k}_key"] = cv2.imencode(".png", make_sticker(_seed("key", k), 108, 100))[1].tobytes()

    # ---- 檔案（adb pull） ----
    def list_files(self, remote_dir):
        prefix = remote_dir.rstrip("/") + "/"
        return [(len(data), path[len(prefix):]) for path, data in self.files.items() if path.startswith(prefix)]

    def pull(self, remote, local_dir):
        if remote in self.files:
            self._write(os.path.join(local_dir, os.path.basename(remote)), self.files[remote])
            return
        parent = os.path.dirname(remote.rstrip("/"))
        for path, data in self.files.items():
            if path.startswith(remote.rstrip("/") + "/"):
                self._write(os.path.join(local_dir, path[len(parent) + 1:]), data)

    def pull_many(self, remotes, local_dir):
        for remote in remotes:
            self.pull(remote, local_dir)

    @staticmethod
    def _write(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    # ---- Device 介面 ----
    def screen_size(self):
        return W, H, 420

    def wake(self):
        pass

    def swipe(self, x, y1, y2, duration_ms):
        distance = (y2 - y1) * (1 + self.fling)
        self.offset = max(0, self.offset - int(distance))

    def screencap(self):
        img = self.render()
        return cv2.imencode(".png", img)[1].tobytes(), img

    def dump(self):
        return self.xml()

    # ---- 產生畫面 ----
    def visible(self):
        for it in self.items:
            y1, y2 = it["y1"] - self.offset + TOP, it["y2"] - self.offset + TOP
            if y2 > TOP and y1 < BOTTOM:
                yield it, y1, y2

    def _x_span(self, it):
        if it["kind"] == "text":
            width = min(760, 150 + 26 * max(len(s) for s in it["text"].split("\n")))
        elif it["kind"] == "sticker":
            width = 453
        else:
            width = 388
        return (W - 21 - width, W - 21) if it.get("self") else (114, 114 + width)

    def render(self):
        if getattr(self, "_canvas", None) is None:
            self._canvas = self._draw()
        img = np.full((H, W, 3), (205, 214, 222), np.uint8)
        img[:TOP] = (70, 75, 105)
        img[BOTTOM:] = (240, 240, 240)
        view = self._canvas[self.offset:self.offset + self.view]
        img[TOP:TOP + view.shape[0]] = view
        return img

    def _draw(self):
        canvas = np.full((self.total + 2000, W, 3), BG, np.uint8)
        for it in self.items:
            y1, y2 = it["y1"], it["y2"]
            if it["kind"] == "date":
                cv2.rectangle(canvas, (446, y1 + 10), (635, y2 - 10), (180, 180, 180), -1)
                continue
            x1, x2 = self._x_span(it)
            if it["kind"] == "text":
                color = (140, 230, 150) if it.get("self") else (255, 255, 255)
                cv2.rectangle(canvas, (x1, y1), (x2, y2), color, -1)
                label = hashlib.md5(it["text"].encode()).hexdigest()[:12]
                cv2.putText(canvas, label, (x1 + 20, y1 + 60), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (40, 40, 40), 3)
            else:
                canvas[y1:y2, x1:x2] = it["display"]
            if it["avatar"]:
                cv2.circle(canvas, (60, y1 + 40), 38, (90, 120, 200), -1)
        return canvas

    def xml(self):
        rows = []
        for it, y1, y2 in self.visible():
            if it["kind"] == "date":
                rows.append(xmlgen.date_row(y1, y2, it["text"]))
            elif it["kind"] == "text":
                rows.append(xmlgen.text_row(y1, y2, it["text"], it["time"], it.get("self"),
                                            sender=it.get("sender", "") if it["avatar"] else ""))
            else:
                rows.append(xmlgen.media_row(y1, y2, it["kind"], it["time"], it.get("self"),
                                             sender=it.get("sender", "") if it["avatar"] else "",
                                             x_span=self._x_span(it) if not it.get("self") else (0, self._x_span(it)[1] - self._x_span(it)[0])))
        xml = xmlgen.hierarchy((0, TOP, W, BOTTOM), rows)
        return clip_to_list(xml)


def clip_to_list(xml):
    """模仿 uiautomator：只保留看得到的元件，範圍裁到聊天區內。"""
    root = ET.fromstring(xml)

    def bounds(n):
        return list(map(int, re.findall(r"-?\d+", n.get("bounds"))))

    lst = next(n for n in root.iter("node") if n.get("resource-id", "").endswith("chathistory_message_list"))

    def walk(parent):
        for child in list(parent):
            x1, y1, x2, y2 = bounds(child)
            if y2 <= TOP or y1 >= BOTTOM:
                parent.remove(child)
                continue
            child.set("bounds", f"[{x1},{max(y1, TOP)}][{x2},{min(y2, BOTTOM)}]")
            walk(child)

    walk(lst)
    body = ET.tostring(root, encoding="unicode")
    return ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>" + body).encode()
