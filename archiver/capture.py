"""模組 2：從最新訊息往上捲，每一步截圖＋dump，存到 <工作目錄>/capture/。

- 每一步：滑動 → 等畫面停穩 → 截圖 → dump → 估計和上一步之間捲了多少。
- 位移估計用截圖的重疊區（實機測試 5）；比對分數太低時改用 dump 中同一則訊息的位置差。
- 連續 stop_after 步沒有位移就判定到頂。
- 中斷後再執行會接續：先確認目前畫面和最後一步的截圖有重疊，再繼續往上捲。
"""
import json
import os
import statistics
import time

import cv2

from .offset import LOW_SCORE, find_offset, wait_still
from .screen import parse_dump


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
    def __init__(self, device, workdir, drag=0.4, duration_ms=1000, stop_after=3, max_steps=5000,
                 idle_wait=2.0, log=print):
        self.device = device
        self.dir = os.path.join(workdir, "capture")
        self.drag = drag
        self.duration_ms = duration_ms
        self.stop_after = stop_after
        self.max_steps = max_steps
        self.idle_wait = idle_wait
        self.log = log
        os.makedirs(self.dir, exist_ok=True)

    # ---- 存取 ----
    def path(self, step, ext):
        return os.path.join(self.dir, f"{step:05d}.{ext}")

    @property
    def steps_file(self):
        return os.path.join(self.dir, "steps.jsonl")

    def load_steps(self):
        if not os.path.exists(self.steps_file):
            return []
        with open(self.steps_file, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _save(self, step, png, xml, record):
        with open(self.path(step, "png"), "wb") as f:
            f.write(png)
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

    def _roi(self, screen):
        top, bottom = screen.list_bounds[1], screen.list_bounds[3]
        for o in screen.overlays:            # 釘在頂端的公告列
            if o[1] <= top + 50 and o[3] < (top + bottom) / 2:
                top = max(top, o[3])
        _, _, dpi = self.device.screen_size()
        return top, bottom, round(48 * dpi / 160)   # 再避開頂端 48dp 的浮動日期條

    def _offset(self, prev_img, prev_screen, img, screen, roi):
        dy, score = find_offset(prev_img, img, roi)
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
            prev_img = cv2.imread(self.path(step, "png"))
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
        drag = int((bottom - top) * self.drag)
        y1 = top + (bottom - top - drag) // 2
        scale_x, scale_y = width / img.shape[1], height / img.shape[0]
        streak = 0
        prev_img, prev_screen = img, screen
        t0, done_here = time.time(), 0
        while step < self.max_steps:
            self.device.swipe(round(img.shape[1] / 2 * scale_x), round(y1 * scale_y),
                              round((y1 + drag) * scale_y), self.duration_ms)
            png, img, xml, screen = self._grab(roi)
            dy, score, method = self._offset(prev_img, prev_screen, img, screen, roi)
            step += 1
            done_here += 1
            streak = streak + 1 if dy == 0 else 0
            finished = streak >= self.stop_after
            self._save(step, png, xml, {"dy": dy, "score": round(score, 3), "method": method, "done": finished})
            per_step = (time.time() - t0) / done_here
            self.log(f"第 {step} 步：位移 {dy if dy is not None else '?'}px（{method}），"
                     f"畫面上 {len(screen.rows)} 列，平均每步 {per_step:.1f} 秒")
            if dy is None:
                self.log("    警告：這一步的位移無法判定，可能漏掉中間的訊息。")
            if finished:
                self.log(f"連續 {streak} 步沒有移動，已到最舊的訊息。擷取完成。")
                break
            if dy == 0:
                time.sleep(self.idle_wait)     # 可能正在載入更舊的訊息，多等一下
            prev_img, prev_screen = img, screen
        return self.load_steps()
