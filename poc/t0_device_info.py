"""測試 0：記錄裝置與 LINE 版本，並確認 adb 能模擬觸控。

用法：python poc/t0_device_info.py
"""
import os
import re
import sys

from common import LINE_PKG, Report, adb, shell

PROPS = (
    "ro.product.manufacturer",
    "ro.product.model",
    "ro.build.version.release",
    "ro.build.version.sdk",
    "ro.build.display.id",
)


def check_devices():
    lines = adb("devices").decode("utf-8", "replace").splitlines()[1:]
    states = [line.split("\t") for line in lines if "\t" in line]
    if not states:
        sys.exit("沒有偵測到裝置：確認 USB 線、USB 偵錯已開啟，再執行 adb devices。")
    for serial, state in states:
        if state != "device":
            sys.exit(f"裝置 {serial} 狀態是 {state}：請在手機上按「允許 USB 偵錯」。")
    if len(states) > 1 and not os.environ.get("ANDROID_SERIAL"):
        sys.exit("連了多台裝置：請先設定環境變數 ANDROID_SERIAL=<序號>。")


def main():
    check_devices()
    log = Report("t0_device")
    for prop in PROPS:
        log(f"{prop}: {shell('getprop ' + prop).strip()}")
    for cmd in ("wm size", "wm density"):
        log(f"{cmd}:", " / ".join(l.strip() for l in shell(cmd).splitlines() if l.strip()))

    m = re.search(r"versionName=(\S+)", shell(f"dumpsys package {LINE_PKG}"))
    log("LINE 版本:", m.group(1) if m else "找不到（LINE 未安裝？）")

    log("input motionevent:", "支援" if "motionevent" in shell("input") else "不支援")

    # KEYCODE_UNKNOWN (0) 不會造成任何動作，只用來測試能否注入事件
    inject = shell("input keyevent 0").strip()
    blocked = "Exception" in inject or "INJECT_EVENTS" in inject
    log("觸控注入:", f"被擋：{inject[:200]}" if blocked else "正常")
    if blocked:
        log("→ 小米/紅米請開啟開發者選項中的「USB 偵錯（安全性設定）」；"
            "其他品牌請在開發者選項找類似的權限開關，然後重跑本測試。")
    log.done()


if __name__ == "__main__":
    main()
