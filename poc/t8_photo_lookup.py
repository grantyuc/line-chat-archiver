"""測試 8：照片快速對位。

在聊天室畫面上找出每張照片（uiautomator dump），從截圖裁出它，到該聊天室快取的
縮圖裡找出對應的訊息編號。聊天畫面顯示的就是快取裡的 .thumb，所以只和縮圖比：

1. 粗篩：所有縮圖先縮成 32×32 小圖存成索引（只建一次），一次矩陣運算比完全部。
2. 驗證：只對粗篩前幾名做特徵點＋逐塊比對（imgmatch）。

聊天室快取要先用測試 7 拉回來（poc/out/t7_chat_media/<目錄名>）。

用法：
  python poc/t8_photo_lookup.py --chat 目錄名          # 手機停在有照片的聊天畫面
  python poc/t8_photo_lookup.py --chat 目錄名 --shot poc/out/t2_media.png --xml poc/out/t2_media.xml
                                                       # 改用測試 2 存下的截圖與 dump
"""
import argparse
import glob
import os
import sys
import time
from collections import Counter

import cv2
import numpy as np

from common import LINE_PKG, OUT_DIR, Report, decode_png, imread, imwrite, out_path, screencap_png, wake_screen
from imgmatch import SAME_PHOTO, features, score, similarity
from t2_ui_dump import dump, parse

SIG = 32          # 粗篩小圖邊長
CANDIDATES = 10   # 粗篩後驗證的候選數


def signature(gray):
    small = cv2.resize(gray, (SIG, SIG), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
    small -= small.mean()
    norm = np.linalg.norm(small)
    return small / norm if norm else small


def thumb_index(log, chat):
    """回傳 (縮圖路徑陣列, 小圖矩陣)。第一次建立後存檔，之後直接讀取。"""
    root = os.path.join(OUT_DIR, "t7_chat_media", chat)
    if not os.path.isdir(root):
        sys.exit(f"找不到 {root}：請先執行 python poc/t7_chat_media.py --chat {chat} --skip-match")
    cache = os.path.join(OUT_DIR, f"t8_index_{chat}.npz")
    if os.path.exists(cache):
        data = np.load(cache)
        log(f"讀取縮圖索引：{len(data['paths'])} 張")
        return data["paths"], data["sigs"]
    t0 = time.time()
    paths, sigs = [], []
    for path in sorted(glob.glob(os.path.join(root, "**", "*.thumb"), recursive=True)):
        gray = imread(path, cv2.IMREAD_GRAYSCALE)
        if gray is not None:
            paths.append(path)
            sigs.append(signature(gray))
    paths, sigs = np.array(paths), np.array(sigs, dtype=np.float32)
    np.savez(cache, paths=paths, sigs=sigs)
    log(f"建立縮圖索引：{len(paths)} 張，{time.time() - t0:.1f} 秒（已存檔，下次直接讀取）")
    return paths, sigs


def versions(thumb_path):
    """列出同一則訊息在快取中的其他版本與尺寸。"""
    base = thumb_path[: -len(".thumb")]
    found = []
    for label, path in (("原檔", base + ".original"), ("一般", base)):
        if os.path.exists(path):
            img = imread(path, cv2.IMREAD_GRAYSCALE)
            if img is not None:
                found.append(f"{label} {img.shape[1]}x{img.shape[0]}")
    return "、".join(found) or "只有縮圖"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chat", required=True, help="聊天室快取目錄名（測試 7 找到的 ID）")
    ap.add_argument("--shot", help="改用這張截圖（需同時給 --xml）")
    ap.add_argument("--xml", help="改用這份 uiautomator dump")
    args = ap.parse_args()

    log = Report("t8_photo_lookup")
    paths, sigs = thumb_index(log, args.chat)

    if args.shot and args.xml:
        screen = imread(args.shot)
        with open(args.xml, "rb") as f:
            xml = f.read()
    else:
        wake_screen()
        time.sleep(1)
        msg, elapsed, xml = dump()
        screen = decode_png(screencap_png())
        log(f"dump 耗時 {elapsed:.1f} 秒")
        if xml is None:
            sys.exit(f"dump 失敗：{msg}")
        imwrite(out_path("t8_screen.png"), screen)
        with open(out_path("t8_screen.xml"), "wb") as f:
            f.write(xml)

    if LINE_PKG.encode() not in xml:
        sys.exit("畫面上沒有 LINE：螢幕可能關閉或鎖定，或目前不在 LINE 聊天室。請解鎖並打開聊天室再執行。")
    nodes = parse(xml)
    lists = [n["bounds"] for n in nodes if n["rid"] == "chathistory_message_list"]
    top, bottom = (lists[0][1], lists[0][3]) if lists else (0, screen.shape[0])
    inside = [n for n in nodes if n["rid"] and top <= n["bounds"][1] < bottom]
    # 單張照片是 chat_ui_row_image；一次傳多張的相簿格狀排列可能用別的 id，
    # 所以也收描述含「照片」的圖片元件（頭像 chat_ui_row_thumbnail 除外）
    photos = [n for n in inside if n["rid"] == "chat_ui_row_image"
              or ("照片" in n["desc"] and n["cls"] == "ImageView" and n["rid"] != "chat_ui_row_thumbnail")]
    log(f"畫面上的照片：{len(photos)} 張")
    for rid, count in Counter(n["rid"] for n in photos).items():
        log(f"    {rid}：{count}")
    if not photos:
        log("沒有認出照片。以下是聊天區內所有元件 id 與描述（文字訊息的內容不列出），請把這段貼回來：")
        text_ids = ("chat_ui_row_text_message", "chat_ui_message_text")
        seen = Counter((n["rid"], "" if n["rid"] in text_ids else n["desc"][:20]) for n in inside)
        for (rid, desc), count in seen.most_common(40):
            log(f"    {count:3d}  {rid}  {desc!r}")
    photos = [n["bounds"] for n in photos]

    total = time.time()
    for x1, y1, x2, y2 in photos:
        t0 = time.time()
        crop = cv2.cvtColor(screen[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        partial = y1 <= top + 2 or y2 >= bottom - 2
        coarse = sigs @ signature(crop)
        order = np.argsort(-coarse)[:CANDIDATES]
        query = features(crop)
        hit = None
        for rank, i in enumerate(order, 1):
            inliers, _, h = score(query, features(str(paths[i])))
            agree = similarity(crop, str(paths[i]), h) if inliers >= 8 else 0.0
            if agree >= SAME_PHOTO:
                hit = (rank, i, agree)
                break
        where = f"[{x1},{y1}][{x2},{y2}]" + ("（只露出一部分）" if partial else "")
        if hit:
            rank, i, agree = hit
            mid = os.path.basename(str(paths[i]))[: -len(".thumb")]
            log(f"    {where} → 訊息 {mid}（粗篩第 {rank} 名，粗篩分數 {coarse[i]:.2f}，一致區塊 {agree:.0%}，"
                f"{time.time() - t0:.2f} 秒）：{versions(str(paths[i]))}")
        else:
            best = order[0]
            log(f"    {where} → 找不到（粗篩第 1 名分數 {coarse[best]:.2f}，{time.time() - t0:.2f} 秒）")
    if photos:
        log(f"平均每張 {(time.time() - total) / len(photos):.2f} 秒")
    log.done()


if __name__ == "__main__":
    main()
