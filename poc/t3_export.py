"""測試 3：檢查 LINE 匯出 txt 的格式。

用法：python poc/t3_export.py 匯出檔.txt [--lines 80]

摘要裡的「前 N 行原文」和範例行會包含聊天內容，貼給別人之前請先刪改。
"""
import argparse
import re
from collections import Counter

from common import Report

DATE_RE = re.compile(r"^\d{4}[/.\-]\d{1,2}[/.\-]\d{1,2}")
TIME_FORMATS = {
    "24 小時制（14:21）": re.compile(r"^\d{1,2}:\d{2}\t"),
    "前綴上午/下午（下午02:21）": re.compile(r"^(上午|下午|午前|午後)\s?\d{1,2}:\d{2}\t"),
    "後綴 AM/PM（2:21 PM）": re.compile(r"^\d{1,2}:\d{2}\s?(AM|PM|am|pm)\t"),
}
PLACEHOLDER_RE = re.compile(r"^\[[^\]\t]{1,16}\]$")
SYSTEM_HINTS = ("☎", "通話", "收回", "unsent", "加入", "退出", "邀請", "相簿", "記事本", "投票")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="LINE 匯出的 .txt")
    ap.add_argument("--lines", type=int, default=80, help="列出前幾行原文（預設 80）")
    args = ap.parse_args()

    with open(args.path, "rb") as f:
        raw = f.read()
    log = Report("t3_export")
    log("UTF-8 BOM：", "有" if raw.startswith(b"\xef\xbb\xbf") else "無")
    log("換行字元：", "CRLF" if b"\r\n" in raw else "LF")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        log(f"不是 UTF-8：{e}；改用 UTF-16 試讀")
        text = raw.decode("utf-16")
    lines = text.splitlines()
    log("總行數：", len(lines))

    kinds = Counter()
    time_formats = Counter()
    field_counts = Counter()
    field_examples = {}
    placeholders = Counter()
    quoted = []
    continuation = []
    system = Counter()
    system_examples = {}
    dates = []

    for i, line in enumerate(lines):
        if i < 3 and not DATE_RE.match(line):
            kinds["檔頭"] += 1
            continue
        if not line.strip():
            kinds["空行"] += 1
            continue
        if DATE_RE.match(line) and "\t" not in line:
            kinds["日期行"] += 1
            if len(dates) < 3:
                dates.append(line)
            continue
        fmt = next((name for name, rx in TIME_FORMATS.items() if rx.match(line)), None)
        if fmt is None:
            kinds["其他（續行？）"] += 1
            if len(continuation) < 5:
                continuation.append((i + 1, line))
            continue
        kinds["訊息行"] += 1
        time_formats[fmt] += 1
        fields = line.split("\t")
        field_counts[len(fields)] += 1
        field_examples.setdefault(len(fields), []).append((i + 1, line))
        body = "\t".join(fields[2:]) if len(fields) >= 3 else fields[-1]
        if PLACEHOLDER_RE.match(body.strip()):
            placeholders[body.strip()] += 1
        if body.startswith('"') and len(quoted) < 5:
            quoted.append((i + 1, line))
        for hint in SYSTEM_HINTS:
            if hint in body:
                system[hint] += 1
                system_examples.setdefault(hint, (i + 1, line))

    log("\n== 行種類統計 ==")
    for k, v in kinds.most_common():
        log(f"    {k}: {v}")
    log("\n== 日期行範例 ==")
    for d in dates:
        log(f"    {d!r}  括號：{'全形' if '（' in d else '半形' if '(' in d else '無'}")
    log("\n== 時間格式 ==")
    for k, v in time_formats.most_common():
        log(f"    {k}: {v}")
    log("\n== 訊息行以 Tab 分割後的欄位數（3 = 時間/發話者/內容）==")
    for n, v in sorted(field_counts.items()):
        log(f"    {n} 欄: {v} 行")
        if n != 3:
            for no, ex in field_examples[n][:3]:
                log(f"        第 {no} 行：{ex!r}")
    log("\n== 整則內容是 [...] 的佔位符 ==")
    for k, v in placeholders.most_common():
        log(f"    {k}: {v}")
    log("\n== 疑似系統訊息（關鍵字：出現次數，第一個例子）==")
    for k, v in system.most_common():
        no, ex = system_examples[k]
        log(f"    {k}: {v}  第 {no} 行：{ex!r}")
    log("\n== 內容以雙引號開頭的訊息（多行訊息的可能寫法）==")
    for no, ex in quoted:
        log(f"    第 {no} 行：{ex!r}")
    log("\n== 沒有時間戳的行（多行訊息的續行？）==")
    for no, ex in continuation:
        log(f"    第 {no} 行：{ex!r}")
    log(f"\n== 前 {args.lines} 行原文（repr 顯示，\\t 是 Tab）==")
    for i, line in enumerate(lines[: args.lines], 1):
        log(f"{i:4d} {line!r}")
    log.done()


if __name__ == "__main__":
    main()
