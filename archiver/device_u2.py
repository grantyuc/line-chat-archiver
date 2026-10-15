"""用 uiautomator2 操作手機，讓每一步擷取從約 6 秒降到 1 秒左右。

內建的 `uiautomator dump` 每次都要重新啟動，一次 3～4 秒；uiautomator2 在手機上常駐一個
服務，dump 和截圖都快得多。滑動也改成「拖曳到底、停住、再放開」，放開時速度為零，
畫面不會再慣性滑動，不必等它停下。截圖直接存服務傳回的 JPEG，不再轉成 PNG
（省下編碼時間，檔案也小很多）。

uiautomator2 3.x 每個請求都重新經 adb 建立一次連線，實測約 60 毫秒；一次拖曳要送好幾個觸控
事件，累積起來很可觀。所以這裡自己和服務維持連線（HTTP keep-alive），出錯時才改回
uiautomator2 原本的方式。另開第二條連線，讓「放開、截圖」和 dump 同時進行。

第一次連線時，uiautomator2 會在手機上安裝它的服務 App（ATX）。擷取結束後會停止服務，
以免和內建的 uiautomator（實機測試腳本用的）互搶。
"""
import base64
import http.client
import json
import os
import threading
import time
from collections import Counter

import cv2
import numpy as np

from .device import Device, DeviceError


class AdbConnection(http.client.HTTPConnection):
    """經 adb 連到手機上某個 TCP 埠的 HTTP 連線；連線斷了，下一個請求會自動重連。"""

    def __init__(self, adb_device, port, timeout=30):
        super().__init__("127.0.0.1", port, timeout=timeout)
        self.adb_device, self.remote_port = adb_device, port
        self.opened = 0

    def connect(self):
        import adbutils
        self.sock = self.adb_device.create_connection(adbutils.Network.TCP, self.remote_port)
        self.sock.settimeout(self.timeout)
        self.opened += 1


class Rpc:
    """uiautomator2 服務的 JSON-RPC，重複使用同一條連線。"""

    HEADERS = {"Content-Type": "application/json", "Accept-Encoding": ""}

    def __init__(self, conn):
        self.conn, self.id = conn, 0
        self.seconds = Counter()                        # 各方法累計秒數
        self.count = Counter()

    def call(self, method, *params):
        self.id += 1                                    # 也是請求次數
        body = json.dumps({"jsonrpc": "2.0", "id": self.id, "method": method, "params": list(params)})
        t = time.time()
        for attempt in (0, 1):
            try:
                self.conn.request("POST", "/jsonrpc/0", body, self.HEADERS)
                resp = self.conn.getresponse()
                data = json.loads(resp.read())
                break
            except (OSError, http.client.HTTPException, ValueError):
                self.conn.close()                       # 服務關掉了閒置的連線：重連再送一次
                if attempt:
                    raise
        self.seconds[method] += time.time() - t
        self.count[method] += 1
        if "error" in data:
            raise DeviceError(f"{method} 失敗：{str(data['error'])[:300]}")
        return data.get("result")


def decode_shot(data):
    """takeScreenshot 傳回的 base64 JPEG → (JPEG 位元組, 影像)；沒有內容或解不開時回傳 None。"""
    raw = base64.b64decode(data) if data else b""
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) if raw else None
    return (raw, img) if img is not None else None


class U2Device(Device):
    fast = True
    DOWN, UP, MOVE = 0, 1, 2            # MotionEvent 的動作
    drag_moves = 3                      # 越過觸控誤差之後分幾段拖完（見 drag）

    def __init__(self, serial=None, reuse=True):
        try:
            import uiautomator2 as u2
        except ImportError:
            raise DeviceError("沒有安裝 uiautomator2：請執行 pip install uiautomator2")
        try:
            self.d = u2.connect(serial or os.environ.get("ANDROID_SERIAL"))
            self.d.info                                   # 確認服務已啟動
        except Exception as e:
            raise DeviceError(f"uiautomator2 連線失敗：{e}")
        self.rpc, self.helper, self.rpc_error = None, None, "已用 --new-connections 關閉連線重複使用"
        if not reuse:
            return
        try:
            self.rpc = Rpc(AdbConnection(self.d._dev, self.d._device_server_port))
            self.rpc.call("deviceInfo")
            self.helper = Rpc(AdbConnection(self.d._dev, self.d._device_server_port))
            self.helper.call("deviceInfo")
        except Exception as e:                            # 版本不同或連不上：用 uiautomator2 原本的方式
            self.rpc, self.helper, self.rpc_error = None, None, f"{type(e).__name__}: {e}"

    def _call(self, method, *params):
        if self.rpc is not None:
            try:
                return self.rpc.call(method, *params)
            except Exception as e:                        # 改用 uiautomator2 原本的方式（它會自動重啟服務）
                self.rpc, self.rpc_error = None, f"{type(e).__name__}: {e}"
        return getattr(self.d.jsonrpc, method)(*params)

    def status(self):
        """連線方式和各請求平均花多久，用來確認連線有沒有重複使用、時間花在哪。"""
        if self.rpc is None:
            return f"uiautomator2 連線：每個請求各開一條（{self.rpc_error[:200]}）"
        rpcs = [r for r in (self.rpc, self.helper) if r is not None]
        seconds, count = Counter(), Counter()
        for r in rpcs:
            seconds.update(r.seconds)
            count.update(r.count)
        each = "、".join(f"{m} {seconds[m] / count[m] * 1000:.0f}ms" for m in count)
        return (f"uiautomator2 連線：{sum(r.id for r in rpcs)} 個請求用了 {sum(r.conn.opened for r in rpcs)} 條連線"
                f"（{'放開、截圖和 dump 同時進行' if self.helper else '依序進行'}）；平均 {each}")

    def screencap(self):
        """回傳 (JPEG 位元組, 影像)。"""
        try:
            shot = decode_shot(self._call("takeScreenshot", 1, 80))
            if shot is None:                              # 服務沒有傳回截圖時，改用一般介面
                img = self.d.screenshot(format="opencv")
                shot = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes(), img
        except Exception as e:
            raise DeviceError(f"截圖失敗：{e}")
        if shot[1] is None:
            raise DeviceError("截圖失敗")
        return shot

    def dump(self, retries=3):
        last, xml = None, None
        for _ in range(retries):
            try:
                xml = self._call("dumpWindowHierarchy", False, 50)
                if xml and "<node" in xml:
                    return xml.encode("utf-8")
            except Exception as e:
                last = e
            time.sleep(0.3)
        if xml and "<hierarchy" in xml:
            return xml.encode("utf-8")                    # 空的畫面：交給呼叫端判斷
        raise DeviceError(f"dump 失敗：{last}")

    def drag(self, x, y1, y2, steps=None, hold=0.12):
        """由 y1 拖到 y2 後停住再放開（放開時速度為零，不會慣性滑動）。

        先小移 40px：手指按在訊息上時，聊天列表要等移動超過觸控誤差才接手，而接手的那一下
        移動不會捲動。所以先用小移讓列表接手，之後的移動才全部拿來捲。不先小移、一次跳到終點，
        實機上就捲不動。之後分 3 段拖完（實機驗證過；只拖 1 段反而較慢，也較常捲不動）。
        """
        end = self._press_and_move(x, y1, y2, steps)
        time.sleep(hold)
        self._call("injectInputEvent", self.UP, x, end, 0)

    def _press_and_move(self, x, y1, y2, steps=None):
        """按下並拖到終點（還沒放開）。回傳手指最後的 y。"""
        steps = steps or self.drag_moves
        slop = 40 if y2 > y1 else -40
        self._call("injectInputEvent", self.DOWN, x, y1, 0)
        self._call("injectInputEvent", self.MOVE, x, y1 + slop, 0)
        for k in range(1, steps + 1):
            self._call("injectInputEvent", self.MOVE, x, y1 + slop + (y2 - y1) * k // steps, 0)
        return y2 + slop

    def drag_and_grab(self, x, y1, y2, steps=None, hold=0.12, settle=0.15):
        """拖曳並取得畫面，回傳 (dump, (JPEG, 影像))。

        最後一段移動之後畫面就不會再動（停住再放開，沒有慣性），所以 dump 立刻開始；同時用第二條
        連線停住、放開、再等一下讓新出現的圖片載入，然後截圖。dump 約 0.5 秒，這些都藏在它裡面。
        沒有第二條連線、或第二條連線出錯時，依序做。
        """
        end = self._press_and_move(x, y1, y2, steps)
        helper = self.helper
        if helper is None:
            time.sleep(hold)
            self._call("injectInputEvent", self.UP, x, end, 0)
            return self.dump(), self.screencap()
        done = {}

        def release_and_shoot():
            try:
                time.sleep(hold)
                helper.call("injectInputEvent", self.UP, x, end, 0)
                done["up"] = True
                time.sleep(settle)
                done["shot"] = decode_shot(helper.call("takeScreenshot", 1, 80))
            except Exception as e:
                done["error"] = e

        worker = threading.Thread(target=release_and_shoot, daemon=True)
        worker.start()
        try:
            xml = self.dump()
        finally:
            worker.join()
            if "up" not in done:                          # 放開沒送出：用主連線補送，免得手指一直按著
                self._call("injectInputEvent", self.UP, x, end, 0)
        if "error" in done:
            self.helper = None                            # 之後改成依序做
        return xml, done.get("shot") or self.screencap()

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


def connect(backend="auto", reuse=True, log=print):
    """backend：auto（有裝 uiautomator2 就用，否則用 adb）、u2、adb。
    reuse=False 時每個請求各開一條連線（uiautomator2 原本的方式）。"""
    if backend in ("auto", "u2"):
        try:
            device = U2Device(reuse=reuse)
            log("使用 uiautomator2（快速模式）")
            return device
        except DeviceError as e:
            if backend == "u2":
                raise
            log(f"改用 adb（每步約 6 秒）：{e}")
    return Device()
