"""uiautomator2 的 JSON-RPC 連線：重複使用同一條連線、斷線重連、出錯時改用 uiautomator2 原本的方式。"""
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from archiver.device import DeviceError
from archiver.device_u2 import Rpc, U2Device


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # keep-alive，和 uiautomator2 服務（NanoHTTPD）一樣

    def setup(self):
        self.server.connects += 1
        super().setup()

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((req["method"], req["params"]))
        if req["method"] == "boom":
            body = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -1, "message": "boom"}}
        else:
            body = {"jsonrpc": "2.0", "id": req["id"], "result": [req["method"], req["params"]]}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.calls, srv.connects = [], 0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def test_rpc_reuses_one_connection_and_reconnects(server):
    rpc = Rpc(http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5))
    assert rpc.call("injectInputEvent", 0, 540, 900, 0) == ["injectInputEvent", [0, 540, 900, 0]]
    rpc.call("takeScreenshot", 1, 80)
    rpc.call("dumpWindowHierarchy", False, 50)
    assert server.connects == 1
    rpc.conn.sock.close()                       # 服務關掉閒置的連線
    assert rpc.call("deviceInfo") == ["deviceInfo", []]
    assert server.connects == 2
    with pytest.raises(DeviceError):
        rpc.call("boom")


def test_falls_back_to_uiautomator2_and_drag_sends_down_move_up():
    class Broken:
        def call(self, *a):
            raise OSError("adb 斷線")

    sent = []

    class Proxy:
        def __getattr__(self, method):
            return lambda *params: sent.append((method, params))

    dev = object.__new__(U2Device)
    dev.rpc, dev.d = Broken(), type("D", (), {"jsonrpc": Proxy()})()
    dev.drag(540, 600, 1800, hold=0)
    assert dev.rpc is None and "adb 斷線" in dev.status()
    # 按下 → 小移越過觸控誤差 → 分三段拖完 → 放開
    assert sent == [("injectInputEvent", (0, 540, 600, 0)), ("injectInputEvent", (2, 540, 640, 0)),
                    ("injectInputEvent", (2, 540, 1040, 0)), ("injectInputEvent", (2, 540, 1440, 0)),
                    ("injectInputEvent", (2, 540, 1840, 0)), ("injectInputEvent", (1, 540, 1840, 0))]


class StubRpc:
    """記錄每個請求的開始、結束時間；dump 和截圖各花一點時間。"""

    def __init__(self, log, name, fail=None):
        self.log, self.name, self.fail = log, name, fail

    def call(self, method, *params):
        import base64
        import time

        import cv2
        import numpy as np
        start = time.time()
        if method == self.fail:
            raise OSError("斷線")
        result = None
        if method == "dumpWindowHierarchy":
            time.sleep(0.3)
            result = "<hierarchy><node /></hierarchy>"
        elif method == "takeScreenshot":
            time.sleep(0.1)
            result = base64.b64encode(cv2.imencode(".jpg", np.zeros((20, 10, 3), np.uint8))[1].tobytes()).decode()
        self.log.append((self.name, method, params[:1] if method == "injectInputEvent" else (), start, time.time()))
        return result


def make_device(log, helper_fail=None):
    dev = object.__new__(U2Device)
    dev.rpc, dev.helper = StubRpc(log, "main"), StubRpc(log, "helper", helper_fail)
    dev.d = None
    return dev


def test_release_and_screenshot_overlap_the_dump():
    import time
    log = []
    dev = make_device(log)
    t = time.time()
    xml, (jpeg, img) = dev.drag_and_grab(540, 600, 1800, hold=0.05, settle=0.05)
    elapsed = time.time() - t
    assert xml.startswith(b"<hierarchy") and jpeg[:2] == b"\xff\xd8" and img.shape == (20, 10, 3)
    main = [(m, p) for who, m, p, *_ in log if who == "main"]
    helper = [(m, p) for who, m, p, *_ in log if who == "helper"]
    assert main == [("injectInputEvent", (0,))] + [("injectInputEvent", (2,))] * 4 + [("dumpWindowHierarchy", ())]
    assert helper == [("injectInputEvent", (1,)), ("takeScreenshot", ())]
    dump = next(e for e in log if e[1] == "dumpWindowHierarchy")
    up = next(e for e in log if e[2] == (1,))
    last_move = [e for e in log if e[2] == (2,)][-1]
    assert last_move[4] <= dump[3] < up[3]          # 最後一段移動之後才 dump；dump 開始時還沒放開
    assert elapsed < 0.3 + 0.05 + 0.05 + 0.1        # 放開和截圖藏在 dump 裡


def test_failed_helper_still_releases_the_finger():
    log = []
    dev = make_device(log, helper_fail="injectInputEvent")
    xml, (jpeg, img) = dev.drag_and_grab(540, 600, 1800, hold=0, settle=0)
    main = [(m, p) for who, m, p, *_ in log if who == "main"]
    assert ("injectInputEvent", (1,)) in main and main[-1] == ("takeScreenshot", ())
    assert dev.helper is None                       # 之後改成依序做
