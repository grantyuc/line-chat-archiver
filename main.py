"""LINE 聊天記錄封存工具。

    python main.py run     聊天.txt --work work/名稱      # 一次做完：解析 → 擷取 → 輸出
    python main.py parse   聊天.txt --work work/名稱      # 只解析 txt
    python main.py capture --work work/名稱               # 只擷取手機畫面（可中斷後接續）
    python main.py build   --work work/名稱               # 用已擷取的畫面輸出網站
    python main.py inspect 某次dump.xml                   # 檢查一份 dump 解析出哪些列

執行 capture／run 前，手機要解鎖並打開該聊天室、停在最新訊息。
"""
import argparse
import os
import sys
import xml.etree.ElementTree as ET
from collections import Counter

from archiver.build import build, load_chat, save_chat
from archiver.capture import Capture, CaptureError, oldest_texts
from archiver.device import Device, DeviceError
from archiver.device_u2 import connect
from archiver.render import PAGE_MIN
from archiver.screen import parse_dump
from archiver.txt_parser import parse_file

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except AttributeError:
        pass


def cmd_parse(args):
    chat = parse_file(args.txt, args.me or "")
    save_chat(args.work, chat)
    kinds = Counter(m.kind for m in chat.messages)
    first, last = (chat.messages[0].date, chat.messages[-1].date) if chat.messages else ("-", "-")
    print(f"聊天室：{chat.title}（{'一對一，對方 ' + chat.partner if chat.partner else '群組'}）")
    print(f"自己：{chat.me or '（尚未決定，擷取畫面後會自動判斷）'}")
    print(f"{len(chat.messages)} 則訊息，{first} ～ {last}")
    print("類型：" + "、".join(f"{k} {v}" for k, v in kinds.most_common()))


def cmd_inspect(args):
    with open(args.xml, "rb") as f:
        data = f.read()
    try:
        screen = parse_dump(data)
    except ET.ParseError as e:
        sys.exit(f"{args.xml} 不是有效的 uiautomator dump：{e}")
    print(f"LINE 畫面：{'是' if screen.has_line else '否'}；聊天區 {screen.list_bounds}；蓋在上面的元件 {screen.overlays}")
    for r in screen.rows:
        flags = "（部分）" if r.partial else ""
        extra = f" 媒體 {len(r.media)}" if r.media else ""
        who = f" {r.sender}" if r.sender else ""
        label = f" [{r.date_label}]" if r.date_label and r.kind != "date" else ""
        print(f"  y={r.bounds[1]:4d}～{r.bounds[3]:4d} {r.kind:7s} {r.side:6s} {r.time:5s}{who}{extra}{label}{flags}"
              f"  {r.text[:30]!r}")
        if r.kind == "unknown":
            print(f"      元件：{', '.join(r.ids)}")
    if args.tree:
        print_unknown_trees(data)


def print_unknown_trees(xml):
    """印出未知列的完整元件樹（id、類別、範圍、描述），用來新增支援。文字訊息的內容不印。"""
    from archiver.screen import LIST_ID, _bounds, _rid, _row, _row_nodes
    root = ET.fromstring(xml)
    nodes = list(root.iter("node"))
    lst = next((n for n in nodes if _rid(n) == LIST_ID), None)
    if lst is None:
        return
    width = max(_bounds(n)[2] for n in nodes)
    for row_node in _row_nodes(lst):
        row = _row(row_node, width, _bounds(lst))
        if row.kind != "unknown":
            continue
        print(f"\n未知列 y={row.bounds[1]}～{row.bounds[3]}：")

        def walk(node, depth):
            rid, cls = _rid(node), node.get("class", "").split(".")[-1]
            text = "" if rid == "chat_ui_message_text" else node.get("text", "")[:24]
            desc = node.get("content-desc", "")[:24]
            print(f"  {'  ' * depth}{rid or '-'}  {cls}  {_bounds(node)}"
                  + (f"  desc={desc!r}" if desc else "") + (f"  text={text!r}" if text else ""))
            for child in node.findall("node"):
                walk(child, depth + 1)

        walk(row_node, 0)


def cmd_capture(args):
    # txt 最前面的訊息用來確認真的捲到頂了；還沒 parse 時不檢查
    has_txt = os.path.exists(os.path.join(args.work, "chat.json"))
    oldest = oldest_texts(load_chat(args.work).messages) if has_txt else []
    device = connect(args.backend)
    try:
        Capture(device, args.work, drag=args.drag, duration_ms=args.duration, max_steps=args.max_steps,
                oldest=oldest).run()
    finally:
        if hasattr(device, "close"):
            device.close()


def cmd_build(args):
    if not os.path.exists(os.path.join(args.work, "chat.json")):
        sys.exit("找不到 chat.json：請先執行 parse。")
    if args.me:
        chat = load_chat(args.work)
        chat.me = args.me
        save_chat(args.work, chat)
    device = None if args.offline else Device()
    site = build(args.work, device=device, chat_dir=args.chat_dir, stickers=not args.no_stickers,
                 photos=not args.no_photos, refresh=args.refresh, page_min=args.page_min)
    print(f"\n完成：用瀏覽器打開 {os.path.join(site, 'index.html')}")


def cmd_run(args):
    cmd_parse(args)
    cmd_capture(args)
    cmd_build(args)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, txt=False):
        if txt:
            p.add_argument("txt", help="LINE 匯出的聊天記錄 .txt")
        p.add_argument("--work", required=True, help="這個聊天室的工作目錄，例如 work/waiting")
        p.add_argument("--me", help="自己的顯示名稱（群組聊天用；一對一會自動判斷）")

    def capture_opts(p):
        p.add_argument("--drag", type=float, default=0.75,
                       help="uiautomator2 模式每步最多拖曳聊天區高度的多少（預設 0.75；實際距離依畫面調整，"
                            "確保每一列都完整出現過）。adb 模式固定 0.5")
        p.add_argument("--backend", choices=("auto", "u2", "adb"), default="auto",
                       help="auto：有裝 uiautomator2 就用（快速模式），否則用 adb（約 6 秒一步）")
        p.add_argument("--duration", type=int, default=1000, help="每次滑動的毫秒數（預設 1000）")
        p.add_argument("--max-steps", type=int, default=5000, help="最多捲幾步（預設 5000）")

    def build_opts(p):
        p.add_argument("--chat-dir", help="聊天室快取目錄 ID（u/c/m 開頭）；省略時自動用照片比對找出")
        p.add_argument("--no-stickers", action="store_true", help="貼圖直接用截圖，不比對貼圖快取")
        p.add_argument("--no-photos", action="store_true", help="照片直接用截圖，不比對聊天快取")
        p.add_argument("--offline", action="store_true", help="不連手機：貼圖和照片都用截圖裁切")
        p.add_argument("--refresh", action="store_true", help="重新列出、下載手機上的快取")
        p.add_argument("--page-min", type=int, default=PAGE_MIN,
                       help=f"訊息少於這麼多則的月份和相鄰月份合併成一頁（預設 {PAGE_MIN}；0 為每月一頁）")

    p = sub.add_parser("parse", help="解析 txt")
    common(p, txt=True)
    p.set_defaults(func=cmd_parse)
    p = sub.add_parser("capture", help="擷取手機畫面")
    common(p)
    capture_opts(p)
    p.set_defaults(func=cmd_capture)
    p = sub.add_parser("build", help="輸出網站")
    common(p)
    build_opts(p)
    p.set_defaults(func=cmd_build)
    p = sub.add_parser("run", help="解析＋擷取＋輸出")
    common(p, txt=True)
    capture_opts(p)
    build_opts(p)
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("inspect", help="檢查一份 uiautomator dump")
    p.add_argument("xml")
    p.add_argument("--tree", action="store_true", help="另外印出未知列的完整元件樹")
    p.set_defaults(func=cmd_inspect)

    args = ap.parse_args()
    try:
        args.func(args)
    except (DeviceError, CaptureError) as e:
        sys.exit(f"\n錯誤：{e}")
    except KeyboardInterrupt:
        sys.exit("\n已中斷。再執行同一個指令會從中斷處接續。")


if __name__ == "__main__":
    main()
