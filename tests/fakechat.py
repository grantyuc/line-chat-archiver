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


def adts(seconds, rate_index=4):
    """長度為 seconds 秒的 ADTS AAC（44.1kHz；內容是空的，只有長度對）。"""
    payload = bytes(10)
    length = 7 + len(payload)
    header = bytes([0xFF, 0xF1, (1 << 6) | (rate_index << 2), 0x80 | (length >> 11),
                    (length >> 3) & 0xFF, ((length & 0x07) << 5) | 0x1F, 0xFC])
    frames = round(seconds * 44100 / 1024)
    return (header + payload) * frames


def m4a(seconds):
    """長度為 seconds 秒的 M4A（只有 ftyp 和 moov/mvhd，內容是空的）。"""
    mvhd = (bytes(4) + bytes(8) + (1000).to_bytes(4, "big") + int(seconds * 1000).to_bytes(4, "big") + bytes(80))
    mvhd = (8 + len(mvhd)).to_bytes(4, "big") + b"mvhd" + mvhd
    moov = (8 + len(mvhd)).to_bytes(4, "big") + b"moov" + mvhd
    return b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A mp42" + moov


def make_sticker(seed, w=259, h=224, frame=0):
    """透明背景上的幾個色塊，模擬貼圖 PNG（BGRA）。frame 讓色塊位移，模擬動畫的不同影格。"""
    r = np.random.default_rng(seed)
    img = np.zeros((h, w, 4), np.uint8)
    for k in range(6):
        color = tuple(int(v) for v in r.integers(0, 255, 3)) + (255,)
        cx, cy = int(r.integers(40, w - 40)), int(r.integers(40, h - 40))
        ax, ay, angle = int(r.integers(15, 60)), int(r.integers(15, 60)), int(r.integers(0, 180))
        cv2.ellipse(img, (cx + frame * 4 * (k % 2), cy - frame * 3 * (k % 3 == 0)), (ax, ay), angle + frame * 8,
                    0, 360, color, -1)
    return img


def apng(frames):
    """BGRA 影格 → APNG 位元組。"""
    import io

    from PIL import Image
    pics = [Image.fromarray(f[:, :, [2, 1, 0, 3]]) for f in frames]
    buf = io.BytesIO()
    pics[0].save(buf, format="PNG", save_all=True, append_images=pics[1:], duration=100, loop=0)
    return buf.getvalue()


def composite(bgra, background):
    """把貼圖縮放到 background 的大小，疊在背景上。"""
    h, w = background.shape[:2]
    img = cv2.resize(bgra, (w, h), interpolation=cv2.INTER_AREA).astype(np.float32)
    alpha = img[:, :, 3:] / 255
    out = img[:, :, :3] * alpha + background.astype(np.float32) * (1 - alpha)
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

    def __init__(self, items, fling=0.04, fast=False):
        self.items, self.fling, self.fast = [], fling, fast
        self.sticky = ()            # 在這些類型的列上按下去拖，聊天列表不會捲動（例如照片橫排）
        self.wall, self.wall_left = None, None   # 捲不過這個位置；wall_left 次之後才放行（None 為永遠）
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
            elif it["kind"] == "album":
                height = 312
            elif it["kind"] == "voice":
                height = 96
            elif it["kind"] == "emoji":
                height = 230
            elif it["kind"] == "combo":
                height = 456
            else:
                height = 500
            height = it.get("height", height)
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
            if it["kind"] == "voice":
                number += 7
                it["number"] = str(number)
                self.files[f"{CHATS}/{CHAT_ID}/messages/voice_{number}.aac"] = adts(it["seconds"])
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
            elif it["kind"] == "album":
                # 一次傳多張：快取中編號相鄰；畫面上的橫排只露出前兩張，裁成正方形
                it["numbers"], it["displays"] = [], []
                for k in range(it["count"]):
                    base = texture(_seed(it["idx"], "album", k), 1200, 900)
                    thumb = cv2.resize(base, (500, 375), interpolation=cv2.INTER_AREA)
                    number += 1
                    it["numbers"].append(str(number))
                    folder = f"{CHATS}/{CHAT_ID}/messages/{number}"
                    self.files[folder + ".thumb"] = jpg(thumb, 80)
                    self.files[folder] = jpg(base, 85)
                    if k < 2:
                        square = thumb[:, 62:437]
                        it["displays"].append(cv2.resize(square, (312, 312), interpolation=cv2.INTER_AREA))
                number += 6
            elif it["kind"] == "sticker":
                seed = _seed(it["idx"], "sticker")
                it["sticker_id"] = str(5000000 + it["idx"])
                base = f"{STICKERS}/1000/{it['sticker_id']}"
                self.files[base] = cv2.imencode(".png", make_sticker(seed))[1].tobytes()
                if it["idx"] % 2:          # 動態貼圖：畫面上停在第 3 格
                    frames = [make_sticker(seed, frame=k) for k in range(4)]
                    self.files[base + "_ani"] = apng(frames)
                    it["sticker_bgra"] = frames[2]
                    it["animated"] = True
                else:
                    it["sticker_bgra"] = make_sticker(seed)
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

    def pull(self, remote, local):
        """和真的 adb pull 一樣：拉目錄時，本機目標不存在就直接當成該目錄，存在則在裡面再建一層。"""
        if remote in self.files:
            target = os.path.join(local, os.path.basename(remote)) if os.path.isdir(local) else local
            self._write(target, self.files[remote])
            return
        remote = remote.rstrip("/")
        base = os.path.join(local, os.path.basename(remote)) if os.path.isdir(local) else local
        for path, data in self.files.items():
            if path.startswith(remote + "/"):
                self._write(os.path.join(base, path[len(remote) + 1:]), data)

    def pull_dir(self, remote_dir, target):
        from archiver.device import Device
        Device.pull_dir(self, remote_dir, target)

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

    def drag(self, x, y1, y2):
        """快速模式的拖曳：停住再放開，沒有慣性。"""
        for it, sy1, sy2 in self.visible():
            xa, xb = self._x_span(it)
            if it["kind"] in self.sticky and sy1 <= y1 <= sy2 and xa <= x <= xb:
                return
        target = min(max(0, self.offset - (y2 - y1)), max(0, self.total - self.view))
        if self.wall is not None and target < self.wall:
            if self.wall_left is None or self.wall_left > 0:     # 還在載入更舊的訊息
                self.wall_left = None if self.wall_left is None else self.wall_left - 1
                target = max(target, min(self.offset, self.wall))
            else:
                self.wall = None
        self.offset = target

    def screencap(self):
        """一般模式和 adb 一樣傳回 PNG；快速模式和 uiautomator2 一樣傳回 JPEG。"""
        img = self.render()
        if self.fast:
            data = jpg(img, 80)
            return data, cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
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
        elif it["kind"] == "album":
            width = 630
        elif it["kind"] == "voice":
            width = 280
        elif it["kind"] == "emoji":
            width = 220 * it["text"].count("(")
        elif it["kind"] == "combo":
            width = 667
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
        for y in range(0, canvas.shape[0], 36):          # 有花紋的聊天背景
            cv2.line(canvas, (0, y), (W, y + 120), (214, 222, 230), 7)
        for it in self.items:
            y1, y2 = it["y1"], it["y2"]
            if it["kind"] == "date":
                cv2.rectangle(canvas, (446, y1 + 10), (635, y2 - 10), (180, 180, 180), -1)
                continue
            x1, x2 = self._x_span(it)
            if it["kind"] == "text":
                # 和實機一樣：外側 14px 只有頂端畫尾巴，其餘透明；文字留在泡泡裡（左右各留 16px）
                color = (140, 230, 150) if it.get("self") else (255, 255, 255)
                bx1, bx2, tail = (x1, x2 - 14, x2) if it.get("self") else (x1 + 14, x2, x1)
                inner = np.full((y2 - y1, max(1, bx2 - bx1 - 32), 3), color, np.uint8)
                label = hashlib.md5(it["text"].encode()).hexdigest()[:12]
                cv2.putText(inner, label, (4, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (40, 40, 40), 3)
                cv2.rectangle(canvas, (bx1, y1), (bx2, y2), color, -1)
                canvas[y1:y2, bx1 + 16:bx1 + 16 + inner.shape[1]] = inner
                corner = bx2 if it.get("self") else bx1
                cv2.fillPoly(canvas, [np.array([(corner, y1), (tail, y1), (corner, y1 + 30)])], color)
            elif it["kind"] == "sticker":
                canvas[y1:y2, x1:x2] = composite(it["sticker_bgra"], canvas[y1:y2, x1:x2])
                if it.get("custom_text"):            # 訊息貼圖：使用者輸入的文字畫在貼圖上
                    cv2.putText(canvas, it["custom_text"], (x1 + 30, y1 + 200), cv2.FONT_HERSHEY_SIMPLEX, 2.2,
                                (20, 20, 20), 7)
            elif it["kind"] == "emoji":
                for k, box in enumerate(self.emoji_boxes(it)):
                    name = re.findall(r"\([^()]+\)", it["text"])[k]
                    color = tuple(int(v) for v in np.random.default_rng(_seed(name)).integers(30, 200, 3))
                    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
                    cv2.circle(canvas, (cx, cy), 95, color, -1)
                    cv2.circle(canvas, (cx - 35, cy - 25), 18, (255, 255, 255), -1)
                    cv2.ellipse(canvas, (cx, cy + 30), (50, 25), 0, 0, 180, (255, 255, 255), 8)
            elif it["kind"] == "combo":
                for k, box in enumerate(self.combo_boxes(it)):
                    bgra = make_sticker(_seed(it["idx"], "combo", k), box[2] - box[0], box[3] - box[1])
                    canvas[box[1]:box[3], box[0]:box[2]] = composite(bgra, canvas[box[1]:box[3], box[0]:box[2]])
            elif it["kind"] == "voice":
                cv2.rectangle(canvas, (x1, y1), (x2, y2), (255, 255, 255), -1)
                cv2.putText(canvas, f"{it['seconds']}s #{it['idx']}", (x1 + 90, y1 + 62), cv2.FONT_HERSHEY_SIMPLEX,
                            1.2, (40, 40, 40), 3)
            elif it["kind"] == "album":
                canvas[y1:y2, x1:x1 + 312] = it["displays"][0]
                canvas[y1:y2, x1 + 318:x1 + 630] = it["displays"][1]
            else:
                canvas[y1:y2, x1:x2] = it["display"]
            if it["avatar"]:
                cv2.circle(canvas, (60, y1 + 40), 38, (90, 120, 200), -1)
            if it.get("reaction"):
                bx = self.reaction_box(it, 0)
                cv2.circle(canvas, ((bx[0] + bx[2]) // 2, (bx[1] + bx[3]) // 2), 24,
                           (40, 140, 250) if it["reaction"] == "有趣" else (60, 200, 60), -1)
        return canvas

    # ---- 新類型的版面（整段對話座標；shift 換成畫面座標） ----
    def emoji_boxes(self, it, shift=0):
        x1, _ = self._x_span(it)
        return [(x1 + 220 * k, it["y1"] + 5 - shift, x1 + 220 * (k + 1), it["y1"] + 225 - shift)
                for k in range(it["text"].count("("))]

    def combo_boxes(self, it, shift=0):
        x1, _ = self._x_span(it)
        y1 = it["y1"] - shift
        return [(x1 + 114, y1, x1 + 544, y1 + 430), (x1, y1 + 186, x1 + 270, y1 + 456)]

    def reaction_box(self, it, shift=0):
        _, x2 = self._x_span(it)
        y2 = it["y2"] - shift
        return (x2 - 53, y2 - 53, x2, y2)

    def xml(self):
        rows = []
        for it, y1, y2 in self.visible():
            if it["kind"] == "date":
                rows.append(xmlgen.date_row(y1, y2, it["text"]))
            elif it["kind"] == "album":
                x1, _ = self._x_span(it)
                boxes = [(x1, y1, x1 + 312, y2), (x1 + 318, y1, x1 + 630, y2)]
                rows.append(xmlgen.album_row(y1, y2, it["time"], it.get("self"), boxes,
                                             sender=it.get("sender", "") if it["avatar"] else ""))
            elif it["kind"] == "text":
                rows.append(xmlgen.text_row(y1, y2, it["text"], it["time"], it.get("self"),
                                            sender=it.get("sender", "") if it["avatar"] else "",
                                            x_span=self._x_span(it)))
            elif it["kind"] == "voice":
                rows.append(xmlgen.voice_row(y1, y2, it["seconds"], it["time"], it.get("self"),
                                             sender=it.get("sender", "") if it["avatar"] else ""))
            elif it["kind"] == "emoji":
                rows.append(xmlgen.emoji_row(y1, y2, self.emoji_boxes(it, it["y1"] - y1), it["time"], it.get("self"),
                                             sender=it.get("sender", "") if it["avatar"] else ""))
            elif it["kind"] == "combo":
                rows.append(xmlgen.combo_row(y1, y2, self._x_span(it), self.combo_boxes(it, it["y1"] - y1), it["time"],
                                             it.get("self"), sender=it.get("sender", "") if it["avatar"] else ""))
            else:
                rows.append(xmlgen.media_row(y1, y2, it["kind"], it["time"], it.get("self"),
                                             sender=it.get("sender", "") if it["avatar"] else "",
                                             x_span=self._x_span(it) if not it.get("self") else (0, self._x_span(it)[1] - self._x_span(it)[0])))
        for k, (it, y1, y2) in enumerate(self.visible()):              # 回應：加在那一列裡
            if it.get("reaction"):
                rows[k] = add_child(rows[k], xmlgen.reaction_node(it["reaction"], self.reaction_box(it, it["y1"] - y1)))
        xml = xmlgen.hierarchy((0, TOP, W, BOTTOM), rows)
        return clip_to_list(xml)


def add_child(row, child):
    """在 xmlgen 產生的節點字串裡加一個子節點。"""
    if row.endswith("/>"):
        return row[:-2] + ">" + child + "</node>"
    return row[:-len("</node>")] + child + "</node>"


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
