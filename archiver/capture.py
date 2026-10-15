"""模組 2：從最新訊息往上捲，每一步截圖＋dump，存到 <工作目錄>/capture/。

- 每一步：滑動 → 等畫面停穩 → 截圖 → dump → 估計和上一步之間捲了多少。
- 快速模式（uiautomator2）每步拖曳的距離依畫面而定：盡量拖遠，但最上面被切掉的那一列，
  拖完後下緣還要留在畫面內，下一步它就會完整出現。所以每一列都至少完整出現在一張截圖裡，
  一般情況每步可捲聊天區的四分之三。
- 位移估計用截圖的重疊區（實機測試 5）；比對分數太低時改用 dump 中同一則訊息的位置差。
- 捲不動時換個位置再拖（有些列會攔住觸控），並逐次拉長等待（可能正在載入更舊的訊息）。
  連續 stop_after 步沒有位移，而且畫面上出現 txt 最前面的訊息，才判定到頂；
  一直捲不動、最舊的訊息卻還沒出現時，停下來但不標成完成。
- 中斷後再執行會接續：先確認目前畫面和最後一步的截圖有重疊，再繼續往上捲。
  已標成完成的也會再試著往上捲，確認真的到頂。
"""
import json
import os
import statistics
import time
from collections import Counter
from contextlib import contextmanager

import cv2

from .align import text_similarity
from .imgio import imread
from .offset import LOW_SCORE, find_offset, wait_still
from .screen import parse_dump

SLOW_DRAG = 0.5          # adb 模式的滑動有慣性，距離不固定，維持每步半個聊天區
DRAG_X = (0.5, 0.12, 0.88)   # 捲不動時依序換到這些水平位置（畫面寬度的比例）再拖
MAX_WAIT = 10.0          # 捲不動時，每次最多等幾秒


class CaptureError(RuntimeError):
    pass


def oldest_texts(messages, count=15):
    """txt 最前面 count 則訊息中，整段對話只出現一次的文字訊息；用來確認真的捲到頂了
    （「好」「OK」之類到處都有，不能當證據）。"""
    seen = Counter(m.body.strip() for m in messages if m.kind == "text")
    return [m.body for m in messages[:count] if m.kind == "text" and seen[m.body.strip()] == 1]


def dump_offset(prev, cur):
    """用前後兩次 dump 中都只出現一次的列，估計位移。找不到或互相矛盾時回傳 None。

    兩邊都完整的列直接比上緣。捲得遠、重疊區小時，常常沒有一列在兩邊都完整，所以有文字的列
    就算被切掉一邊，也比兩邊都沒被切到的那一緣（以文字辨認，因為被切掉時可能看不到時間）。
    只靠一列時，那一列要夠有辨識度：兩邊都完整，或文字至少 6 個字。
    """
    def entries(screen):
        top, bottom = screen.list_bounds[1], screen.list_bounds[3]
        seen = {}
        for r in screen.rows:
            if r.kind == "unknown":
                continue
            cut_top, cut_bottom = r.bounds[1] <= top + 1, r.bounds[3] >= bottom - 1
            if not cut_top and not cut_bottom:
                seen.setdefault(("whole",) + r.signature(), []).append((r, False, False))
            if r.text.strip():
                seen.setdefault(("text", r.kind, r.side, r.text[:60]), []).append((r, cut_top, cut_bottom))
        return {k: v[0] for k, v in seen.items() if len(v) == 1}

    a, b = entries(prev), entries(cur)
    found = []                                   # (位移, 是否夠有辨識度)
    for key in a.keys() & b.keys():
        (ra, top_a, bottom_a), (rb, top_b, bottom_b) = a[key], b[key]
        strong = key[0] == "whole" or len(rb.text.strip()) >= 6
        if not top_a and not top_b:
            found.append((rb.bounds[1] - ra.bounds[1], strong))
        elif not bottom_a and not bottom_b:
            found.append((rb.bounds[3] - ra.bounds[3], strong))
    if not found:
        return None
    middle = statistics.median(d for d, _ in found)
    agree = [(d, strong) for d, strong in found if abs(d - middle) <= 6]
    if len(agree) * 2 <= len(found) or (len(agree) == 1 and not agree[0][1]):
        return None
    return int(statistics.median(d for d, _ in agree))


class Capture:
    def __init__(self, device, workdir, drag=0.75, duration_ms=1000, stop_after=3, max_steps=5000,
                 idle_wait=2.0, oldest=(), give_up=8, log=print):
        self.device = device
        self.dir = os.path.join(workdir, "capture")
        self.drag = drag                    # 快速模式每步最多拖曳聊天區的多少比例
        self.duration_ms = duration_ms
        self.stop_after = stop_after
        self.oldest = [t for t in oldest if t.strip()]   # txt 最前面幾則文字訊息，用來確認真的到頂
        self.give_up = give_up              # 最舊的訊息還沒出現時，最多連續幾步捲不動就先停下
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
        """讀 steps.jsonl。中斷時可能留下寫到一半、或少了換行而和下一筆黏在一起的紀錄，
        都要讀得過去；同一步有兩筆時取後寫的那筆。"""
        if not os.path.exists(self.steps_file):
            return []
        with open(self.steps_file, encoding="utf-8") as f:
            text = f.read()
        decoder, by_step, pos, skipped = json.JSONDecoder(), {}, 0, []
        while True:
            pos = text.find("{", pos)
            if pos < 0:
                break
            try:
                record, end = decoder.raw_decode(text, pos)
            except json.JSONDecodeError:
                end = text.find("{", pos + 1)
                skipped.append(text[pos:end if end >= 0 else len(text)].strip()[:80])
                if end < 0:
                    break
                pos = end
                continue
            if isinstance(record, dict) and isinstance(record.get("step"), int):
                by_step[record["step"]] = record
            pos = end
        for s in skipped:
            self.log(f"steps.jsonl 有一筆不完整的紀錄，已略過：{s!r}")
        return [by_step[k] for k in sorted(by_step)]

    def _save(self, step, image, xml, record):
        with open(self.path(step, "jpg" if image[:2] == b"\xff\xd8" else "png"), "wb") as f:
            f.write(image)
        with open(self.path(step, "xml"), "wb") as f:
            f.write(xml)
        line = json.dumps({"step": step, **record}, ensure_ascii=False) + "\n"
        with open(self.steps_file, "a+b") as f:
            if f.tell():                        # 上一筆少了換行（中斷時寫到一半）：先補上
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    line = "\n" + line
            f.write(line.encode("utf-8"))

    def done(self):
        steps = self.load_steps()
        return bool(steps) and bool(steps[-1].get("done"))

    def at_oldest(self, screen):
        """畫面上有 txt 最前面的訊息嗎？沒有 txt 可以對照時一律當作是。"""
        if not self.oldest:
            return True
        texts = [r.text for r in screen.rows if r.kind == "text" and r.text]
        return any(text_similarity(t, r) >= 0.9 for t in self.oldest for r in texts)

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
            if found[0] is not None and (found[2] != "image" or dy_dump is None or abs(found[0] - dy_dump) <= 6):
                return png, img, xml, screen, found
            time.sleep(0.3)                    # 位移判定不了或兩者不一致：畫面可能還在變（載入圖片等），重拍
        if found[0] is None or dy_dump is None:
            return png, img, xml, screen, found
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
        """擷取到最舊的訊息或 max_steps 為止。同一個工作目錄同時只能有一個擷取在跑。"""
        lock = os.path.join(self.dir, "capture.lock")
        other = _lock_owner(lock)
        if other:
            raise CaptureError(f"另一個擷取程式（PID {other}）正在使用 {self.dir}。請先結束它（Ctrl+C），再執行一次。")
        with open(lock, "w") as f:
            f.write(str(os.getpid()))
        try:
            return self._run()
        finally:
            if os.path.exists(lock):
                os.remove(lock)

    def _run(self):
        steps = self.load_steps()
        if steps and steps[-1].get("done"):
            self.log("上次已判定捲到最舊的訊息；再試著往上捲，確認真的到頂。要整個重新擷取，請刪除 capture 目錄。")
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
        streak = 0                          # 連續捲不動的步數
        prev_img, prev_screen = img, screen
        t0, done_here = time.time(), 0
        while step < self.max_steps:
            distance = self._distance(prev_screen, roi) if fast else int((bottom - top) * min(self.drag, SLOW_DRAG))
            y1 = top + (bottom - top - distance) // 2
            x = round(img.shape[1] * DRAG_X[streak % len(DRAG_X)] * scale_x)
            ya, yb = round(y1 * scale_y), round((y1 + distance) * scale_y)
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
            finished = streak >= self.stop_after and self.at_oldest(screen)
            stuck = not finished and streak >= max(self.give_up, self.stop_after)
            with self._timed("存檔"):
                self._save(step, png, xml, {"dy": dy, "score": round(score, 3), "method": method, "done": finished})
            per_step = (time.time() - t0) / done_here
            self.log(f"第 {step} 步：位移 {dy if dy is not None else '?'}px（{method}），"
                     f"畫面上 {len(screen.rows)} 列，平均每步 {per_step:.1f} 秒")
            if done_here % 20 == 0:
                self.log("    " + self.timing_summary(done_here))
                status = getattr(self.device, "status", None)
                if callable(status):
                    self.log("    " + status())
            if dy is None:
                self.log("    警告：這一步的位移無法判定，可能漏掉中間的訊息。")
            if finished:
                self.log(f"連續 {streak} 步沒有移動，畫面上已是最舊的訊息。擷取完成。")
                break
            if stuck:
                self.log(f"連續 {streak} 步捲不動，但 txt 最前面的訊息（例如「{self.oldest[0][:20]}」）還沒出現。"
                         "請看一下手機畫面（是否跳出視窗、正在載入），處理好、不要捲動聊天室，再執行同一個指令接續。")
                break
            if dy == 0:
                # 可能正在載入更舊的訊息：逐次多等一點；下一次也換個位置拖（這裡的列可能攔住了觸控）
                wait = min(self.idle_wait * 2 ** (streak - 1), MAX_WAIT)
                self.log(f"    沒有移動：等 {wait:.0f} 秒後換個位置再拖")
                time.sleep(wait)
            prev_img, prev_screen = img, screen
        if done_here:
            self.log(self.timing_summary(done_here))
            if self.overshoot:
                self.log(f"捲過頭 {self.overshoot} 次（見上方警告）。")
        status = getattr(self.device, "status", None)
        if callable(status):
            self.log(status())
        return self.load_steps()

    def timing_summary(self, steps):
        parts = "、".join(f"{k} {v / steps:.2f}" for k, v in self.timing.items())
        return f"每步平均秒數：{parts}"


def _lock_owner(lock):
    """鎖檔記錄的程式還在跑的話回傳它的 PID，否則回傳 None（舊的鎖檔視為已失效）。"""
    try:
        with open(lock) as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return None
    if not pid or pid == os.getpid() or os.name != "posix":    # Windows 上無法安全地檢查
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def image_path(capture_dir, step):
    """某一步的截圖路徑：.jpg（uiautomator2）或 .png（adb）。"""
    jpg = os.path.join(capture_dir, f"{step:05d}.jpg")
    return jpg if os.path.exists(jpg) else os.path.join(capture_dir, f"{step:05d}.png")
