"""用 uiautomator2 操作手機，讓每一步擷取從約 6 秒降到 2 秒左右。

內建的 `uiautomator dump` 每次都要重新啟動，一次 3～4 秒；uiautomator2 在手機上常駐一個
服務，dump 和截圖都快得多。滑動也改成「拖曳到底、停住、再放開」，放開時速度為零，
畫面不會再慣性滑動，不必等它停下。

第一次連線時，uiautomator2 會在手機上安裝它的服務 App（ATX）。擷取結束後會停止服務，
以免和內建的 uiautomator（實機測試腳本用的）互搶。
"""
import os
import time

import cv2

from .device import Device, DeviceError


class U2Device(Device):
    fast = True

    def __init__(self, serial=None):
        try:
            import uiautomator2 as u2
        except ImportError:
            raise DeviceError("沒有安裝 uiautomator2：請執行 pip install uiautomator2")
        try:
            self.d = u2.connect(serial or os.environ.get("ANDROID_SERIAL"))
            self.d.info                                   # 確認服務已啟動
        except Exception as e:
            raise DeviceError(f"uiautomator2 連線失敗：{e}")

    def screencap(self):
        try:
            img = self.d.screenshot(format="opencv")
        except Exception as e:
            raise DeviceError(f"截圖失敗：{e}")
        if img is None:
            raise DeviceError("截圖失敗")
        return cv2.imencode(".png", img)[1].tobytes(), img

    def dump(self, retries=3):
        last = None
        for _ in range(retries):
            try:
                xml = self.d.dump_hierarchy(compressed=False)
                if xml and "<hierarchy" in xml:
                    return xml.encode("utf-8")
            except Exception as e:
                last = e
            time.sleep(0.3)
        raise DeviceError(f"dump 失敗：{last}")

    def drag(self, x, y1, y2, steps=8, hold=0.12):
        """由 y1 拖到 y2 後停住再放開。第一小段只用來越過觸控誤差範圍，不會造成捲動。"""
        touch = self.d.touch
        touch.down(x, y1)
        slop = 40 if y2 > y1 else -40
        touch.move(x, y1 + slop)
        for k in range(1, steps + 1):
            touch.move(x, y1 + slop + (y2 - y1) * k // steps)
            time.sleep(0.012)
        time.sleep(hold)
        touch.up(x, y2 + slop)

    def swipe(self, x, y1, y2, duration_ms):
        self.d.swipe(x, y1, x, y2, duration=duration_ms / 1000)

    def wake(self):
        try:
            self.d.screen_on()
        except Exception:
            super().wake()

    def close(self):
        stop = getattr(self.d, "stop_uiautomator", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass


def connect(backend="auto", log=print):
    """backend：auto（有裝 uiautomator2 就用，否則用 adb）、u2、adb。"""
    if backend in ("auto", "u2"):
        try:
            device = U2Device()
            log("使用 uiautomator2（快速模式）")
            return device
        except DeviceError as e:
            if backend == "u2":
                raise
            log(f"改用 adb（每步約 6 秒）：{e}")
    return Device()
