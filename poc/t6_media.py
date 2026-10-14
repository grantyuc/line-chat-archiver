"""測試 6：「照片・影片」批次下載後，檔案存到哪裡、解析度多少、能不能對回聊天截圖。

步驟：
  python poc/t6_media.py mark                    # 1. 記錄手機上現有的檔案清單
  （在手機上從「照片・影片」批次下載照片）
  python poc/t6_media.py scan --pull             # 2. 找出新增的檔案並拉回電腦
  python poc/t6_media.py shot                    # 3. 截一張含照片縮圖的聊天畫面
  python poc/t6_media.py match poc/out/t6_shot.png --box x1,y1,x2,y2
                                                 # 4. 拿截圖中的照片縮圖去比對拉回來的原檔
  python poc/t6_media.py viewer X Y              # （選做）點開座標 X,Y 的照片並截圖，再按返回
"""
import argparse
import glob
import os
import sys
import time

from common import OUT_DIR, Report, adb_text, decode_png, imread, imwrite, out_path, screencap_png, shell

SNAPSHOT = os.path.join(OUT_DIR, "t6_snapshot.txt")
LOCAL_DIR = os.path.join(OUT_DIR, "t6_media")
SKIP = ("/Android/data/", "/Android/obb/")


def list_files():
    text = shell("find /sdcard/ -type f 2>/dev/null", timeout=900)
    return {line.strip() for line in text.splitlines()
            if line.startswith("/") and not any(s in line for s in SKIP)}


def quote(path):
    return "'" + path.replace("'", "'\\''") + "'"


def cmd_mark(_args):
    files = list_files()
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(SNAPSHOT, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(files)))
    print(f"已記錄 {len(files)} 個檔案。現在到手機上批次下載照片，完成後執行：python poc/t6_media.py scan --pull")


def cmd_scan(args):
    if not os.path.exists(SNAPSHOT):
        sys.exit("找不到檔案清單：請先執行 python poc/t6_media.py mark")
    with open(SNAPSHOT, encoding="utf-8") as f:
        before = set(f.read().splitlines())
    new = sorted(list_files() - before)
    log = Report("t6_scan")
    log(f"新增檔案：{len(new)} 個")
    for i in range(0, len(new), 40):
        chunk = new[i:i + 40]
        log(shell("stat -c '%s B  %y  %n' " + " ".join(quote(p) for p in chunk)).rstrip())
    if not args.pull or not new:
        log.done()
        return

    from PIL import Image

    os.makedirs(LOCAL_DIR, exist_ok=True)
    log("\n== 拉回電腦後的檔案資訊 ==")
    for path in new:
        local = os.path.join(LOCAL_DIR, os.path.basename(path))
        adb_text("pull", "-a", path, local, timeout=600)
        if not os.path.exists(local):
            log(f"    拉取失敗：{path}")
            continue
        info = f"{os.path.getsize(local)} B"
        try:
            with Image.open(local) as im:
                exif = im.getexif()
                shot = exif.get_ifd(0x8769).get(36867) or exif.get(306)
                info += f"  {im.size[0]}x{im.size[1]}  {im.format}  EXIF 時間：{shot or '無'}"
        except Exception:
            info += "  （非圖片或無法解碼）"
        log(f"    {os.path.basename(path)}  {info}")
    log(f"\n檔案在 {LOCAL_DIR}")
    log.done()


def cmd_match(args):
    import cv2

    from imgmatch import features, score

    shot = imread(args.image, cv2.IMREAD_GRAYSCALE)
    if shot is None:
        sys.exit(f"讀不到圖片：{args.image}")
    if args.box:
        x1, y1, x2, y2 = map(int, args.box.split(","))
        shot = shot[y1:y2, x1:x2]
    query = features(shot)
    if query is None or len(query[0]) < 10:
        sys.exit("縮圖上的特徵點太少，請框大一點或換一張照片。")
    k1 = query[0]

    results = []
    for path in sorted(glob.glob(os.path.join(args.dir, "*"))):
        target = features(path)
        if target is None:
            continue
        inliers, good = score(query, target)
        results.append((inliers, good, os.path.basename(path)))

    if not results:
        sys.exit(f"{args.dir} 裡沒有可讀的圖片。")
    results.sort(reverse=True)
    log = Report("t6_match")
    log(f"縮圖 {shot.shape[1]}x{shot.shape[0]}，特徵點 {len(k1)} 個；比對 {len(results)} 張原檔")
    for inliers, good, name in results[:5]:
        log(f"    幾何一致點 {inliers:4d}  初步配對 {good:4d}  {name}")
    top = results[0][0]
    near = [r for r in results[1:] if r[0] >= 0.9 * top]   # 同分的多半是同一張照片存了兩次
    rest = [r[0] for r in results[1:] if r[0] < 0.9 * top]
    if top >= 25 and top >= 3 * max(rest[0] if rest else 0, 1):
        log("→ 第一名明顯勝出：特徵比對可以把縮圖對回原檔。"
            + (f"另有 {len(near)} 張分數相近，請確認是不是重複的檔案。" if near else ""))
    else:
        log("→ 第一名沒有明顯勝出：請確認原檔真的在資料夾裡、框選範圍是照片本身，再換幾張試。")
    log.done()


def cmd_shot(_args):
    img = decode_png(screencap_png())
    path = out_path("t6_shot.png")
    imwrite(path, img)
    print(f"已存截圖：{path}（{img.shape[1]}x{img.shape[0]}）")


def cmd_viewer(args):
    shell(f"input tap {args.x} {args.y}")
    time.sleep(args.wait)
    img = decode_png(screencap_png())
    path = out_path(f"t6_viewer_{args.x}_{args.y}.png")
    imwrite(path, img)
    shell("input keyevent KEYCODE_BACK")
    print(f"已存檢視畫面截圖：{path}（{img.shape[1]}x{img.shape[0]}），並已按返回鍵。")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mark", help="記錄手機上現有的檔案清單")
    p = sub.add_parser("scan", help="列出 mark 之後新增的檔案")
    p.add_argument("--pull", action="store_true", help="把新增的檔案拉回電腦並讀取解析度與 EXIF")
    sub.add_parser("shot", help="截一張聊天畫面到 poc/out/t6_shot.png")
    p = sub.add_parser("match", help="用截圖中的縮圖比對原檔")
    p.add_argument("image", help="含照片縮圖的聊天截圖（或已裁好的縮圖）")
    p.add_argument("--box", help="縮圖在截圖中的範圍 x1,y1,x2,y2")
    p.add_argument("--dir", default=LOCAL_DIR, help=f"原檔所在資料夾（預設 {LOCAL_DIR}）")
    p = sub.add_parser("viewer", help="點開照片、截圖、按返回")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--wait", type=float, default=2.5, help="點開後等幾秒再截圖（預設 2.5）")
    args = ap.parse_args()
    commands = {"mark": cmd_mark, "scan": cmd_scan, "shot": cmd_shot, "match": cmd_match, "viewer": cmd_viewer}
    commands[args.cmd](args)


if __name__ == "__main__":
    main()
