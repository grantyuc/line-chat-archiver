"""透過 adb 操作手機：截圖、uiautomator dump、滑動、拉檔案。

多台裝置同時連線時，先設定環境變數 ANDROID_SERIAL=<序號>。
"""
import re
import subprocess
import time

import cv2
import numpy as np

DUMP_PATH = "/data/local/tmp/line_archiver_ui.xml"


class DeviceError(RuntimeError):
    pass


class Device:
    def _run(self, args, timeout=120):
        try:
            return subprocess.run(["adb", *args], capture_output=True, timeout=timeout)
        except FileNotFoundError:
            raise DeviceError("找不到 adb：請安裝 Android SDK Platform-Tools 並加入 PATH。")

    def shell(self, cmd, timeout=120):
        r = self._run(["shell", cmd], timeout)
        return (r.stdout + r.stderr).decode("utf-8", "replace")

    def screen_size(self):
        sizes = re.findall(r"(\d+)x(\d+)", self.shell("wm size"))
        dpis = re.findall(r"(\d+)", self.shell("wm density"))
        if not sizes or not dpis:
            raise DeviceError("讀不到螢幕尺寸：請確認 adb devices 顯示 device。")
        w, h = map(int, sizes[-1])
        return w, h, int(dpis[-1])

    def screencap(self):
        """回傳 (PNG 位元組, BGR 影像)。"""
        r = self._run(["exec-out", "screencap", "-p"])
        img = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise DeviceError("截圖失敗：" + r.stderr.decode("utf-8", "replace")[:200])
        return r.stdout, img

    def dump(self, retries=3):
        """回傳 uiautomator dump 的 XML 位元組。畫面一直在動時會重試。"""
        last = ""
        for _ in range(retries):
            self.shell(f"rm -f {DUMP_PATH}")
            last = self.shell(f"uiautomator dump {DUMP_PATH}", timeout=90).strip()
            xml = self._run(["exec-out", "cat", DUMP_PATH]).stdout
            if xml.lstrip().startswith(b"<?xml"):
                return xml
            time.sleep(1)
        raise DeviceError(f"uiautomator dump 失敗：{last}")

    def swipe(self, x, y1, y2, duration_ms):
        out = self.shell(f"input swipe {x} {y1} {x} {y2} {duration_ms}")
        if "Exception" in out:
            raise DeviceError(f"無法模擬滑動：{out.strip()[:200]}")

    def wake(self):
        self.shell("input keyevent KEYCODE_WAKEUP")

    def list_files(self, remote_dir):
        """回傳 [(大小, 相對路徑)]。用 xargs 分批，避免檔案很多的目錄被整批漏掉。"""
        out = self.shell(f"find {remote_dir} -type f 2>/dev/null | xargs -n 200 stat -c '%s|%n' 2>/dev/null",
                         timeout=1800)
        files = []
        prefix = remote_dir.rstrip("/") + "/"
        for line in out.splitlines():
            size, _, path = line.partition("|")
            if size.isdigit() and path.startswith(prefix):
                files.append((int(size), path[len(prefix):]))
        return files

    def pull(self, remote, local_dir, timeout=3600):
        """把 remote（檔案或目錄）拉到 local_dir 底下。"""
        r = self._run(["pull", remote, local_dir], timeout)
        if r.returncode != 0:
            raise DeviceError(f"adb pull {remote} 失敗：" + (r.stderr or r.stdout).decode("utf-8", "replace")[:300])

    def pull_many(self, remotes, local_dir, batch=100):
        """一次拉多個檔案到同一個本機目錄。"""
        for i in range(0, len(remotes), batch):
            r = self._run(["pull", *remotes[i:i + batch], local_dir], 1800)
            if r.returncode != 0:
                raise DeviceError("adb pull 失敗：" + (r.stderr or r.stdout).decode("utf-8", "replace")[:300])
