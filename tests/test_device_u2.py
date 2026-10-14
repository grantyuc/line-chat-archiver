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
    assert dev.rpc is None
    assert sent == [("injectInputEvent", (0, 540, 600, 0)), ("injectInputEvent", (2, 540, 1800, 0)),
                    ("injectInputEvent", (1, 540, 1800, 0))]
