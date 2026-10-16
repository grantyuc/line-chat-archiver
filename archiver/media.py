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
import bisect
import json
import os
import re
import shutil
from collections import Counter

import cv2
import numpy as np

from .capture import image_path
from .imgio import MP4_BOXES, head_hex, imread, imwrite, is_animated, sniff
from .imgmatch import SAME_PHOTO, features, score, similarity

CUSTOM_STICKER = 0.03   # 截圖比快取的貼圖多出這麼多內容，就當成訊息貼圖／拼拼樂，改用截圖
NEARBY_PHOTO = 0.65     # 依編號限定候選時的門檻（見 PhotoLibrary.match_nearby）
NEARBY_MARGIN = 0.3     # 而且要比第二高的候選高出這麼多
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


EMOJI_RE = re.compile(r"\(([^()\n]{1,24})\)")


def cut_out(image):
    """放大顯示的表情沒有泡泡，周圍是聊天背景：把接近背景色（取四邊的中位數）的地方變透明。"""
    ring = np.concatenate([image[0], image[-1], image[:, 0], image[:, -1]]).astype(np.int16)
    background = np.median(ring, axis=0)
    diff = np.abs(image.astype(np.int16) - background).max(axis=2)
    alpha = np.clip((diff - 10) * 255 / 30, 0, 255).astype(np.uint8)
    return np.dstack([image, alpha])


def name_file(prefix, name):
    """用名稱（例如「有趣」「(hahaha)」）當檔名的一部分不安全，改用雜湊。"""
    import hashlib
    return f"{prefix}{hashlib.md5(name.encode('utf-8')).hexdigest()[:12]}.png"


def shrink(img, side=480):
    """縮到長邊不超過 side；img 為 None 時回傳 None。"""
    if img is None:
        return None
    scale = side / max(img.shape[:2])
    return cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else img


# ---------------------------------------------------------------- 照片 ----

class PhotoLibrary:
    def __init__(self, device, cache_dir, chat_dir=None, refresh=False, log=print):
        self.device, self.log = device, log
        self.thumb_dir = os.path.join(cache_dir, "thumbs")
        self.full_dir = os.path.join(cache_dir, "photos")
        self.voice_dir = os.path.join(cache_dir, "voice")
        os.makedirs(self.thumb_dir, exist_ok=True)
        os.makedirs(self.full_dir, exist_ok=True)
        self.index_file = os.path.join(cache_dir, "thumb_index.npz")
        self.listing_file = os.path.join(cache_dir, "chats_listing.json")
        if os.path.exists(self.listing_file) and not refresh:
            with open(self.listing_file, encoding="utf-8") as f:
                self.listing = json.load(f)
        else:
            log("列出手機上的聊天媒體快取（檔案多時要幾分鐘）……")
            self.listing = device.list_files(CHATS)
            self._save_listing()
        self._parse_listing()
        self.chat_dir = chat_dir
        self.names, self.sigs = np.array([]), np.zeros((0, SIG * SIG), np.float32)
        self.unknown = {}

    def _save_listing(self):
        with open(self.listing_file, "w", encoding="utf-8") as f:
            json.dump(self.listing, f)

    def _parse_listing(self):
        self.entries = {}            # 編號 -> {"dir": 聊天室ID, "thumb"/"plain"/"original": (相對路徑, 大小)}
        self.voices = {}             # 編號 -> {"dir": 聊天室ID, "file": (相對路徑, 大小)}
        for size, rel in self.listing:
            parts = rel.split("/")
            if len(parts) != 3 or parts[1] != "messages":
                continue
            v = re.fullmatch(r"voice_(\d+)\.aac", parts[-1])
            if v:
                self.voices[v.group(1)] = {"dir": parts[0], "file": (rel, size)}
                continue
            m = re.fullmatch(r"(\d+)(\.thumb|\.original)?", parts[-1])
            if not m:
                continue
            entry = self.entries.setdefault(m.group(1), {"dir": parts[0]})
            entry[{".thumb": "thumb", ".original": "original", None: "plain"}[m.group(2)]] = (rel, size)

    def refresh_chat(self, chat_dir):
        """重新列出這個聊天室的快取，回傳多了幾個檔案。
        快取清單是第一次執行時存下的；之後擷取畫面時，LINE 可能又下載了舊照片的縮圖。"""
        old = {rel for _, rel in self.listing if rel.startswith(chat_dir + "/")}
        fresh = [[size, f"{chat_dir}/{rel}"] for size, rel in self.device.list_files(f"{CHATS}/{chat_dir}")]
        self.listing = [x for x in self.listing if not x[1].startswith(chat_dir + "/")] + fresh
        self._save_listing()
        self._parse_listing()
        added = sum(1 for _, rel in fresh if rel not in old)
        if added:
            self.build_index({chat_dir})
        return added

    def voice_numbers(self, chat_dir):
        """這個聊天室快取中的語音訊息編號（由小到大）。"""
        return sorted(int(n) for n, v in self.voices.items() if v["dir"] == chat_dir)

    def fetch_voice(self, number):
        """拉回一則語音訊息的檔案，回傳本機路徑。"""
        rel, size = self.voices[str(number)]["file"]
        os.makedirs(self.voice_dir, exist_ok=True)
        local = os.path.join(self.voice_dir, os.path.basename(rel))
        if not os.path.exists(local) or os.path.getsize(local) != size:
            self.device.pull(f"{CHATS}/{rel}", self.voice_dir)
        return local

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

    def verify(self, image, path):
        """截圖中的照片和這個檔案的一致區塊比例（0～1）。"""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        inliers, _, h = score(features(gray), features(path))
        return similarity(gray, path, h) if inliers >= 8 else 0.0

    def nearby(self, chat_dir, after, before, used, limit=6):
        """這個聊天室快取中編號介於 after 和 before 之間、還沒用過的訊息（含沒有縮圖的）。
        只知道一邊時，取最靠近那一邊的 limit 則。"""
        found = sorted(int(n) for n, e in self.entries.items()
                       if e["dir"] == chat_dir and int(n) not in used
                       and (after is None or int(n) > after) and (before is None or int(n) < before))
        if after is None and before is None:
            return []
        return found[:limit] if after is not None else found[-limit:]

    def match_nearby(self, image, chat_dir, after, before, used):
        """縮圖索引找不到時，逐一比對編號相鄰的訊息：縮圖，再試一般版或原檔。
        回傳 (編號或 None, [(編號, 有哪些版本, 一致比例)])。

        候選已經由前後訊息的編號限定在很小的範圍，所以門檻比全索引搜尋低：沒有一張達到
        SAME_PHOTO 時，最高的一張達到 NEARBY_PHOTO、又明顯高於其他候選，也算對到。
        影片縮圖在畫面上蓋著播放鍵和片長，一致比例常在 0.8 左右。
        """
        tried = []
        for number in self.nearby(chat_dir, after, before, used):
            n = str(number)
            entry, best = self.entries[n], 0.0
            if "thumb" in entry and os.path.exists(self.thumb_path(n)):
                best = self.verify(image, self.thumb_path(n))
            if best < SAME_PHOTO and ("plain" in entry or "original" in entry):
                path, _, kind = self.fetch(n)
                if kind != "mp4":
                    best = max(best, self.verify(image, path))
            variants = "/".join(v for v in ("thumb", "plain", "original") if v in entry)
            tried.append((number, variants, round(best, 2)))
            if best >= SAME_PHOTO:
                return n, tried
        ranked = sorted(tried, key=lambda t: -t[2])
        if ranked and ranked[0][2] >= NEARBY_PHOTO and (len(ranked) == 1 or ranked[0][2] - ranked[1][2] >= NEARBY_MARGIN):
            return str(ranked[0][0]), tried
        return None, tried

    def detect_chat_dir(self, images, most=40, enough=3):
        """用截圖中的照片找出這個聊天室在快取中的目錄。images 可以是產生器，依序取用；
        最多試 most 張，有 enough 張對到同一個目錄就停。"""
        self.build_index()
        votes, tried = Counter(), 0
        for image in images:
            if image is None or not image.size:
                continue
            tried += 1
            hit = self.lookup(image)
            if hit:
                votes[self.entries[hit[0]]["dir"]] += 1
                if votes.most_common(1)[0][1] >= enough:
                    break
            if tried >= most:
                break
        if not votes:
            self.log(f"用 {tried} 張截圖中的照片找這個聊天室的快取目錄，都沒有對到。")
            return None
        chat_dir, count = votes.most_common(1)[0]
        self.log(f"聊天室快取目錄：{chat_dir}（試了 {tried} 張照片，{count} 張對到這個目錄）")
        return chat_dir

    def chat_numbers(self, chat_dir):
        """這個聊天室快取中有縮圖的訊息編號（由小到大）。"""
        return sorted(int(n) for n, e in self.entries.items() if e["dir"] == chat_dir and "thumb" in e)

    def fetch(self, number):
        """拉回這則訊息最好的版本。回傳 (本機路徑, 版本, 格式)。
        有大圖或影片檔但認不得格式時，記在 self.unknown[編號] = [(版本, 大小, 檔頭)]，報告中列出。"""
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
                self.unknown.setdefault(number, []).append((variant, size, head_hex(local)))
        return self.thumb_path(number), "thumb", sniff(self.thumb_path(number)) or "jpg"


# ---------------------------------------------------------------- 貼圖 ----

class StickerLibrary:
    SIZE = 32
    ANI_FRAMES = 12         # 動態貼圖每張取幾格建索引
    MAX_FRAMES = 60         # 驗證時最多比對幾格

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
    def _frames(path, limit):
        """回傳 [(影格編號, BGRA 整張畫布)]。靜態 PNG 一格；APNG 平均取 limit 格。"""
        from PIL import Image
        try:
            with Image.open(path) as im:
                count = getattr(im, "n_frames", 1)
                n = min(count, limit)
                picks = sorted({round(i * (count - 1) / max(1, n - 1)) for i in range(n)})
                frames = []
                for k in picks:
                    im.seek(k)
                    rgba = np.array(im.convert("RGBA"))
                    frames.append((k, rgba[:, :, [2, 1, 0, 3]]))
                return frames
        except Exception:
            return []

    def _build_index(self, index_file):
        ids = sorted(self.entries)
        if os.path.exists(index_file):
            data = np.load(index_file)
            # 舊版的索引沒有 version 欄位，或格式不同：重建
            if "version" in data.files and int(data["version"]) == 3 and data["ids"].tolist() == ids:
                self._set(ids, data)
                self.log(f"貼圖索引：{len(ids)} 張（{len(self.owner)} 個影格）")
                return
        self.log(f"建立貼圖索引：{len(ids)} 張……")
        owner, paths, frame_no, aspect, gray, alpha = [], [], [], [], [], []
        for n, sid in enumerate(ids):
            entry = self.entries[sid]
            for key, limit in (("static", 1), ("ani", self.ANI_FRAMES)):
                if key not in entry:
                    continue
                for k, bgra in self._frames(entry[key], limit):
                    if bgra[:, :, 3].max() < 16:
                        continue
                    g, core = self._small(bgra)
                    owner.append(n)
                    paths.append(entry[key])
                    frame_no.append(k)
                    aspect.append(bgra.shape[1] / bgra.shape[0])
                    gray.append(g)
                    alpha.append(core)
        data = {"ids": np.array(ids), "owner": np.array(owner, np.int32), "paths": np.array(paths),
                "frame_no": np.array(frame_no, np.int32), "aspect": np.array(aspect, np.float32),
                "gray": np.array(gray, np.float32).reshape(-1, self.SIZE * self.SIZE),
                "alpha": np.array(alpha, np.float32).reshape(-1, self.SIZE * self.SIZE), "version": 3}
        np.savez(index_file, **data)
        self._set(ids, data)

    def _small(self, bgra):
        """縮成 SIZE×SIZE：回傳 (灰階, 權重)。只用完全不透明的核心像素，邊緣的半透明像素
        會混進背景色，在截圖和貼圖檔之間對不上。"""
        a = bgra[:, :, 3].astype(np.float32) / 255
        size = (self.SIZE, self.SIZE)
        small_a = cv2.resize(a, size, interpolation=cv2.INTER_AREA)
        premul = cv2.resize(bgra[:, :, :3].astype(np.float32) * a[:, :, None], size, interpolation=cv2.INTER_AREA)
        color = np.clip(premul / np.maximum(small_a[:, :, None], 1e-3), 0, 255).astype(np.float32)
        core = small_a > 0.9
        if core.sum() < 20:
            core = small_a > 0.5
        return cv2.cvtColor(color, cv2.COLOR_BGR2GRAY).ravel(), core.astype(np.float32).ravel()

    def _set(self, ids, data):
        self.ids = ids
        self.owner, self.paths, self.frame_no = data["owner"], data["paths"], data["frame_no"]
        self.aspect, self.gray, self.alpha = data["aspect"], data["gray"], data["alpha"]
        # 預先算好加權相關係數要用的量
        weight = self.alpha / (self.alpha.sum(axis=1, keepdims=True) + 1e-6)
        self.wg = weight * self.gray
        self.weight = weight
        self.mean_g = self.wg.sum(axis=1)
        self.var_g = (self.wg * self.gray).sum(axis=1) - self.mean_g ** 2

    @staticmethod
    def compare(candidate, alpha, target):
        """只看貼圖不透明的像素，回傳 (相關係數, 平均色差)。

        相關係數只看明暗變化像不像；色塊很平的貼圖換一張也可能很高，所以另外要求顏色本身接近。
        """
        mask = cv2.erode((alpha > 0.5).astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
        if mask.sum() < 50:
            return 0.0, 255.0
        a = cv2.GaussianBlur(candidate, (3, 3), 0)[mask].astype(np.float32)
        b = cv2.GaussianBlur(target, (3, 3), 0)[mask].astype(np.float32)
        mae = float(np.abs(a - b).mean())
        a = a.mean(axis=1) - a.mean()
        b = b.mean(axis=1) - b.mean()
        denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
        return (float((a * b).sum() / denom) if denom else 0.0), mae

    @staticmethod
    def extra_content(bgra, image):
        """截圖比快取的貼圖多出來的內容（0～1）：例如訊息貼圖上自己輸入的文字。

        看兩處：貼圖不透明的地方顏色差很多的比例；以及貼圖透明的地方（應該只看得到聊天背景）
        和背景差很多的比例。取較大者。一般的貼圖對到之後兩者都接近 0。
        """
        alpha = bgra[:, :, 3]
        solid = cv2.erode((alpha > 230).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        clear = cv2.erode((alpha < 25).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
        a = cv2.GaussianBlur(bgra[:, :, :3], (3, 3), 0).astype(np.int16)
        b = cv2.GaussianBlur(image, (3, 3), 0).astype(np.int16)
        parts = []
        if solid.sum() > 200:
            parts.append(float((np.abs(a - b).max(axis=2)[solid] > 60).mean()))
        if clear.sum() > 200:
            ring = np.concatenate([image[0], image[-1], image[:, 0], image[:, -1]]).astype(np.int16)
            background = np.median(ring, axis=0)
            parts.append(float((np.abs(b - background).max(axis=2)[clear] > 80).mean()))
        return max(parts) if parts else 0.0

    def lookup(self, image, candidates=24):
        """回傳 (貼圖ID, 相關係數, 多出來的內容比例)；找不到時回傳 None。

        LINE 把整張貼圖畫布（含透明邊）縮放到貼圖元件的範圍，所以把候選貼圖縮放到截圖大小，
        只比對不透明的像素。背景有花紋、或截到動畫的其他影格，都不影響。
        """
        if not len(self.owner) or image.size == 0 or min(image.shape[:2]) < 16:
            return None
        h, w = image.shape[:2]
        target = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        c = cv2.resize(target, (self.SIZE, self.SIZE), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
        ok = np.abs(np.log(self.aspect / (w / h))) < 0.06          # 畫布長寬比要相同
        if not ok.any():
            return None
        mean_c = self.weight[ok] @ c
        cov = self.wg[ok] @ c - self.mean_g[ok] * mean_c
        var_c = self.weight[ok] @ (c * c) - mean_c ** 2
        coarse = cov / np.sqrt(np.maximum(var_c * self.var_g[ok], 1e-6))
        index = np.nonzero(ok)[0]
        tried, best = [], None            # best = (色差, 相關係數, 貼圖ID, 影格)
        for i in index[np.argsort(-coarse)]:
            sid = self.ids[self.owner[i]]
            if sid in tried:
                continue
            tried.append(sid)
            # 粗篩只索引了部分影格；驗證時比對這張貼圖的每一格（截圖可能停在任何一格）
            entry = self.entries[sid]
            for key, limit in (("static", 1), ("ani", self.MAX_FRAMES)):
                for _, bgra in self._frames(entry[key], limit) if key in entry else []:
                    bgra = cv2.resize(bgra, (w, h), interpolation=cv2.INTER_AREA)
                    ncc, mae = self.compare(bgra[:, :, :3], bgra[:, :, 3] / 255, image)
                    if ncc >= 0.9 and mae <= 8:
                        return sid, ncc, self._extra(sid, bgra, image)     # 非常確定，不用再看其他候選
                    if ncc >= 0.85 and mae <= 15 and (best is None or mae < best[0]):
                        best = (mae, ncc, sid, bgra)
            if len(tried) >= candidates:
                break
        return (best[2], best[1], self._extra(best[2], best[3], image)) if best else None

    def _extra(self, sid, bgra, image):
        """對到的貼圖和截圖相比多出來的內容。動態貼圖對到的不一定是截圖那一格，
        多出來的看起來不少時，再和這張貼圖的每一格比，取最小的。"""
        extra = self.extra_content(bgra, image)
        if extra <= CUSTOM_STICKER:
            return extra
        h, w = image.shape[:2]
        entry = self.entries[sid]
        for key, limit in (("static", 1), ("ani", self.MAX_FRAMES)):
            for _, frame in self._frames(entry[key], limit) if key in entry else []:
                extra = min(extra, self.extra_content(cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA), image))
                if extra <= CUSTOM_STICKER:
                    return extra
        return extra


# ---------------------------------------------------------------- 整合 ----

class FrameCache:
    """依畫面順序（stitch 的 frame 編號）讀截圖，只留最近幾張在記憶體。
    paths 為各畫面的截圖路徑；省略時，畫面順序就是 capture_dir 裡的步數。"""

    def __init__(self, capture_dir=None, size=8, paths=None):
        self.dir, self.paths, self.size, self.items = capture_dir, paths, size, {}

    def get(self, frame):
        if frame not in self.items:
            if len(self.items) >= self.size:
                self.items.pop(next(iter(self.items)))
            path = self.paths[frame] if self.paths is not None else image_path(self.dir, frame)
            self.items[frame] = imread(path)
        return self.items[frame]


AAC_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)
DURATION_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")


def audio_seconds(path):
    """語音檔的長度（秒）：ADTS 格式的 AAC 數音框，MP4／M4A 讀 mvhd；讀不到時回傳 None。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if data[4:8] in MP4_BOXES:
        return _mp4_seconds(data)
    i, frames, rate = 0, 0, None
    while i + 7 <= len(data) and data[i] == 0xFF and data[i + 1] & 0xF6 == 0xF0:
        index = (data[i + 2] >> 2) & 0x0F
        length = ((data[i + 3] & 0x03) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
        if index >= len(AAC_RATES) or length < 7:
            break
        rate = AAC_RATES[index]
        frames += 1 + (data[i + 6] & 0x03)
        i += length
    return frames * 1024 / rate if rate and frames else None


def _mp4_seconds(data):
    i = data.find(b"mvhd")
    if i < 4 or i + 36 > len(data):
        return None
    if data[i + 4] == 1:                                  # 版本 1：時間欄位是 64 位元
        scale, length = int.from_bytes(data[i + 24:i + 28], "big"), int.from_bytes(data[i + 28:i + 36], "big")
    else:
        scale, length = int.from_bytes(data[i + 16:i + 20], "big"), int.from_bytes(data[i + 20:i + 24], "big")
    return length / scale if scale else None


VOICE_EXT = {"aac": "aac", "mp4": "m4a", "amr": "amr", "ogg": "ogg"}     # 語音檔的實際格式 → 網站上的副檔名


def screen_seconds(row):
    """語音訊息列上顯示的長度（例如 0:07）；看不到時回傳 None。"""
    m = DURATION_RE.search(row.text or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def assign_voices(positions, anchors, files, seconds=None, file_seconds=None, tolerance=2.0):
    """把語音訊息對到快取中的語音檔。語音沒有畫面可以比，只能靠編號的順序和長度。

    positions：語音訊息的位置（由舊到新）；anchors：{位置: 已對到的照片／影片編號}；
    files：這個聊天室的語音檔編號（由小到大）；seconds：{位置: 畫面上顯示的秒數}；
    file_seconds：{編號: 語音檔的秒數}。回傳 {位置: 編號}。

    - 則數和檔數相同：依序配對（編號隨時間遞增）。
    - 否則以前後已對到的編號分段，段內則數和檔數相同就依序配對；不同時，用長度配對（保持順序）。
    依序配對時，兩邊都知道長度的配對若長度不合，代表中間少了或多了檔案，也改用長度配對。
    """
    seconds, file_seconds = seconds or {}, file_seconds or {}

    def in_order(msgs, cands):
        pairs = dict(zip(msgs, cands))
        if any(seconds.get(p) is not None and file_seconds.get(n) is not None
               and abs(seconds[p] - file_seconds[n]) > tolerance for p, n in pairs.items()):
            return _match_by_length(msgs, cands, seconds, file_seconds, tolerance)
        return pairs

    if len(positions) == len(files):
        return in_order(positions, files)
    keyed = sorted(anchors.items())
    groups = {}
    for p in positions:
        before = [n for q, n in keyed if q < p]
        after = [n for q, n in keyed if q > p]
        groups.setdefault((before[-1] if before else -1, after[0] if after else float("inf")), []).append(p)
    result = {}
    for (low, high), msgs in groups.items():
        cands = [n for n in files if low < n < high]
        if len(cands) == len(msgs):
            result.update(in_order(msgs, cands))
        elif msgs and cands:
            result.update(_match_by_length(msgs, cands, seconds, file_seconds, tolerance))
    return result


def _match_by_length(msgs, cands, seconds, file_seconds, tolerance):
    """保持順序、長度差在 tolerance 秒內的配對，配到的越多越好（動態規劃）。"""
    n, m = len(msgs), len(cands)
    best = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            a, b = seconds.get(msgs[i]), file_seconds.get(cands[j])
            ok = a is not None and b is not None and abs(a - b) <= tolerance
            best[i][j] = max(best[i + 1][j], best[i][j + 1], best[i + 1][j + 1] + 1 if ok else 0)
    result, i, j = {}, 0, 0
    while i < n and j < m:
        a, b = seconds.get(msgs[i]), file_seconds.get(cands[j])
        if a is not None and b is not None and abs(a - b) <= tolerance and best[i][j] == best[i + 1][j + 1] + 1:
            result[msgs[i]] = cands[j]
            i, j = i + 1, j + 1
        elif best[i][j] == best[i + 1][j]:
            i += 1
        else:
            j += 1
    return result


def expand_albums(messages, uniques, pairs):
    """一次傳多張的照片在畫面上是同一列（多個 media），txt 則是連續多則 [照片]（同一人、同一分鐘）。
    回傳每則訊息對到的 (列索引, 該列第幾張媒體)。橫排（more_media）中沒露出來的照片，
    第幾張會超出該列的 media 數量，由 resolve 依快取編號推斷。"""
    slots = [(j, 0) if j is not None else None for j, _ in pairs]

    def same(a, b):
        return a.kind == "photo" and a.sender == b.sender and a.time == b.time

    for i, msg in enumerate(messages):
        if slots[i] is None or msg.kind != "photo" or slots[i][1]:
            continue
        j = slots[i][0]
        row = uniques[j].row
        if len(row.media) <= 1 and not row.more_media:
            continue
        # 對齊可能把這一列配給這批照片的任何一則：往前、往後找出整批
        start = i
        while start > 0 and slots[start - 1] is None and same(messages[start - 1], msg):
            start -= 1
        end = i
        while end + 1 < len(messages) and slots[end + 1] is None and same(messages[end + 1], msg):
            end += 1
        group = list(range(start, end + 1))
        if not row.more_media:
            group = group[:len(row.media)]
        for k, m in enumerate(group):
            slots[m] = (j, k)
    return slots


def infer_hidden(numbers, used, after, before=None):
    """橫排中沒露出來的照片：取同一聊天室快取中 after 之後的下一個未用過的編號。
    必須小於 before（下一則已對到的媒體編號），否則代表快取中缺了這張，回傳 None。"""
    k = bisect.bisect_right(numbers, after)
    while k < len(numbers) and numbers[k] in used:
        k += 1
    if k >= len(numbers) or (before is not None and numbers[k] >= before):
        return None
    return numbers[k]


def resolve(workdir, chat, uniques, pairs, device=None, cache_dir=None, chat_dir=None,
            stickers=True, photos=True, refresh=False, frame_paths=None, log=print):
    """產生 <工作目錄>/site/media/ 與 media.json：{訊息索引: [媒體…]}、{"avatars": {名稱: 路徑}}。
    frame_paths：各畫面（uniques 的 frame 編號）的截圖路徑，見 build.load_capture。"""
    cache_dir = cache_dir or os.path.join(workdir, "cache")
    site = os.path.join(workdir, "site")
    for sub in ("stickers", "photos", "crops", "avatars"):
        os.makedirs(os.path.join(site, "media", sub), exist_ok=True)
    frames = FrameCache(os.path.join(workdir, "capture"), paths=frame_paths)
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
            # 從最新的照片往前試：舊照片可能早就不在手機的快取裡
            newest_first = [s for m, s in zip(reversed(chat.messages), reversed(slots)) if s and m.kind == "photo"]
            chat_dir = photo_lib.detect_chat_dir(media_crop(s) for s in newest_first)
            if not chat_dir:
                log("找不到這個聊天室的照片快取，照片改用截圖裁切。也可以用 --chat-dir 直接指定"
                    "（u/c/m 開頭的目錄名，找法見 poc/README.md 的測試 7）。")
        if chat_dir:
            photo_lib.build_index({chat_dir})

    def photo_item(number, shot=None, photo=True):
        """photo：這則是 [照片]。照片的快取是 GIF／APNG，或被 LINE 轉成 MP4 的動圖時，標成 anim。"""
        path, variant, kind = photo_lib.fetch(number)
        thumb_rel = f"media/photos/{number}_t.jpg"
        if not os.path.exists(os.path.join(site, thumb_rel)):
            if os.path.exists(photo_lib.thumb_path(number)):
                shutil.copyfile(photo_lib.thumb_path(number), os.path.join(site, thumb_rel))
            else:                     # 快取中沒有縮圖：從大圖縮小，影片則用截圖
                small = shrink(imread(path)) if kind not in ("mp4", "webm") else None
                imwrite(os.path.join(site, thumb_rel), small if small is not None else shot)
        full_rel = f"media/photos/{number}.{kind}"
        if not os.path.exists(os.path.join(site, full_rel)):
            shutil.copyfile(path, os.path.join(site, full_rel))
        item = {"type": "video" if kind in ("mp4", "webm") else "image", "src": thumb_rel, "full": full_rel,
                "source": variant, "number": number}
        if number in photo_lib.unknown:
            item["unknown"] = photo_lib.unknown[number]
        if is_animated(path, kind) or (kind in ("mp4", "webm") and photo):
            item["anim"] = True
        return item

    result, stats = {}, Counter()
    numbers, hidden = {}, []          # 訊息位置 -> 快取編號；橫排中沒露出來的照片
    missed = []                       # 縮圖索引找不到的照片／影片：(位置, 截圖)
    for pos, (msg, slot) in enumerate(zip(chat.messages, slots)):
        if msg.kind not in ("sticker", "photo", "video") or slot is None:
            continue
        image = media_crop(slot)
        if image is None:
            hidden.append(pos)
            continue
        if not image.size:
            continue
        item = None
        if msg.kind == "sticker" and uniques[slot[0]].row.combo:
            # 貼圖拼拼樂：幾張貼圖疊在一起，用截圖才看得到整組
            item = {"type": "sticker", "src": save_crop(f"{msg.idx}_{slot[1]}", image), "source": "combo"}
        elif msg.kind == "sticker" and sticker_lib:
            hit = sticker_lib.lookup(image)
            if hit and hit[2] > CUSTOM_STICKER:
                # 訊息貼圖（自己輸入的文字）、貼圖拼拼樂：快取裡只有底圖，用截圖才看得到全部
                item = {"type": "sticker", "src": save_crop(f"{msg.idx}_{slot[1]}", image), "source": "custom"}
            elif hit:
                entry = sticker_lib.entries[hit[0]]
                src = entry.get("ani") or entry["static"]
                rel = f"media/stickers/{entry['pkg']}_{hit[0]}.png"
                if not os.path.exists(os.path.join(site, rel)):
                    shutil.copyfile(src, os.path.join(site, rel))
                item = {"type": "sticker", "src": rel, "source": "cache"}
        elif msg.kind in ("photo", "video") and photo_lib and chat_dir:
            hit = photo_lib.lookup(image)
            if hit:
                item = photo_item(hit[0], photo=msg.kind == "photo")
                numbers[pos] = int(hit[0])
        if item is None:
            item = {"type": "sticker" if msg.kind == "sticker" else "image",
                    "src": save_crop(f"{msg.idx}_{slot[1]}", image), "source": "screenshot"}
            if msg.kind in ("photo", "video") and photo_lib and chat_dir:
                missed.append((pos, image))
        result.setdefault(msg.idx, []).append(item)

    # 縮圖索引找不到的：先重新列出聊天室快取再找一次，再在前後已對到的編號之間逐一比對
    # （也涵蓋快取中沒有縮圖、只有大圖的訊息）
    added = photo_lib.refresh_chat(chat_dir) if missed else 0
    if missed:
        log(f"    {len(missed)} 張照片在縮圖索引中找不到；重新列出聊天室快取，多了 {added} 個檔案")
    for pos, image in missed:
        hit = photo_lib.lookup(image) if added else None
        if hit:
            number, tried = hit[0], []
        else:
            after = next((numbers[p] for p in range(pos - 1, -1, -1) if p in numbers), None)
            before = next((numbers[p] for p in range(pos + 1, len(slots)) if p in numbers), None)
            number, tried = photo_lib.match_nearby(image, chat_dir, after, before, set(numbers.values()))
        items = result[chat.messages[pos].idx]
        k = next(i for i, it in enumerate(items) if it["source"] == "screenshot")
        if number is None:
            items[k]["tried"] = tried
            continue
        items[k] = photo_item(number, image, photo=chat.messages[pos].kind == "photo")
        numbers[pos] = int(number)
    for msg in chat.messages:
        for it in result.get(msg.idx, []):
            stats[f"{msg.kind}:{it['source']}"] += 1

    # 橫排中沒露出來的照片：依快取編號推斷（同一批送出的照片編號相鄰）
    if hidden and photo_lib and chat_dir:
        ordered = photo_lib.chat_numbers(chat_dir)
        for pos in hidden:
            j = slots[pos][0]
            prev = next((numbers[p] for p in range(pos - 1, -1, -1)
                         if p in numbers and slots[p] and slots[p][0] == j), None)
            later = next((numbers[p] for p in range(pos + 1, len(slots)) if p in numbers and p not in hidden), None)
            number = infer_hidden(ordered, set(numbers.values()), prev, later) if prev is not None else None
            if number is None:
                stats["photo:hidden"] += 1
                continue
            item = photo_item(str(number))
            if item["type"] != "image":
                stats["photo:hidden"] += 1
                continue
            numbers[pos] = number
            item["inferred"] = True
            stats[f"photo:{item['source']}:inferred"] += 1
            result.setdefault(chat.messages[pos].idx, []).append(item)

    # 語音訊息：依快取編號的順序和長度，對到聊天室快取中的語音檔
    voice_pos = [p for p, m in enumerate(chat.messages) if m.kind == "voice"]
    if voice_pos and photo_lib and chat_dir:
        files = photo_lib.voice_numbers(chat_dir)
        if files:
            local = {n: photo_lib.fetch_voice(n) for n in files}
            shown = {p: screen_seconds(uniques[slots[p][0]].row) for p in voice_pos if slots[p]}
            lengths = {n: audio_seconds(path) for n, path in local.items()}
            assigned = assign_voices(voice_pos, numbers, files, {p: s for p, s in shown.items() if s is not None},
                                     lengths)
            os.makedirs(os.path.join(site, "media", "voice"), exist_ok=True)
            for p, n in assigned.items():
                if not os.path.getsize(local[n]):           # 空檔：LINE 沒下載到（過期）
                    stats["voice:empty"] += 1
                    continue
                kind = sniff(local[n])
                stats[f"voice:format:{kind or 'unknown'}"] += 1
                rel = f"media/voice/{n}.{VOICE_EXT.get(kind, 'aac')}"
                if not os.path.exists(os.path.join(site, rel)):
                    shutil.copyfile(local[n], os.path.join(site, rel))
                seconds = shown.get(p) if shown.get(p) is not None else lengths[n]
                item = {"type": "audio", "src": rel, "source": "cache", "number": str(n), "format": kind or "unknown"}
                if seconds is not None:
                    item["seconds"] = round(seconds)
                if not kind:
                    item["head"] = head_hex(local[n])
                result.setdefault(chat.messages[p].idx, []).append(item)
                stats["voice:cache"] += 1
        log(f"    語音訊息：{len(voice_pos)} 則，快取中有 {len(files) if files else 0} 個語音檔，"
            f"對到 {stats['voice:cache']} 則")

    # LINE 表情：只有表情的訊息放大顯示，從截圖裁下每個表情，記住「名稱 → 圖」，
    # 文字訊息裡的 (名稱) 也換成這張圖
    emoji_map, emoji_size = {}, {}
    os.makedirs(os.path.join(site, "media", "emoji"), exist_ok=True)
    for msg, slot in zip(chat.messages, slots):
        if slot is None or msg.kind != "text" or uniques[slot[0]].row.kind != "emoji":
            continue
        row = uniques[slot[0]].row
        names = ["(" + n + ")" for n in EMOJI_RE.findall(msg.body)]
        items = []
        for name, box in zip(names, row.media):
            image = crop(frames.get(uniques[slot[0]].frame), box)
            if not image.size or box[3] - box[1] < (box[2] - box[0]) * 0.8:      # 被裁到一半的不用
                continue
            rel = f"media/emoji/{name_file('', name)}"
            if image.shape[0] > emoji_size.get(name, 0):
                imwrite(os.path.join(site, rel), cut_out(image))
                emoji_map[name], emoji_size[name] = rel, image.shape[0]
            items.append({"type": "emoji", "src": rel, "name": name, "source": "screenshot"})
        if items:
            result.setdefault(msg.idx, []).extend(items)
            stats["emoji"] += 1

    # 回應：從截圖裁下圖示，每種回應存一張
    reactions, reaction_files = {}, {}
    os.makedirs(os.path.join(site, "media", "reactions"), exist_ok=True)
    for msg, (j, _) in zip(chat.messages, pairs):
        if j is None or not uniques[j].row.reactions:
            continue
        shown = []
        for name, box in uniques[j].row.reactions:
            if name not in reaction_files:
                image = crop(frames.get(uniques[j].frame), box)
                if not image.size or abs((box[3] - box[1]) - (box[2] - box[0])) > 4:
                    continue
                reaction_files[name] = f"media/reactions/{name_file('', name)}"
                imwrite(os.path.join(site, reaction_files[name]), image)
            shown.append({"name": name, "src": reaction_files[name]})
        if shown:
            reactions[str(msg.idx)] = shown
            stats["reaction"] += 1

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
            "emoji": emoji_map, "reactions": reactions, "stats": dict(stats)}
    with open(os.path.join(workdir, "media.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    for key, n in sorted(stats.items()):
        log(f"    {key}：{n}")
    return data
