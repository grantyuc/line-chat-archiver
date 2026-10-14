"""實機測試共用工具：呼叫 adb、截圖、讀寫圖片、輸出報告。

多台裝置同時連線時，先設定環境變數 ANDROID_SERIAL=<adb devices 列出的序號>，
adb 會自動使用該裝置。
"""
import os
import re
import subprocess
import sys

LINE_PKG = "jp.naver.line.android"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

# Windows 主控台遇到無法編碼的字元時改印替代字元，不要整支程式中止
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except AttributeError:
        pass


def _run(args, timeout):
    try:
        return subprocess.run(["adb", *args], capture_output=True, timeout=timeout)
    except FileNotFoundError:
        sys.exit("找不到 adb：請安裝 Android SDK Platform-Tools 並把它加入 PATH。")


def adb(*args, check=True, timeout=120):
    """執行 adb，回傳 stdout 位元組。check=True 時失敗就中止。"""
    r = _run(args, timeout)
    if check and r.returncode != 0:
        err = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
        sys.exit(f"adb {' '.join(args)} 失敗：{err}")
    return r.stdout


def adb_text(*args, timeout=120):
    """執行 adb，回傳 stdout + stderr 文字（不因結束碼非零而中止）。"""
    r = _run(args, timeout)
    return (r.stdout + r.stderr).decode("utf-8", "replace")


def shell(cmd, timeout=120):
    """在手機上執行 shell 指令，回傳 stdout + stderr 文字。"""
    return adb_text("shell", cmd, timeout=timeout)


def wake_screen():
    """螢幕關著時點亮它（已亮時沒有作用）。解鎖仍要手動。"""
    shell("input keyevent KEYCODE_WAKEUP")


def screencap_png():
    return adb("exec-out", "screencap", "-p")


def screen_size():
    """回傳 wm 回報的 (寬, 高, dpi)；使用者改過解析度時取 Override 值。"""
    sizes = re.findall(r"(\d+)x(\d+)", shell("wm size"))
    dpis = re.findall(r"(\d+)", shell("wm density"))
    if not sizes or not dpis:
        sys.exit("讀不到螢幕尺寸：請確認 adb devices 顯示 device。")
    w, h = map(int, sizes[-1])
    return w, h, int(dpis[-1])


def decode_png(data):
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        head = data[:200].decode("utf-8", "replace")
        sys.exit(f"截圖解碼失敗，adb 回傳的不是圖片：{head}")
    return img


def imread(path, flags=None):
    """可讀含中文的路徑（cv2.imread 在 Windows 讀不到非 ASCII 路徑）。"""
    import cv2
    import numpy as np

    data = np.fromfile(path, np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR if flags is None else flags)


def imwrite(path, img):
    import cv2

    ok, buf = cv2.imencode(os.path.splitext(path)[1] or ".png", img)
    if ok:
        buf.tofile(path)


def out_path(*parts):
    path = os.path.join(OUT_DIR, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


class Report:
    """同時印到螢幕並寫進 out/<name>.txt，方便事後整份貼回來。"""

    def __init__(self, name):
        self.path = out_path(f"{name}.txt")
        self._f = open(self.path, "w", encoding="utf-8")

    def __call__(self, *parts):
        line = " ".join(str(p) for p in parts)
        print(line)
        self._f.write(line + "\n")
        self._f.flush()

    def done(self):
        self._f.close()
        print(f"\n（以上內容已存到 {self.path}）")
