"""測試 1：LINE 貼圖快取是否存在、adb 讀不讀得到、檔案是什麼格式。

用法：
  python poc/t1_stickers.py                 # 只在手機上查看
  python poc/t1_stickers.py --pull          # 另外把找到的貼圖目錄拉回電腦並分析
  python poc/t1_stickers.py --analyze 目錄   # 分析電腦上已有的目錄
"""
import argparse
import math
import os
from collections import Counter

from common import LINE_PKG, OUT_DIR, Report, adb_text, shell

BASES = (f"/sdcard/Android/data/{LINE_PKG}", f"/sdcard/Android/media/{LINE_PKG}")
LOCAL_DIR = os.path.join(OUT_DIR, "t1_stickers")


def indent(text):
    return "\n".join("    " + line for line in text.strip().splitlines()) or "    （無輸出）"


def probe(log):
    for base in BASES:
        log(f"$ ls -la {base}")
        log(indent(shell(f"ls -la {base}")))

    log("\n== 各子目錄大小 ==")
    log(indent(shell(f"du -sh {BASES[0]}/* {BASES[0]}/*/* 2>/dev/null")))

    find_cmd = (
        "find " + " ".join(BASES) + " -maxdepth 6 -type d "
        "\\( -iname '*stick*' -o -iname '*sticon*' -o -iname '*emoji*' \\) 2>/dev/null"
    )
    found = sorted(l.strip() for l in shell(find_cmd).splitlines() if l.startswith("/"))
    # 只留最上層的目錄，子目錄會一起拉回來
    dirs = [d for d in found if not any(d.startswith(p + "/") for p in found)]
    log("\n== 名稱含 sticker / sticon / emoji 的目錄 ==")
    for d in dirs:
        count = shell(f"find '{d}' -type f 2>/dev/null | wc -l").strip()
        size = shell(f"du -sh '{d}' 2>/dev/null").split("\t")[0].strip()
        log(f"    {d}  （{count} 個檔案，{size}）")
        log(indent(shell(f"ls '{d}' | head -n 15")))
    if not dirs:
        log("    沒有找到")

    log("\n== /sdcard 下其他名稱含 line 的目錄（參考用）==")
    log(indent(shell("find /sdcard/ -maxdepth 3 -type d -iname '*line*' 2>/dev/null")))
    return dirs


def sniff(path):
    with open(path, "rb") as f:
        head = f.read(4096)
    if not head:
        return "空檔", head
    if head.startswith(b"\x89PNG"):
        return ("APNG" if b"acTL" in head else "PNG"), head
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP", head
    if head[:3] == b"GIF":
        return "GIF", head
    if head[:3] == b"\xff\xd8\xff":
        return "JPEG", head
    if head[:2] == b"PK":
        return "ZIP", head
    if head[4:8] == b"ftyp":
        return "MP4", head
    if head[0] == 0xFF and head[1] & 0xF6 == 0xF0:
        return "AAC", head
    if head.lstrip()[:1] in (b"{", b"["):
        return "JSON?", head
    return "未知", head


def entropy(data):
    counts = Counter(data)
    return -sum(c / len(data) * math.log2(c / len(data)) for c in counts.values())


def analyze(log, root):
    from PIL import Image

    kinds, dims, samples = Counter(), Counter(), []
    unknown_entropy = []
    for dirpath, _, files in os.walk(root):
        for name in sorted(files):
            path = os.path.join(dirpath, name)
            kind, head = sniff(path)
            kinds[kind] += 1
            size = None
            if kind in ("PNG", "APNG", "WEBP", "GIF", "JPEG"):
                try:
                    with Image.open(path) as im:
                        size = im.size
                    dims[size] += 1
                except Exception:
                    kind += "（無法解碼）"
            elif kind == "未知" and len(head) >= 256:
                unknown_entropy.append(entropy(head))
            if len(samples) < 30:
                rel = os.path.relpath(path, root)
                samples.append(f"    {rel}  {os.path.getsize(path)} B  {kind}  {size or ''}")

    log(f"\n== 分析 {root} ==")
    log("格式統計：", dict(kinds))
    log("最常見的尺寸：", [f"{w}x{h}: {n}" for (w, h), n in dims.most_common(8)])
    if unknown_entropy:
        avg = sum(unknown_entropy) / len(unknown_entropy)
        hint = "接近 8，很可能是加密或壓縮過的資料" if avg > 7.5 else "不像加密"
        log(f"未知格式檔案的平均位元組熵：{avg:.2f}（{hint}）")
    log("檔案範例（前 30 個）：")
    for line in samples:
        log(line)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pull", action="store_true", help="把找到的貼圖目錄拉回電腦並分析")
    ap.add_argument("--analyze", metavar="目錄", help="只分析電腦上已有的目錄")
    args = ap.parse_args()

    log = Report("t1_stickers")
    if args.analyze:
        analyze(log, args.analyze)
        log.done()
        return

    dirs = probe(log)
    if args.pull and dirs:
        for d in dirs:
            dest = os.path.join(LOCAL_DIR, d.strip("/").replace("/", "_"))
            os.makedirs(dest, exist_ok=True)
            log(f"\n$ adb pull {d}")
            out = adb_text("pull", d, dest, timeout=3600).strip().splitlines()
            log(indent(out[-1] if out else ""))
            analyze(log, dest)
    elif dirs:
        log("\n找到候選目錄。加上 --pull 參數可拉回電腦分析格式。")
    log.done()


if __name__ == "__main__":
    main()
