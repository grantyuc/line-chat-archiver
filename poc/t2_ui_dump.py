"""測試 2：uiautomator dump 能不能拿到聊天訊息的文字與座標。

先在手機上打開測試聊天室、停在要測的畫面，再執行：
  python poc/t2_ui_dump.py text              # 標籤自訂，用來區分不同畫面
  python poc/t2_ui_dump.py anim --no-anim    # 暫時關閉系統動畫再 dump，結束後自動還原

輸出（poc/out/）：
  t2_<標籤>.xml        原始 dump
  t2_<標籤>.png        同一時間的截圖
  t2_<標籤>_boxes.png  紅框 = 有 text 的節點，藍框 = 只有 content-desc，綠框 = 可捲動區域
  t2_<標籤>.txt        摘要
"""
import argparse
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter

from common import Report, adb, decode_png, imwrite, out_path, screencap_png, shell

REMOTE = "/data/local/tmp/poc_ui.xml"
ANIM_KEYS = ("window_animation_scale", "transition_animation_scale", "animator_duration_scale")


def dump():
    shell(f"rm -f {REMOTE}")
    t0 = time.time()
    msg = shell(f"uiautomator dump {REMOTE}", timeout=90).strip()
    elapsed = time.time() - t0
    xml = adb("exec-out", "cat", REMOTE, check=False)
    return msg, elapsed, xml if xml.lstrip().startswith(b"<?xml") else None


def dump_without_animation(log):
    old = {k: shell(f"settings get global {k}").strip() for k in ANIM_KEYS}
    log("暫時關閉系統動畫，原設定：", old)
    try:
        for k in ANIM_KEYS:
            shell(f"settings put global {k} 0")
        time.sleep(1)
        return dump()
    finally:
        for k, v in old.items():
            shell(f"settings put global {k} {v if v not in ('', 'null') else '1.0'}")
        log("系統動畫設定已還原")


def parse(xml):
    nodes = []
    for n in ET.fromstring(xml).iter("node"):
        bounds = list(map(int, re.findall(r"-?\d+", n.get("bounds", ""))))
        if len(bounds) != 4:
            continue
        nodes.append({
            "text": n.get("text", ""),
            "desc": n.get("content-desc", ""),
            "rid": n.get("resource-id", "").split("/")[-1],
            "cls": n.get("class", "").split(".")[-1],
            "scrollable": n.get("scrollable") == "true",
            "bounds": bounds,
        })
    return nodes


def short(s, n=50):
    s = s.replace("\n", "⏎")
    return s if len(s) <= n else s[: n - 1] + "…"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("label", help="這次畫面的標籤，例如 text、anim、media")
    ap.add_argument("--no-anim", action="store_true", help="dump 前暫時關閉系統動畫")
    args = ap.parse_args()

    log = Report(f"t2_{args.label}")
    msg, elapsed, xml = dump_without_animation(log) if args.no_anim else dump()
    png = screencap_png()
    log(f"uiautomator 訊息：{msg}")
    log(f"dump 耗時：{elapsed:.1f} 秒")
    if xml is None:
        log("dump 失敗，沒有拿到 XML。若訊息是 could not get idle state，代表畫面一直在動；"
            "請改用 --no-anim 再試，或換到沒有動態貼圖的畫面比較。")
        log.done()
        return

    with open(out_path(f"t2_{args.label}.xml"), "wb") as f:
        f.write(xml)
    img = decode_png(png)
    imwrite(out_path(f"t2_{args.label}.png"), img)

    nodes = parse(xml)
    labeled = [n for n in nodes if n["text"] or n["desc"]]
    log(f"節點總數：{len(nodes)}，有 text 或 content-desc 的節點：{len(labeled)}")

    log("\n== 可捲動節點（面積大的通常就是聊天列表，上下緣可當測試 5 的 --top / --bottom）==")
    scroll = sorted((n for n in nodes if n["scrollable"]),
                    key=lambda n: -(n["bounds"][2] - n["bounds"][0]) * (n["bounds"][3] - n["bounds"][1]))
    for n in scroll[:5]:
        x1, y1, x2, y2 = n["bounds"]
        log(f"    [{x1},{y1}][{x2},{y2}]  {n['cls']}  id={n['rid']}  → --top {y1} --bottom {y2}")
    if not scroll:
        log("    沒有")

    log("\n== 有文字或描述的節點（由上到下）==")
    for n in sorted(labeled, key=lambda n: (n["bounds"][1], n["bounds"][0])):
        x1, y1, x2, y2 = n["bounds"]
        log(f"    [{x1},{y1}][{x2},{y2}]  {n['cls']}  id={n['rid'] or '-'}"
            f"  text={short(n['text'])!r}  desc={short(n['desc'])!r}")

    log("\n== 出現最多的 resource-id（可看出 LINE 有沒有區分訊息種類）==")
    for rid, count in Counter(n["rid"] for n in nodes if n["rid"]).most_common(25):
        log(f"    {count:4d}  {rid}")

    import cv2

    for n in nodes:
        x1, y1, x2, y2 = n["bounds"]
        if n["scrollable"]:
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 0), 6)
        elif n["text"]:
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 3)
        elif n["desc"]:
            cv2.rectangle(img, (x1, y1), (x2, y2), (255, 0, 0), 3)
    imwrite(out_path(f"t2_{args.label}_boxes.png"), img)
    log(f"\n已畫框：{out_path(f't2_{args.label}_boxes.png')}")
    log.done()


if __name__ == "__main__":
    main()
