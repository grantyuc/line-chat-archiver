"""測試 7：LINE 的聊天媒體快取（files/chats）裡有什麼。

測試 1 發現 /sdcard/Android/data/jp.naver.line.android/files/chats 有好幾 GB。
這個測試看它的目錄結構和檔名規則，並和測試 6 批次下載的原檔逐一比對內容，
判斷照片原檔能不能直接從這裡取得，不必在手機上手動下載。

用法：
  python poc/t7_chat_media.py                # 在手機上查看結構，並與 poc/out/t6_media 比對
  python poc/t7_chat_media.py --pull 30      # 另外拉回最新的 30 個檔案，分析格式與尺寸
  python poc/t7_chat_media.py --chat 目錄名   # 拉回該聊天室的快取，用特徵點比對測試 6 的照片

聊天室目錄名是 u/c/m 開頭的 33 碼 ID。找出測試聊天室的方法：請對方在測試聊天室傳一張
「新」照片，在手機上點開看大圖，再執行 python poc/t7_chat_media.py --recent 10，
「最近 10 分鐘內有變動的檔案」裡的目錄就是它。點開舊照片不一定會寫入新檔案。
"""
import argparse
import hashlib
import os
import re
import time
from collections import Counter, defaultdict

from common import LINE_PKG, OUT_DIR, Report, adb_text, shell
from t1_stickers import sniff

CHATS = f"/sdcard/Android/data/{LINE_PKG}/files/chats"
T6_DIR = os.path.join(OUT_DIR, "t6_media")
LOCAL_DIR = os.path.join(OUT_DIR, "t7_chat_media")


def quote(path):
    return "'" + path.replace("'", "'\\''") + "'"


def list_files():
    """回傳 [(大小, 修改時間, 相對路徑)]。"""
    out = shell(f"find {CHATS} -type f -exec stat -c '%s|%Y|%n' {{}} + 2>/dev/null", timeout=1800)
    files = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[2].startswith(CHATS + "/"):
            files.append((int(parts[0]), int(parts[1]), parts[2][len(CHATS) + 1:]))
    return files


def pattern(rel):
    """把路徑中的 ID 和長數字換成佔位符，看出檔名規則。"""
    parts = []
    for part in rel.split("/"):
        part = re.sub(r"(?<![0-9a-z])\d{3,}(?![0-9a-z])", "<數字>", part)
        part = re.sub(r"[a-z]?[0-9a-f]{16,}", "<ID>", part)
        parts.append(part)
    return "/".join(parts)


def mb(n):
    return f"{n / 1e6:,.1f} MB"


def fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def variant(name):
    if name.endswith(".thumb"):
        return "縮圖"
    if name.endswith(".original"):
        return "原檔"
    return "一般"


def match_chat(log, chat):
    """用特徵點比對測試 6 下載的每一張照片，找出它在這個聊天室快取中的版本。"""
    from PIL import Image

    from imgmatch import SAME_PHOTO, features, score, similarity

    if not os.path.isdir(T6_DIR):
        log("\n（找不到 poc/out/t6_media，無法比對）")
        return
    local = os.path.join(LOCAL_DIR, chat)
    if not os.path.isdir(local):
        log(f"\n$ adb pull {CHATS}/{chat}")
        out = adb_text("pull", f"{CHATS}/{chat}", LOCAL_DIR, timeout=3600).strip().splitlines()
        log("    " + (out[-1] if out else ""))
    if not os.path.isdir(local):
        log("拉取失敗：請確認目錄名稱正確（見「最近有新檔案的聊天室目錄」）。")
        return

    cache = []                     # (訊息 ID, 路徑, 特徵)
    versions = defaultdict(dict)   # 訊息 ID -> {版本: 尺寸}
    for dirpath, _, names in os.walk(local):
        for name in sorted(names):
            m = re.match(r"\d+", name)
            if not m or name.endswith(("-hash", "-hmac")):
                continue
            path = os.path.join(dirpath, name)
            feats = features(path)
            if feats is None:
                continue
            with Image.open(path) as im:
                versions[m.group(0)][variant(name)] = im.size
            cache.append((m.group(0), path, feats))
    log(f"\n== 測試 6 的照片在 {chat} 快取中的版本 ==")
    log(f"快取中可比對的圖片：{len(cache)} 張，涵蓋 {len(versions)} 則訊息")
    log(f"（先用特徵點挑出前 5 個候選，再疊圖逐塊比對，一致區塊 ≥ {SAME_PHOTO:.0%} 才算同一張）")

    summary = Counter()
    for name in sorted(os.listdir(T6_DIR)):
        path = os.path.join(T6_DIR, name)
        query = features(path)
        if query is None:
            continue
        with Image.open(path) as im:
            qw, qh = im.size
        scored = sorted(((score(query, feats), mid, cpath) for mid, cpath, feats in cache),
                        key=lambda r: -r[0][0])[:5]
        verified = [(similarity(path, cpath, h), inliers, mid)
                    for (inliers, _, h), mid, cpath in scored if inliers >= 8]
        same = sorted((v for v in verified if v[0] >= SAME_PHOTO), reverse=True)
        if not same:
            summary["找不到"] += 1
            best = max(verified, default=(0.0, 0, "-"))
            log(f"    {name}  {qw}x{qh}  → 找不到（最接近：訊息 {best[2]}，一致區塊 {best[0]:.0%}，一致點 {best[1]}）")
            continue
        ncc, inliers, mid = same[0]
        have = versions[mid]
        kind = "有原檔" if "原檔" in have else "有一般版" if "一般" in have else "只有縮圖"
        summary[kind] += 1
        bw, bh = have.get("原檔") or have.get("一般") or have["縮圖"]
        if bw * bh >= qw * qh:
            summary["快取最佳版本不小於下載檔"] += 1
        listed = "、".join(f"{v} {d[0]}x{d[1]}" for v, d in have.items())
        log(f"    {name}  {qw}x{qh}  → 訊息 {mid}（一致區塊 {ncc:.0%}，一致點 {inliers}）：{listed}")
    order = ("有原檔", "有一般版", "只有縮圖", "找不到", "快取最佳版本不小於下載檔")
    log("小結：" + "，".join(f"{k} {summary[k]}" for k in order))


def compare_with_t6(log, files):
    if not os.path.isdir(T6_DIR):
        log("\n（找不到 poc/out/t6_media，略過與測試 6 原檔的比對）")
        return set()
    by_size = defaultdict(list)
    for size, _, rel in files:
        by_size[size].append(rel)
    log("\n== 與測試 6 下載的原檔比對（先比大小，再比 MD5）==")
    chats_hit, matched, total = set(), 0, 0
    for name in sorted(os.listdir(T6_DIR)):
        local = os.path.join(T6_DIR, name)
        if not os.path.isfile(local):
            continue
        total += 1
        size = os.path.getsize(local)
        candidates = by_size.get(size, [])[:30]
        if not candidates:
            log(f"    {name}  {size} B  → 沒有同樣大小的檔案")
            continue
        with open(local, "rb") as f:
            digest = hashlib.md5(f.read()).hexdigest()
        out = shell("md5sum " + " ".join(quote(f"{CHATS}/{c}") for c in candidates))
        hits = [line.split(None, 1)[1].strip()[len(CHATS) + 1:]
                for line in out.splitlines() if line.startswith(digest)]
        if hits:
            matched += 1
            chats_hit.add(hits[0].split("/")[0])
            log(f"    {name}  {size} B  → 內容完全相同：{hits[0]}")
        else:
            log(f"    {name}  {size} B  → {len(candidates)} 個同大小的檔案，但內容不同")
    log(f"完全相同：{matched} / {total}")
    return chats_hit


def pull_samples(log, files, count):
    from PIL import Image

    os.makedirs(LOCAL_DIR, exist_ok=True)
    log(f"\n== 最新 {count} 個檔案的格式 ==")
    for size, mtime, rel in sorted(files, key=lambda f: -f[1])[:count]:
        local = os.path.join(LOCAL_DIR, rel.replace("/", "_"))
        adb_text("pull", f"{CHATS}/{rel}", local, timeout=600)
        if not os.path.exists(local):
            log(f"    拉取失敗：{rel}")
            continue
        kind, _ = sniff(local)
        dims = ""
        if kind in ("PNG", "APNG", "WEBP", "GIF", "JPEG"):
            try:
                with Image.open(local) as im:
                    dims = f"{im.size[0]}x{im.size[1]}"
            except Exception:
                dims = "（無法解碼）"
        log(f"    {rel}  {size} B  {kind}  {dims}")
    log(f"檔案在 {LOCAL_DIR}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pull", type=int, default=0, metavar="N", help="拉回最新的 N 個檔案分析格式")
    ap.add_argument("--chat", metavar="目錄名", help="拉回這個聊天室的快取，與測試 6 的照片做特徵點比對")
    ap.add_argument("--recent", type=int, default=60, metavar="分鐘",
                    help="列出最近幾分鐘內有變動的檔案（預設 60）")
    args = ap.parse_args()

    log = Report("t7_chat_media")
    log(f"$ ls {CHATS} | head")
    log("\n".join("    " + l for l in shell(f"ls {CHATS} | head -n 10").splitlines()))
    log("列舉所有檔案中（檔案多時要幾分鐘）……")
    files = list_files()
    if not files:
        log("沒有讀到任何檔案：目錄不存在或沒有權限。")
        log.done()
        return
    log(f"檔案總數：{len(files)}，總大小：{mb(sum(f[0] for f in files))}")
    du = shell(f"du -sk {CHATS} 2>/dev/null").split()
    if du and du[0].isdigit():
        log(f"du 統計的目錄大小：{mb(int(du[0]) * 1024)}（遠大於上面的總大小時，代表有檔案沒被列出）")

    per_chat = defaultdict(lambda: [0, 0])
    for size, _, rel in files:
        per_chat[rel.split("/")[0]][0] += 1
        per_chat[rel.split("/")[0]][1] += size
    log(f"\n== 聊天室目錄（共 {len(per_chat)} 個，依大小列前 10）==")
    for chat, (n, size) in sorted(per_chat.items(), key=lambda kv: -kv[1][1])[:10]:
        log(f"    {chat}  {n} 個檔案  {mb(size)}")

    log("\n== du 最大的 8 個聊天室目錄，對照實際列出的大小（差很多代表有檔案沒被列出）==")
    for line in shell(f"du -sk {CHATS}/* 2>/dev/null | sort -rn | head -n 8").splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0].isdigit():
            chat = os.path.basename(parts[1].strip())
            log(f"    {chat}  du {mb(int(parts[0]) * 1024):>12}  列出 {mb(per_chat[chat][1]):>12}")

    log(f"\n== 最近 {args.recent} 分鐘內有變動的檔案（直接問手機，不經過上面的清單）==")
    recent = shell(f"find {CHATS} -mmin -{args.recent} -type f 2>/dev/null | head -n 20").split()
    for path in recent:
        log("    " + path[len(CHATS) + 1:])
    if not recent:
        log("    沒有")

    latest = defaultdict(int)
    for _, mtime, rel in files:
        chat = rel.split("/")[0]
        latest[chat] = max(latest[chat], mtime)
    log("\n== 最近有新檔案的聊天室目錄（剛點開過照片的聊天室會排在前面）==")
    for chat, ts in sorted(latest.items(), key=lambda kv: -kv[1])[:8]:
        log(f"    {chat}  最新 {fmt_time(ts)}  {per_chat[chat][0]} 個檔案")

    patterns = Counter()
    pattern_size = Counter()
    examples = {}
    for size, _, rel in files:
        p = pattern(rel)
        patterns[p] += 1
        pattern_size[p] += size
        examples.setdefault(p, (rel, size))
    log("\n== 路徑規則（ID 換成 <ID>、長數字換成 <數字>；依數量列前 20）==")
    for p, n in patterns.most_common(20):
        rel, size = examples[p]
        log(f"    {n:6d} 個  {mb(pattern_size[p]):>12}  {p}")
        log(f"            例：{rel}  {size} B")

    chats_hit = compare_with_t6(log, files)

    focus = args.chat or next(iter(chats_hit), None) or max(per_chat, key=lambda c: per_chat[c][1])
    chat_files = sorted((f for f in files if f[2].startswith(focus + "/")), key=lambda f: f[1])
    log(f"\n== {focus} 中最舊與最新的檔案（依修改時間；看檔名是否隨時間遞增）==")
    shown = chat_files if len(chat_files) <= 10 else chat_files[:5] + [None] + chat_files[-5:]
    for item in shown:
        if item is None:
            log("    ……")
            continue
        size, mtime, rel = item
        log(f"    {fmt_time(mtime)}  {size:>9} B  {rel}")

    if args.pull:
        pull_samples(log, chat_files, args.pull)
    if args.chat:
        match_chat(log, args.chat)
    log.done()


if __name__ == "__main__":
    main()
