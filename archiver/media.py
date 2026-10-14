"""模組 4：取得每則貼圖、照片、影片的檔案，以及發話者頭像。

照片／影片（實機測試 7、8）：
- 聊天快取在 files/chats/<聊天室ID>/messages/，同一則訊息有 <編號>.thumb、<編號>（一般版）、
  <編號>.original（原檔）。編號是全手機共用、隨時間遞增的本機訊息編號。
- 聊天畫面顯示的就是 .thumb，所以只拉縮圖建索引：32×32 小圖粗篩，再用特徵點＋逐塊比對驗證。
  對到編號後，只拉那一則的最佳版本（原檔 > 一般版 > 縮圖）。
貼圖（實機測試 1）：
- files/stickers/<貼圖組ID>/<貼圖ID> 是 PNG，<貼圖ID>_ani 是 APNG 動畫，*_key 是選單縮圖。
- 截圖中的貼圖是合成在聊天背景上的，所以比對前先把候選貼圖合成到同樣的背景色。
都找不到時，用截圖裁切。
"""
import json
import os
import re
import shutil
from collections import Counter

import cv2
import numpy as np

from .imgio import imread, imwrite, sniff
from .imgmatch import SAME_PHOTO, features, score, similarity

LINE_FILES = "/sdcard/Android/data/jp.naver.line.android/files"
CHATS = LINE_FILES + "/chats"
STICKERS = LINE_FILES + "/stickers"
SIG = 32


def signature(gray, size=SIG):
    small = cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
    small -= small.mean()
    norm = np.linalg.norm(small)
    return small / norm if norm else small


def crop(img, bounds):
    x1, y1, x2, y2 = bounds
    h, w = img.shape[:2]
    return img[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]


# ---------------------------------------------------------------- 照片 ----

class PhotoLibrary:
    def __init__(self, device, cache_dir, chat_dir=None, refresh=False, log=print):
        self.device, self.log = device, log
        self.thumb_dir = os.path.join(cache_dir, "thumbs")
        self.full_dir = os.path.join(cache_dir, "photos")
        os.makedirs(self.thumb_dir, exist_ok=True)
        os.makedirs(self.full_dir, exist_ok=True)
        self.index_file = os.path.join(cache_dir, "thumb_index.npz")
        listing_file = os.path.join(cache_dir, "chats_listing.json")
        if os.path.exists(listing_file) and not refresh:
            with open(listing_file, encoding="utf-8") as f:
                listing = json.load(f)
        else:
            log("列出手機上的聊天媒體快取（檔案多時要幾分鐘）……")
            listing = device.list_files(CHATS)
            with open(listing_file, "w", encoding="utf-8") as f:
                json.dump(listing, f)
        self.entries = {}            # 編號 -> {"dir": 聊天室ID, "thumb"/"plain"/"original": (相對路徑, 大小)}
        for size, rel in listing:
            parts = rel.split("/")
            m = re.fullmatch(r"(\d+)(\.thumb|\.original)?", parts[-1]) if len(parts) == 3 else None
            if not m or parts[1] != "messages":
                continue
            entry = self.entries.setdefault(m.group(1), {"dir": parts[0]})
            entry[{".thumb": "thumb", ".original": "original", None: "plain"}[m.group(2)]] = (rel, size)
        self.chat_dir = chat_dir
        self.names, self.sigs = np.array([]), np.zeros((0, SIG * SIG), np.float32)

    def thumb_path(self, number):
        return os.path.join(self.thumb_dir, number + ".thumb")

    def build_index(self, dirs=None):
        """拉回需要的縮圖並建立小圖索引。dirs 為 None 時用全部聊天室。"""
        wanted = [n for n, e in self.entries.items() if "thumb" in e and (dirs is None or e["dir"] in dirs)]
        missing = [n for n in wanted
                   if not os.path.exists(self.thumb_path(n)) or os.path.getsize(self.thumb_path(n)) != self.entries[n]["thumb"][1]]
        if missing:
            self.log(f"拉回 {len(missing)} 張照片縮圖……")
            self.device.pull_many([f"{CHATS}/{self.entries[n]['thumb'][0]}" for n in missing], self.thumb_dir)
        known = {}
        if os.path.exists(self.index_file):
            data = np.load(self.index_file)
            known = dict(zip(data["names"].tolist(), data["sigs"]))
        names, sigs = [], []
        for n in wanted:
            if n not in known:
                gray = imread(self.thumb_path(n), cv2.IMREAD_GRAYSCALE)
                if gray is None:
                    continue
                known[n] = signature(gray)
            names.append(n)
            sigs.append(known[n])
        np.savez(self.index_file, names=np.array(list(known)), sigs=np.array(list(known.values()), np.float32))
        self.names = np.array(names)
        self.sigs = np.array(sigs, np.float32).reshape(-1, SIG * SIG)
        self.log(f"照片縮圖索引：{len(names)} 張")

    def lookup(self, image, candidates=10):
        """回傳 (編號, 一致區塊比例)；找不到時回傳 None。"""
        if not len(self.names) or image.size == 0:
            return None
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        order = np.argsort(-(self.sigs @ signature(gray)))[:candidates]
        query = features(gray)
        for i in order:
            path = self.thumb_path(str(self.names[i]))
            inliers, _, h = score(query, features(path))
            if inliers >= 8:
                agree = similarity(gray, path, h)
                if agree >= SAME_PHOTO:
                    return str(self.names[i]), agree
        return None

    def detect_chat_dir(self, images):
        """用幾張截圖中的照片，找出這個聊天室在快取中的目錄。"""
        self.build_index()
        votes = Counter()
        for image in images:
            hit = self.lookup(image)
            if hit:
                votes[self.entries[hit[0]]["dir"]] += 1
        if not votes:
            return None
        chat_dir, count = votes.most_common(1)[0]
        self.log(f"聊天室快取目錄：{chat_dir}（{count}/{len(images)} 張照片對到）")
        return chat_dir

    def fetch(self, number):
        """拉回這則訊息最好的版本。回傳 (本機路徑, 版本, 格式)。"""
        entry = self.entries[number]
        for variant in ("original", "plain"):
            if variant in entry:
                rel, size = entry[variant]
                local = os.path.join(self.full_dir, os.path.basename(rel))
                if not os.path.exists(local) or os.path.getsize(local) != size:
                    self.device.pull(f"{CHATS}/{rel}", self.full_dir)
                kind = sniff(local)
                if kind:
                    return local, variant, kind
        return self.thumb_path(number), "thumb", sniff(self.thumb_path(number)) or "jpg"


# ---------------------------------------------------------------- 貼圖 ----

class StickerLibrary:
    SIZE = 32

    def __init__(self, device, cache_dir, refresh=False, log=print):
        self.log = log
        self.root = os.path.join(cache_dir, "stickers")
        if refresh and os.path.isdir(self.root):
            shutil.rmtree(self.root)
        if not os.path.isdir(self.root):
            self._adopt_misplaced(cache_dir)
        if not os.path.isdir(self.root):
            log("拉回貼圖快取（約數百 MB，只需一次）……")
            device.pull_dir(STICKERS, self.root)
        self.entries = {}           # 貼圖ID -> {"pkg", "static", "ani"}
        for pkg in sorted(os.listdir(self.root)):
            pkg_dir = os.path.join(self.root, pkg)
            if not os.path.isdir(pkg_dir):
                continue
            for name in os.listdir(pkg_dir):
                m = re.fullmatch(r"(\d+)(_ani)?", name)
                if m:
                    entry = self.entries.setdefault(m.group(1), {"pkg": pkg})
                    entry["ani" if m.group(2) else "static"] = os.path.join(pkg_dir, name)
        self._build_index(os.path.join(cache_dir, "sticker_index.npz"))

    def _adopt_misplaced(self, cache_dir):
        """舊版把貼圖組目錄（數字名稱）直接拉到 cache_dir 底下；搬進 stickers/，不必重拉。"""
        if not os.path.isdir(cache_dir):
            return
        packs = [d for d in os.listdir(cache_dir) if d.isdigit() and os.path.isdir(os.path.join(cache_dir, d))]
        if not packs:
            return
        os.makedirs(self.root)
        for d in packs:
            os.replace(os.path.join(cache_dir, d), os.path.join(self.root, d))
        self.log(f"把先前拉回的 {len(packs)} 個貼圖組搬到 {self.root}")

    @staticmethod
    def _load_rgba(path):
        from PIL import Image
        try:
            with Image.open(path) as im:
                im.seek(0)
                rgba = np.array(im.convert("RGBA"))
        except Exception:
            return None
        alpha = rgba[:, :, 3]
        ys, xs = np.nonzero(alpha > 16)
        if not len(ys):
            return None
        rgba = rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        bgr = rgba[:, :, 2::-1].astype(np.float32) / 255
        return bgr, rgba[:, :, 3:].astype(np.float32) / 255

    def _build_index(self, index_file):
        ids = sorted(self.entries)
        if os.path.exists(index_file):
            data = np.load(index_file)
            if data["ids"].tolist() == ids:
                self.ids, self.bgr, self.alpha = ids, data["bgr"], data["alpha"]
                self.log(f"貼圖索引：{len(ids)} 張")
                return
        self.log(f"建立貼圖索引：{len(ids)} 張……")
        keep, bgrs, alphas = [], [], []
        for sid in ids:
            entry = self.entries[sid]
            loaded = self._load_rgba(entry.get("static") or entry["ani"])
            if loaded is None:
                continue
            bgr, alpha = loaded
            keep.append(sid)
            bgrs.append(cv2.resize(bgr, (self.SIZE, self.SIZE), interpolation=cv2.INTER_AREA))
            alphas.append(cv2.resize(alpha, (self.SIZE, self.SIZE), interpolation=cv2.INTER_AREA)[:, :, None])
        self.ids = keep
        self.bgr = np.array(bgrs, np.float32).reshape(-1, self.SIZE, self.SIZE, 3)
        self.alpha = np.array(alphas, np.float32).reshape(-1, self.SIZE, self.SIZE, 1)
        np.savez(index_file, ids=np.array(keep), bgr=self.bgr, alpha=self.alpha)

    @staticmethod
    def _content(image):
        """回傳 (背景色 BGR 0～1, 去掉背景後的內容)。"""
        border = np.concatenate([image[0], image[-1], image[:, 0], image[:, -1]])
        bg = np.median(border, axis=0)
        diff = np.abs(image.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
        ys, xs = np.nonzero(diff > 25)
        if not len(ys):
            return bg / 255, None
        return bg / 255, image[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

    @staticmethod
    def _ncc(a, b):
        a = cv2.GaussianBlur(a, (5, 5), 0).astype(np.float32).ravel()
        b = cv2.GaussianBlur(b, (5, 5), 0).astype(np.float32).ravel()
        a -= a.mean()
        b -= b.mean()
        denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
        return float((a * b).sum() / denom) if denom else 0.0

    def lookup(self, image, candidates=5):
        """回傳 (貼圖ID, 相關係數)；找不到時回傳 None。"""
        if not self.ids or image.size == 0:
            return None
        bg, content = self._content(image)
        if content is None or min(content.shape[:2]) < 16:
            return None
        q = cv2.cvtColor(cv2.resize(content, (self.SIZE, self.SIZE), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        q = q.astype(np.float32).ravel()
        q = (q - q.mean()) / (np.linalg.norm(q - q.mean()) or 1)
        comp = self.bgr * self.alpha + bg.astype(np.float32) * (1 - self.alpha)
        gray = (comp @ np.array([0.114, 0.587, 0.299], np.float32)).reshape(len(self.ids), -1) * 255
        gray -= gray.mean(axis=1, keepdims=True)
        gray /= np.linalg.norm(gray, axis=1, keepdims=True) + 1e-6
        order = np.argsort(-(gray @ q))[:candidates]
        h, w = content.shape[:2]
        target = cv2.cvtColor(content, cv2.COLOR_BGR2GRAY)
        for i in order:
            sid = self.ids[i]
            entry = self.entries[sid]
            loaded = self._load_rgba(entry.get("static") or entry["ani"])
            if loaded is None:
                continue
            bgr, alpha = loaded
            bgr = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_AREA)
            alpha = cv2.resize(alpha, (w, h), interpolation=cv2.INTER_AREA)[:, :, None]
            cand = ((bgr * alpha + bg * (1 - alpha)) * 255).astype(np.uint8)
            ncc = self._ncc(cv2.cvtColor(cand, cv2.COLOR_BGR2GRAY), target)
            if ncc >= 0.85:
                return sid, ncc
        return None


# ---------------------------------------------------------------- 整合 ----

class FrameCache:
    def __init__(self, capture_dir, size=8):
        self.dir, self.size, self.items = capture_dir, size, {}

    def get(self, step):
        if step not in self.items:
            if len(self.items) >= self.size:
                self.items.pop(next(iter(self.items)))
            self.items[step] = imread(os.path.join(self.dir, f"{step:05d}.png"))
        return self.items[step]


def expand_albums(messages, uniques, pairs):
    """一次傳多張的照片在畫面上是同一列（多個 media），txt 則是多則 [照片]。
    回傳每則訊息對到的 (列索引, 該列第幾張媒體)。"""
    slots = [(j, 0) if j is not None else None for j, _ in pairs]
    for i, msg in enumerate(messages):
        if slots[i] is None or msg.kind != "photo":
            continue
        j, _ = slots[i]
        count = len(uniques[j].row.media)
        k, nxt = 1, i + 1
        while k < count and nxt < len(messages):
            other = messages[nxt]
            if other.kind != "photo" or other.sender != msg.sender or other.time != msg.time or slots[nxt] is not None:
                break
            slots[nxt] = (j, k)
            k, nxt = k + 1, nxt + 1
    return slots


def resolve(workdir, chat, uniques, pairs, device=None, cache_dir=None, chat_dir=None,
            stickers=True, photos=True, refresh=False, log=print):
    """產生 <工作目錄>/site/media/ 與 media.json：{訊息索引: [媒體…]}、{"avatars": {名稱: 路徑}}。"""
    cache_dir = cache_dir or os.path.join(workdir, "cache")
    site = os.path.join(workdir, "site")
    for sub in ("stickers", "photos", "crops", "avatars"):
        os.makedirs(os.path.join(site, "media", sub), exist_ok=True)
    frames = FrameCache(os.path.join(workdir, "capture"))
    slots = expand_albums(chat.messages, uniques, pairs)

    def save_crop(name, image):
        rel = f"media/crops/{name}.png"
        imwrite(os.path.join(site, rel), image)
        return rel

    def media_crop(slot):
        j, k = slot
        row = uniques[j].row
        if k >= len(row.media):
            return None
        return crop(frames.get(uniques[j].frame), row.media[k])

    sticker_lib = photo_lib = None
    if device is not None and stickers and any(m.kind == "sticker" for m in chat.messages):
        sticker_lib = StickerLibrary(device, cache_dir, refresh=refresh, log=log)
    if device is not None and photos and any(m.kind in ("photo", "video") for m in chat.messages):
        photo_lib = PhotoLibrary(device, cache_dir, chat_dir, refresh=refresh, log=log)
        if not chat_dir:
            samples = [media_crop(s) for m, s in zip(chat.messages, slots) if s and m.kind == "photo"]
            samples = [s for s in samples if s is not None and s.size][:8]
            chat_dir = photo_lib.detect_chat_dir(samples) if samples else None
            if not chat_dir:
                log("找不到這個聊天室的照片快取，照片改用截圖裁切。")
        if chat_dir:
            photo_lib.build_index({chat_dir})

    result, stats = {}, Counter()
    for msg, slot in zip(chat.messages, slots):
        if msg.kind not in ("sticker", "photo", "video") or slot is None:
            continue
        image = media_crop(slot)
        if image is None or not image.size:
            continue
        item = None
        if msg.kind == "sticker" and sticker_lib:
            hit = sticker_lib.lookup(image)
            if hit:
                entry = sticker_lib.entries[hit[0]]
                src = entry.get("ani") or entry["static"]
                rel = f"media/stickers/{entry['pkg']}_{hit[0]}.png"
                if not os.path.exists(os.path.join(site, rel)):
                    shutil.copyfile(src, os.path.join(site, rel))
                item = {"type": "sticker", "src": rel, "source": "cache"}
        elif msg.kind in ("photo", "video") and photo_lib and chat_dir:
            hit = photo_lib.lookup(image)
            if hit:
                number = hit[0]
                path, variant, kind = photo_lib.fetch(number)
                thumb_rel = f"media/photos/{number}_t.jpg"
                if not os.path.exists(os.path.join(site, thumb_rel)):
                    shutil.copyfile(photo_lib.thumb_path(number), os.path.join(site, thumb_rel))
                full_rel = f"media/photos/{number}.{kind}"
                if not os.path.exists(os.path.join(site, full_rel)):
                    shutil.copyfile(path, os.path.join(site, full_rel))
                item = {"type": "video" if kind == "mp4" else "image", "src": thumb_rel, "full": full_rel,
                        "source": variant, "number": number}
        if item is None:
            item = {"type": "sticker" if msg.kind == "sticker" else "image",
                    "src": save_crop(f"{msg.idx}_{slot[1]}", image), "source": "screenshot"}
        stats[f"{msg.kind}:{item['source']}"] += 1
        result.setdefault(msg.idx, []).append(item)

    # 頭像以 txt 的發話者名稱存（畫面上的名稱可能是自己設的暱稱，和 txt 不同）
    avatars = {}
    for msg, (j, _) in zip(chat.messages, pairs):
        if j is None or msg.is_self or not msg.sender or msg.sender in avatars:
            continue
        u = uniques[j]
        x1, y1, x2, y2 = u.row.avatar or (0, 0, 0, 0)
        if x2 - x1 > 10 and y2 - y1 >= (x2 - x1) * 0.9:      # 頭像完整出現在畫面上
            rel = f"media/avatars/{len(avatars)}.png"
            imwrite(os.path.join(site, rel), crop(frames.get(u.frame), u.row.avatar))
            avatars[msg.sender] = rel

    data = {"media": {str(k): v for k, v in result.items()}, "avatars": avatars, "chat_dir": chat_dir,
            "stats": dict(stats)}
    with open(os.path.join(workdir, "media.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    for key, n in sorted(stats.items()):
        log(f"    {key}：{n}")
    return data
