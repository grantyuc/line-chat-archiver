"""用 uiautomator2 操作手機，讓每一步擷取從約 6 秒降到 1 秒左右。

內建的 `uiautomator dump` 每次都要重新啟動，一次 3～4 秒；uiautomator2 在手機上常駐一個
服務，dump 和截圖都快得多。滑動也改成「拖曳到底、停住、再放開」，放開時速度為零，
畫面不會再慣性滑動，不必等它停下。截圖直接存服務傳回的 JPEG，不再轉成 PNG
（省下編碼時間，檔案也小很多）。

uiautomator2 3.x 每個請求都重新經 adb 建立一次連線，實測約 60 毫秒；一次拖曳要送好幾個觸控
事件，累積起來很可觀。所以這裡自己和服務維持同一條連線（HTTP keep-alive），出錯時才改回
uiautomator2 原本的方式。

第一次連線時，uiautomator2 會在手機上安裝它的服務 App（ATX）。擷取結束後會停止服務，
以免和內建的 uiautomator（實機測試腳本用的）互搶。
"""
import base64
import http.client
import json
import os
import time

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

    def call(self, method, *params):
        self.id += 1                                    # 也是請求次數
        body = json.dumps({"jsonrpc": "2.0", "id": self.id, "method": method, "params": list(params)})
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
        if "error" in data:
            raise DeviceError(f"{method} 失敗：{str(data['error'])[:300]}")
        return data.get("result")


class U2Device(Device):
    fast = True
    DOWN, UP, MOVE = 0, 1, 2            # MotionEvent 的動作

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
        self.rpc, self.rpc_error = None, "已用 --new-connections 關閉連線重複使用"
        if not reuse:
            return
        try:
            self.rpc = Rpc(AdbConnection(self.d._dev, self.d._device_server_port))
            self.rpc.call("deviceInfo")
        except Exception as e:                            # 版本不同或連不上：用 uiautomator2 原本的方式
            self.rpc, self.rpc_error = None, f"{type(e).__name__}: {e}"

    def _call(self, method, *params):
        if self.rpc is not None:
            try:
                return self.rpc.call(method, *params)
            except Exception as e:                        # 改用 uiautomator2 原本的方式（它會自動重啟服務）
                self.rpc, self.rpc_error = None, f"{type(e).__name__}: {e}"
        return getattr(self.d.jsonrpc, method)(*params)

    def status(self):
        """連線方式的摘要，印在擷取結束時，用來確認連線有沒有重複使用。"""
        if self.rpc is not None:
            return f"uiautomator2 連線：{self.rpc.id} 個請求用了 {self.rpc.conn.opened} 條連線"
        return f"uiautomator2 連線：每個請求各開一條（{self.rpc_error[:200]}）"

    def screencap(self):
        """回傳 (JPEG 位元組, 影像)。"""
        try:
            data = self._call("takeScreenshot", 1, 80)
            raw = base64.b64decode(data) if data else b""
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) if raw else None
            if img is None:                               # 服務沒有傳回截圖時，改用一般介面
                img = self.d.screenshot(format="opencv")
                raw = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()
        except Exception as e:
            raise DeviceError(f"截圖失敗：{e}")
        if img is None:
            raise DeviceError("截圖失敗")
        return raw, img

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

    def drag(self, x, y1, y2, steps=3, hold=0.12):
        """由 y1 拖到 y2 後停住再放開（放開時速度為零，不會慣性滑動）。

        先小移一段越過觸控誤差範圍，再分三段拖完（實機驗證過的手勢）。不能一次跳到終點：
        實機上這樣做，捲了一步之後就捲不動了。
        """
        slop = 40 if y2 > y1 else -40
        self._call("injectInputEvent", self.DOWN, x, y1, 0)
        self._call("injectInputEvent", self.MOVE, x, y1 + slop, 0)
        for k in range(1, steps + 1):
            self._call("injectInputEvent", self.MOVE, x, y1 + slop + (y2 - y1) * k // steps, 0)
        time.sleep(hold)
        self._call("injectInputEvent", self.UP, x, y2 + slop, 0)

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
