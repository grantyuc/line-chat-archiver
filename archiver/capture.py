"""模組 2：從最新訊息往上捲，每一步截圖＋dump，存到 <工作目錄>/capture/。

- 每一步：滑動 → 等畫面停穩 → 截圖 → dump → 估計和上一步之間捲了多少。
- 快速模式（uiautomator2）每步拖曳的距離依畫面而定：盡量拖遠，但最上面被切掉的那一列，
  拖完後下緣還要留在畫面內，下一步它就會完整出現。所以每一列都至少完整出現在一張截圖裡，
  一般情況每步可捲聊天區的四分之三。
- 位移估計用截圖的重疊區（實機測試 5）；比對分數太低時改用 dump 中同一則訊息的位置差。
- 連續 stop_after 步沒有位移就判定到頂。
- 中斷後再執行會接續：先確認目前畫面和最後一步的截圖有重疊，再繼續往上捲。
"""
import json
import os
import statistics
import time
from collections import Counter
from contextlib import contextmanager

import cv2

from .imgio import imread
from .offset import LOW_SCORE, find_offset, wait_still
from .screen import parse_dump

SLOW_DRAG = 0.5          # adb 模式的滑動有慣性，距離不固定，維持每步半個聊天區


class CaptureError(RuntimeError):
    pass


def dump_offset(prev, cur):
    """用前後兩次 dump 中都只出現一次的完整訊息列，估計位移。找不到時回傳 None。"""
    def unique(screen):
        seen = {}
        for r in screen.rows:
            if not r.partial and r.kind != "unknown":
                seen.setdefault(r.signature(), []).append(r)
        return {sig: rows[0] for sig, rows in seen.items() if len(rows) == 1}

    a, b = unique(prev), unique(cur)
    diffs = [b[s].bounds[1] - a[s].bounds[1] for s in a.keys() & b.keys()]
    return int(statistics.median(diffs)) if diffs else None


class Capture:
    def __init__(self, device, workdir, drag=0.75, duration_ms=1000, stop_after=3, max_steps=5000,
                 idle_wait=2.0, log=print):
        self.device = device
        self.dir = os.path.join(workdir, "capture")
        self.drag = drag                    # 快速模式每步最多拖曳聊天區的多少比例
        self.duration_ms = duration_ms
        self.stop_after = stop_after
        self.max_steps = max_steps
        self.idle_wait = idle_wait
        self.log = log
        self.timing = Counter()             # 各階段累計秒數
        self.overshoot = 0                  # 捲得比預期遠的步數
        os.makedirs(self.dir, exist_ok=True)

    @contextmanager
    def _timed(self, name):
        t = time.time()
        try:
            yield
        finally:
            self.timing[name] += time.time() - t

    # ---- 存取 ----
    def path(self, step, ext):
        return os.path.join(self.dir, f"{step:05d}.{ext}")

    def image_path(self, step):
        """這一步的截圖（uiautomator2 存 JPEG 原檔，adb 存 PNG）。"""
        return image_path(self.dir, step)

    @property
    def steps_file(self):
        return os.path.join(self.dir, "steps.jsonl")

    def load_steps(self):
        if not os.path.exists(self.steps_file):
            return []
        with open(self.steps_file, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _save(self, step, image, xml, record):
        with open(self.path(step, "jpg" if image[:2] == b"\xff\xd8" else "png"), "wb") as f:
            f.write(image)
        with open(self.path(step, "xml"), "wb") as f:
            f.write(xml)
        with open(self.steps_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"step": step, **record}, ensure_ascii=False) + "\n")

    def done(self):
        return any(s.get("done") for s in self.load_steps())

    # ---- 手機 ----
    def _grab(self, roi=None):
        self.device.wake()
        if roi:
            png, img = wait_still(self.device, roi)
        else:
            time.sleep(1)
            png, img = self.device.screencap()
        xml = self.device.dump()
        screen = parse_dump(xml)
        if not screen.has_line or screen.list_bounds[3] <= screen.list_bounds[1]:
            raise CaptureError("畫面上沒有 LINE 聊天室：請解鎖手機、打開要封存的聊天室後再執行，會從中斷處接續。")
        return png, img, xml, screen

    def _grab_fast(self, roi, prev_img, prev_screen, expect, tries=3):
        """快速模式（拖曳後停住再放開，畫面不會慣性滑動）：dump → 截圖，各一次。

        用「截圖算出的位移」和「dump 中同一則訊息的位置差」互相核對；不一致代表兩者
        拍到的不是同一個畫面（例如還在動），就重拍。回傳的最後一項是 (位移, 分數, 方法)。
        """
        for _ in range(tries):
            with self._timed("dump"):
                xml = self.device.dump()
            with self._timed("截圖"):
                png, img = self.device.screencap()
            with self._timed("比對"):
                screen = parse_dump(xml)
                if not screen.has_line or screen.list_bounds[3] <= screen.list_bounds[1]:
                    raise CaptureError("畫面上沒有 LINE 聊天室：請解鎖手機、打開要封存的聊天室後再執行，會從中斷處接續。")
                found = self._offset(prev_img, prev_screen, img, screen, roi, expect)
                dy_dump = dump_offset(prev_screen, screen)
            if found[2] != "image" or dy_dump is None or abs(found[0] - dy_dump) <= 6:
                return png, img, xml, screen, found
            time.sleep(0.2)
        # 畫面已經停了，兩者仍不一致：以 dump 中訊息的實際位置為準
        return png, img, xml, screen, (dy_dump, found[1], "dump")

    def _distance(self, screen, roi):
        """快速模式這一步要捲多遠（截圖像素）。

        最上面被切掉、或頂端在浮動日期條底下的列，拖完後下緣要留在畫面內（再留一點餘裕），
        下一步它就會完整出現。這一列比畫面還高、怎樣都無法完整出現時，直接捲最遠。
        """
        top, bottom, buffer = roi
        view = bottom - top
        most = int(view * self.drag)
        cut = [r.bounds[3] for r in screen.rows if r.bounds[1] < top + buffer and r.bounds[3] > top]
        if not cut:
            return most
        room = bottom - buffer - max(cut)
        return most if room < view // 10 else min(most, room)

    def _roi(self, screen):
        top, bottom = screen.list_bounds[1], screen.list_bounds[3]
        for o in screen.overlays:            # 釘在頂端的公告列
            if o[1] <= top + 50 and o[3] < (top + bottom) / 2:
                top = max(top, o[3])
        _, _, dpi = self.device.screen_size()
        return top, bottom, round(48 * dpi / 160)   # 再避開頂端 48dp 的浮動日期條

    def _offset(self, prev_img, prev_screen, img, screen, roi, expect=None):
        dy, score = find_offset(prev_img, img, roi, expect)
        if dy is not None and score >= LOW_SCORE:
            return dy, score, "image"
        dy_dump = dump_offset(prev_screen, screen)
        if dy_dump is not None:
            return dy_dump, score, "dump"
        top, bottom, _ = roi
        if float(cv2.absdiff(prev_img[top:bottom], img[top:bottom]).mean()) < 0.5:
            return 0, score, "same"
        return None, score, "unknown"

    # ---- 主流程 ----
    def run(self):
        steps = self.load_steps()
        if any(s.get("done") for s in steps):
            self.log("這個聊天室已經擷取完成。要重新擷取，請刪除 capture 目錄。")
            return steps
        width, height, _ = self.device.screen_size()

        if steps:
            step = steps[-1]["step"]
            prev_img = imread(self.image_path(step))
            with open(self.path(step, "xml"), "rb") as f:
                prev_screen = parse_dump(f.read())
            roi = self._roi(prev_screen)
            png, img, xml, screen = self._grab(roi)
            dy, score, method = self._offset(prev_img, prev_screen, img, screen, roi)
            if dy is None:
                raise CaptureError(
                    f"手機畫面和上次停下的位置對不上。請把聊天室捲到和 {self.path(step, 'png')} 差不多的位置再執行。")
            step += 1
            self._save(step, png, xml, {"dy": dy, "score": round(score, 3), "method": "resume:" + method})
            self.log(f"從第 {step} 步接續（位移 {dy}px）")
        else:
            png, img, xml, screen = self._grab()
            roi = self._roi(screen)
            step = 0
            self._save(step, png, xml, {"dy": 0, "score": 1.0, "method": "start"})
            self.log("開始擷取：請不要碰手機。")

        top, bottom, _ = roi
        fast = getattr(self.device, "fast", False)
        scale_x, scale_y = width / img.shape[1], height / img.shape[0]
        streak = 0
        prev_img, prev_screen = img, screen
        t0, done_here = time.time(), 0
        while step < self.max_steps:
            distance = self._distance(prev_screen, roi) if fast else int((bottom - top) * min(self.drag, SLOW_DRAG))
            y1 = top + (bottom - top - distance) // 2
            x, ya, yb = round(img.shape[1] / 2 * scale_x), round(y1 * scale_y), round((y1 + distance) * scale_y)
            if fast:
                with self._timed("拖曳"):
                    self.device.drag(x, ya, yb)
                png, img, xml, screen, (dy, score, method) = self._grab_fast(roi, prev_img, prev_screen, distance)
                if dy is not None and dy > distance + roi[2]:
                    self.overshoot += 1
                    self.log(f"    警告：捲了 {dy}px，比預期的 {distance}px 遠，較高的列可能沒有完整出現。")
            else:
                with self._timed("拖曳"):
                    self.device.swipe(x, ya, yb, self.duration_ms)
                with self._timed("截圖"):
                    png, img, xml, screen = self._grab(roi)
                with self._timed("比對"):
                    dy, score, method = self._offset(prev_img, prev_screen, img, screen, roi)
            step += 1
            done_here += 1
            streak = streak + 1 if dy == 0 else 0
            finished = streak >= self.stop_after
            with self._timed("存檔"):
                self._save(step, png, xml, {"dy": dy, "score": round(score, 3), "method": method, "done": finished})
            per_step = (time.time() - t0) / done_here
            self.log(f"第 {step} 步：位移 {dy if dy is not None else '?'}px（{method}），"
                     f"畫面上 {len(screen.rows)} 列，平均每步 {per_step:.1f} 秒")
            if done_here % 20 == 0:
                self.log("    " + self.timing_summary(done_here))
            if dy is None:
                self.log("    警告：這一步的位移無法判定，可能漏掉中間的訊息。")
            if finished:
                self.log(f"連續 {streak} 步沒有移動，已到最舊的訊息。擷取完成。")
                break
            if dy == 0:
                time.sleep(self.idle_wait)     # 可能正在載入更舊的訊息，多等一下
            prev_img, prev_screen = img, screen
        if done_here:
            self.log(self.timing_summary(done_here))
            if self.overshoot:
                self.log(f"捲過頭 {self.overshoot} 次（見上方警告）。")
        return self.load_steps()

    def timing_summary(self, steps):
        parts = "、".join(f"{k} {v / steps:.2f}" for k, v in self.timing.items())
        return f"每步平均秒數：{parts}"


def image_path(capture_dir, step):
    """某一步的截圖路徑：.jpg（uiautomator2）或 .png（adb）。"""
    jpg = os.path.join(capture_dir, f"{step:05d}.jpg")
    return jpg if os.path.exists(jpg) else os.path.join(capture_dir, f"{step:05d}.png")
