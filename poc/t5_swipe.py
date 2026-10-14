"""測試 5：連續滑動時，每步實際捲了多少、前後截圖重疊多少、多久才停穩。

先在手機上打開一個夠長的聊天室（停在最新訊息），再執行：
  python poc/t5_swipe.py                          # 預設 20 步、每步 1000 ms、拖曳聊天區高度的 40%
  python poc/t5_swipe.py --top 330 --bottom 2050  # 指定聊天區上下緣（測試 2 的可捲動節點）
  python poc/t5_swipe.py --dur 300                # 對照組：快速滑動，慣性較大
  python poc/t5_swipe.py --steps 300              # 一直捲到頂，觀察載入舊訊息時的停頓
  python poc/t5_swipe.py --mode hold              # 實驗：放開前先停住，試著消除慣性

手指由上往下拖，畫面往較舊的訊息捲。過程中不要碰手機，按 Ctrl+C 可提前結束。
每步截圖與逐步數據（steps.csv）存在 poc/out/t5_<模式>_<毫秒>ms_<比例>/。
"""
import argparse
import csv
import statistics
import time

import cv2

from common import Report, decode_png, imwrite, out_path, screen_size, screencap_png, shell

STILL_SCORE = 0.9   # 相鄰兩張位移為 0 且分數達此值，視為畫面已停住
LOW_SCORE = 0.8     # 低於此分數的位移估計不可靠


def grab():
    return decode_png(screencap_png())


def locate(src, dst, y0, band):
    """src 中從 y0 開始的水平帶，在 dst 中往下移了多少。回傳 (位移, 分數)。"""
    tpl = src[y0:y0 + band]
    if tpl.std() < 2:   # 幾乎是空白背景，無法定位
        return None, 0.0
    res = cv2.matchTemplate(dst, tpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    return loc[1] - y0, float(score)


def find_offset(a, b, roi, buffer):
    """估計聊天區從 a 到 b 往下移了多少像素。回傳 (dy, 分數)；dy=None 表示無法判定。

    正向用 a 上半部的水平帶去 b 找；反向用 b 下半部的水平帶回 a 找；取分數最高者。
    位移大時上方的帶會移出畫面，靠近底部的反向帶仍在，可量到約 1/6 的重疊。
    為了速度，在一半解析度上計算。
    """
    top, bottom = roi
    ga = cv2.resize(cv2.cvtColor(a[top:bottom], cv2.COLOR_BGR2GRAY), None, fx=0.5, fy=0.5,
                    interpolation=cv2.INTER_AREA)
    gb = cv2.resize(cv2.cvtColor(b[top:bottom], cv2.COLOR_BGR2GRAY), None, fx=0.5, fy=0.5,
                    interpolation=cv2.INTER_AREA)
    h = ga.shape[0]
    band = max(20, h // 6)
    step = max(1, band // 2)
    best = (None, 0.0)
    for y0 in range(buffer // 2, h // 2 - band + 1, step):
        dy, score = locate(ga, gb, y0, band)
        if score > best[1]:
            best = (dy, score)
    for y0 in range(h - band, h // 2 - 1, -step):
        dy, score = locate(gb, ga, y0, band)
        if score > best[1]:
            best = (-dy, score)
    dy, score = best
    return (None if dy is None else dy * 2), score


def is_still(a, b, roi, buffer):
    dy, score = find_offset(a, b, roi, buffer)
    if dy == 0 and score >= STILL_SCORE:
        return True
    top, bottom = roi
    return float(cv2.absdiff(a[top:bottom], b[top:bottom]).mean()) < 0.5


def wait_still(roi, buffer, timeout):
    """連續截圖直到相鄰兩張沒有位移。回傳 (畫面, 停穩耗時秒數或 None=逾時, 截圖張數)。"""
    t0 = time.time()
    prev, n = grab(), 1
    while time.time() - t0 < timeout:
        cur = grab()
        n += 1
        if is_still(prev, cur, roi, buffer):
            return cur, time.time() - t0, n
        prev = cur
    return prev, None, n


def do_swipe(mode, x, y1, y2, dur):
    """執行一次由上往下的拖曳。回傳錯誤訊息，成功時回傳空字串。"""
    if mode == "swipe":
        out = shell(f"input swipe {x} {y1} {x} {y2} {dur}")
    else:
        # 第一次 MOVE 就超過觸控誤差範圍，避免被判成長按；放開前停 0.3 秒讓速度歸零
        cmds = [f"input motionevent DOWN {x} {y1}"]
        cmds += [f"input motionevent MOVE {x} {y1 + (y2 - y1) * k // 6}" for k in range(1, 7)]
        cmds += ["sleep 0.3", f"input motionevent UP {x} {y2}"]
        out = shell("; ".join(cmds))
    return out.strip() if ("Exception" in out or "Error" in out) else ""


def reliable(r):
    return r["dy"] is not None and r["score"] >= LOW_SCORE


def fmt(r):
    dy = "—" if r["dy"] is None else f"{r['dy']:5d}"
    over = "—" if r["overshoot"] is None else f"{r['overshoot']:+5d}"
    lap = "—" if r["overlap"] is None else f"{r['overlap']:4.0%}"
    settle = "逾時" if r["settle_s"] is None else f"{r['settle_s']:.1f}s"
    return (f"{r['step']:3d}  位移 {dy}px  慣性 {over}px  重疊 {lap}  分數 {r['score']:.2f}"
            f"  停穩 {settle}（截 {r['frames']} 張）  本步共 {r['step_s']:.1f}s"
            + ("" if reliable(r) else "  ← 比對不可靠"))


def summarize(log, rows, drag, pauses):
    log("\n== 摘要 ==")
    if not rows:
        log("沒有資料")
        return
    moving = [r for r in rows if reliable(r) and r["dy"] != 0]
    if moving:
        dys = [r["dy"] for r in moving]
        overs = [r["overshoot"] for r in moving]
        laps = [r["overlap"] for r in moving]
        log(f"可靠且有位移的步數：{len(moving)} / {len(rows)}")
        log(f"位移：最小 {min(dys)}、最大 {max(dys)}、平均 {statistics.mean(dys):.0f} px（拖曳 {drag} px）")
        log(f"慣性（位移 − 拖曳）：平均 {statistics.mean(overs):+.0f} px，標準差 {statistics.pstdev(overs):.0f} px")
        log(f"重疊比例：最小 {min(laps):.0%}、平均 {statistics.mean(laps):.0%}")
    low = [r["step"] for r in rows if not reliable(r)]
    log(f"比對不可靠（分數 < {LOW_SCORE}）的步數：{len(low)}" + (f"（第 {low} 步）" if low else ""))
    settles = [r["settle_s"] for r in rows if r["settle_s"] is not None]
    timeouts = [r["step"] for r in rows if r["settle_s"] is None]
    if settles:
        log(f"停穩耗時：平均 {statistics.mean(settles):.1f}、最長 {max(settles):.1f} 秒；逾時 {len(timeouts)} 步")
    log(f"每步總耗時（滑動 + 停穩）：平均 {statistics.mean(r['step_s'] for r in rows):.1f} 秒")
    log(f"停頓後又恢復移動：{len(pauses)} 次" + (f"，最長停頓 {max(pauses)} 步" if pauses else ""))

    log("\n== 判讀 ==")
    if moving and min(laps) < 0.2:
        log("→ 有步數重疊不足 20%：拼接可能出現缺口，請把 --dist 調小再測。")
    if low:
        log("→ 有步數比對不可靠：打開該步與前一步的 step 圖檢查。常見原因是一步捲太多、"
            "前後兩張幾乎沒有重疊（請調小 --dist 或加長 --dur），或畫面整段空白。")
    if timeouts:
        log("→ 有步數一直停不穩：畫面上可能有動態貼圖或影片，正式版要用位移而不是像素差判斷停止。")
    if pauses:
        log("→ 曾經停住後又繼續捲：多半是 LINE 在載入舊訊息，正式版的「到頂判定」至少要等過這個停頓。")
    if moving and min(laps) >= 0.2 and not low and not timeouts:
        log("→ 位移與重疊都穩定，可用截圖重疊拼接。")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--steps", type=int, default=20, help="最多滑幾步（預設 20）")
    ap.add_argument("--dur", type=int, default=1000, help="每次滑動的毫秒數（預設 1000）")
    ap.add_argument("--dist", type=float, default=0.4, help="拖曳距離佔聊天區高度的比例（預設 0.4）")
    ap.add_argument("--top", type=int, help="聊天區上緣 y（截圖座標；預設螢幕高度 15%%）")
    ap.add_argument("--bottom", type=int, help="聊天區下緣 y（截圖座標；預設螢幕高度 85%%）")
    ap.add_argument("--x", type=int, help="滑動的 x（截圖座標；預設螢幕中線）")
    ap.add_argument("--mode", choices=("swipe", "hold"), default="swipe",
                    help="swipe = input swipe；hold = motionevent 放開前停住")
    ap.add_argument("--stop-after", type=int, default=3, help="連續幾步沒有位移就判定到頂（預設 3）")
    ap.add_argument("--timeout", type=float, default=8, help="每步最多等幾秒讓畫面停穩（預設 8）")
    args = ap.parse_args()

    tag = f"t5_{args.mode}_{args.dur}ms_{round(args.dist * 100)}"
    log = Report(tag)
    ww, wh, dpi = screen_size()
    first = grab()
    H, W = first.shape[:2]
    top = args.top if args.top is not None else int(H * 0.15)
    bottom = args.bottom if args.bottom is not None else int(H * 0.85)
    roi, roi_h = (top, bottom), bottom - top
    buffer = round(48 * dpi / 160)          # 頂部 48dp 不拿來比對，避開浮動日期條
    drag = int(roi_h * args.dist)
    x = args.x if args.x is not None else W // 2
    y1 = top + (roi_h - drag) // 2
    y2 = y1 + drag
    # input 用 wm size 的座標系；截圖尺寸不同時依比例換算
    tx, ty1, ty2 = round(x * ww / W), round(y1 * wh / H), round(y2 * wh / H)

    log(f"截圖 {W}x{H}，wm size {ww}x{wh}，dpi {dpi}")
    log(f"聊天區 y={top}~{bottom}（高 {roi_h}px），頂部緩衝 {buffer}px")
    speed = f"，{args.dur}ms" if args.mode == "swipe" else ""
    log(f"模式 {args.mode}，每步由 y={y1} 拖到 y={y2}（{drag}px{speed}），x={x}")
    if args.mode == "hold":
        log("注意：若手機跳出長按選單，代表 input 太慢被判成長按，請按返回鍵並改用 swipe 模式。")
    log("")

    rows, pauses, streak = [], [], 0
    prev, _, _ = wait_still(roi, buffer, args.timeout)
    imwrite(out_path(tag, "step_000.png"), prev)
    try:
        for i in range(1, args.steps + 1):
            t0 = time.time()
            err = do_swipe(args.mode, tx, ty1, ty2, args.dur)
            if err:
                log(f"滑動失敗：{err[:300]}")
                break
            cur, settle, n = wait_still(roi, buffer, args.timeout)
            dy, score = find_offset(prev, cur, roi, buffer)
            still = is_still(prev, cur, roi, buffer)
            if still:
                dy = 0
            row = {
                "step": i, "dy": dy, "drag": drag,
                "overshoot": None if dy is None else dy - drag,
                "overlap": None if dy is None else round((roi_h - dy) / roi_h, 3),
                "score": round(score, 3), "settle_s": None if settle is None else round(settle, 2),
                "frames": n, "step_s": round(time.time() - t0, 2),
            }
            rows.append(row)
            log(fmt(row))
            imwrite(out_path(tag, f"step_{i:03d}.png"), cur)
            if still:
                streak += 1
                if streak >= args.stop_after:
                    log(f"連續 {streak} 步沒有位移：判定已到頂（或載入超過 {streak} 步的時間）。")
                    break
            else:
                if streak:
                    pauses.append(streak)
                streak = 0
            prev = cur
    except KeyboardInterrupt:
        log("（使用者中止）")

    if rows:
        with open(out_path(tag, "steps.csv"), "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    summarize(log, rows, drag, pauses)
    log.done()


if __name__ == "__main__":
    main()
