"""測試 4：聊天室能不能截圖、截一張要多久。

先在手機上打開測試聊天室，再執行：python poc/t4_screencap.py
截圖存在 poc/out/t4_chat.png。
"""
import re
import time

from common import Report, adb, decode_png, imwrite, out_path, screen_size, shell

ROUNDS = 5


def focused_window_secure():
    """回傳 (焦點視窗名稱, 是否帶 FLAG_SECURE)；判斷不了時第二項為 None。"""
    text = shell("dumpsys window windows")
    m = re.search(r"mCurrentFocus=Window\{(\w+) \S+ ([^}]+)\}", text)
    if not m:
        return "（讀不到）", None
    token, name = m.groups()
    start = text.find(f"Window{{{token} ")
    block = text[start:start + 6000] if start >= 0 else ""
    end = block.find("Window #", 10)
    block = block[:end] if end > 0 else block
    flags = re.search(r"fl=([^\n]*)", block)
    return name, (bool(re.search(r"\bSECURE\b", flags.group(1))) if flags else None)


def main():
    log = Report("t4_screencap")
    for label, args in (("PNG（screencap -p）", ("exec-out", "screencap", "-p")),
                        ("raw（screencap）", ("exec-out", "screencap"))):
        times, size = [], 0
        for _ in range(ROUNDS):
            t0 = time.time()
            size = len(adb(*args))
            times.append(time.time() - t0)
        log(f"{label}：平均 {sum(times) / ROUNDS:.2f} 秒，最慢 {max(times):.2f} 秒，每張 {size / 1e6:.1f} MB")

    img = decode_png(adb("exec-out", "screencap", "-p"))
    path = out_path("t4_chat.png")
    imwrite(path, img)
    h, w = img.shape[:2]
    ww, wh, dpi = screen_size()
    log(f"截圖尺寸：{w}x{h}；wm size：{ww}x{wh}；dpi：{dpi}（1 dp = {dpi / 160:.2f} px）")
    if (w, h) != (ww, wh):
        log("→ 尺寸不同：手機改過顯示解析度，正式版的觸控座標要依比例換算。")

    std = float(img.std())
    dark = float((img.max(axis=2) < 16).mean())
    log(f"像素標準差：{std:.1f}；接近全黑的像素比例：{dark:.0%}")
    name, secure = focused_window_secure()
    log(f"目前焦點視窗：{name}；FLAG_SECURE：{ {True: '有', False: '無', None: '無法判斷'}[secure] }")
    if std < 3 or dark > 0.95 or secure:
        log("→ 截圖被擋（幾乎全黑或帶 FLAG_SECURE）：ADB 截圖路線不可行，請先回報再繼續。")
    else:
        log(f"→ 截圖正常。請打開 {path} 確認內容，並用小畫家等工具記下："
            "標題列下緣的 y、輸入框上緣的 y（就是聊天區上下緣）。")
    log.done()


if __name__ == "__main__":
    main()
