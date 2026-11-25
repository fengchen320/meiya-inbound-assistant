# -*- coding: utf-8 -*-
"""table_grid.py — 用表格线把照片切成网格，再把 OCR 文本塞进格子。

为什么必须靠表格线，而不是靠「字框聚类」：一开始我用字框左边界猜列，
实测直接崩——单元格里的长文本会折成两三行，OCR 每行各成一个框，
于是「Dentall / Implants / Industries」被拆成三行独立数据，
而相邻列又挤在一起（「300348ezt14608」粘成一串）。
表格线是印在纸上的物理约束，比任何启发式都可靠：有线就有格，
格子的行列数目是确定的，折行只会落在同一个格子里。

流程：二值化 → 形态学提横向/纵向长直线 → 求交点得列边界与行边界 →
每个格子取覆盖它的全部 OCR 框（跨多行的也收）按阅读序拼接。
"""
import re

import numpy as np


def _cv2():
    import cv2
    return cv2


def _cluster(vals, tol=12.0):
    """把一堆线段中点聚成线。tol 用 12px：同一根粗线在 Hough 里会报出好几条平行段，
    间距通常在个位数；不同格子线之间至少隔一行字，远大于 12。"""
    vals = sorted(vals)
    if not vals:
        return []
    out, cur = [], [vals[0]]
    for v in vals[1:]:
        if v - cur[-1] <= tol:
            cur.append(v)
        else:
            out.append(sum(cur) / float(len(cur)))
            cur = [v]
    out.append(sum(cur) / float(len(cur)))
    return out


def detect_lines(img_path, min_line_ratio=0.10, max_skew_deg=10.0, roi=None, anchor=None):
    """返回 (xs, ys)：列边界 x 列表、行边界 y 列表（都已排序）。

    roi / anchor 是给「真实手机照片」用的（合成样本不需要，纸面干净）：
    - roi=(x0, y0, x1, y1)：只在单据主体范围内找线。手机拍的对账单背景里
      常有钱包、桌面木纹、本子边——它们同样能被 Hough 认成直线，实测 A 单
      全图检出 22 条竖线 26 条横线，切出来的 21×25 网格绝大部分是背景。
    - anchor=(x0, x1, y0, y1)：表格主体的锚定带（就是表头那一行）。
      竖线必须**穿过**这条带的上下边界才算列边界——真实表格的每一根列线
      都必然压在表头上；背景线不会。横线必须横向覆盖锚定带一半以上宽度。
      这一条比长度阈值强得多：长度阈值只能排除短线段，排除不了长的背景线。

    做法：Canny → HoughLinesP 找线段 → 按倾角分成「横线」「竖线」两堆 →
    各自把中点聚成一条条边界。不用形态学腐蚀膨胀，是因为实测
    手机照片里表格线只有 1–2px 宽、灰度 90 上下（~203 灰阶），
    erode 一步就把线抹没了，投影全是 0；而 Hough 直接在边缘图上找
    连续亮点，细线只要够长就能出来，对线宽不敏感。

    min_line_ratio 是「线段长度至少占图片该方向多少比例才算表格线」。
    0.10 是实测值：太低会把文字下划线、表格外框装饰当线，太高会漏掉
    被折行文本打断的短段（Hough 已用 maxLineGap 把断口接起来）。
    """
    cv2 = _cv2()
    im = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
    if im is None:
        raise IOError("读不到图片：%s" % img_path)
    h, w = im.shape[:2]
    edges = cv2.Canny(im, 40, 120)
    # HoughLinesP 的 minLineLength 按「图片短边」算，不按长边：
    # A4 竖版长边 2339、短边 1654，用长边当基准会把所有竖线筛掉。
    segs = cv2.HoughLinesP(edges, 1, np.pi / 720, 50,
                           minLineLength=int(min(w, h) * min_line_ratio),
                           maxLineGap=25)
    if segs is None:
        return [], []
    hys, vxs = [], []
    ax0, ax1, ay0, ay1 = anchor if anchor else (0.0, float(w), 0.0, float(h))
    aymid = (ay0 + ay1) / 2.0
    awid = max(ax1 - ax0, 1.0)
    for x1, y1, x2, y2 in segs[:, 0]:
        if x2 == x1 and y2 == y1:
            continue
        ang = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(ang) <= max_skew_deg:
            ymid = (y1 + y2) / 2.0
            if anchor and not (ay0 - (ay1 - ay0) <= ymid <= ay1 + (ay1 - ay0)):
                continue
            ov = min(max(x1, x2), ax1) - max(min(x1, x2), ax0)
            if anchor and ov < awid * 0.5:
                continue
            hys.append((min(x1, x2), max(x1, x2), ymid))
        elif abs(abs(ang) - 90.0) <= max_skew_deg:
            xmid = (x1 + x2) / 2.0
            # 竖线必须穿过锚定带的纵向中线：真实表格的列线都压在表头上，
            # 背景里那些凑巧竖直的边缘不穿过。
            if anchor and not (min(y1, y2) <= aymid <= max(y1, y2)):
                continue
            if roi and not (roi[0] <= xmid <= roi[2] and roi[1] <= aymid <= roi[3]):
                continue
            vxs.append((min(y1, y2), max(y1, y2), xmid))
    # 只保留够长的线段，避免把单元格内部的短装饰线当边界。
    # 注意竖线天生比横线短：实测一张 A4 对账单里最长的竖线只有 357px / 2339px，
    # 因为它被每一行的文字切断。曾在这里乘过 1.5 倍「留余量」，结果一行竖线都留不下。
    xs = _cluster([x for y0, y1, x in vxs
                   if (y1 - y0) >= h * min_line_ratio])
    ys = _cluster([y for x0, x1, y in hys
                   if (x1 - x0) >= w * min_line_ratio])
    if len(xs) < 2 or len(ys) < 2:
        return [], []
    if xs[0] > w * 0.02:
        xs = [0.0] + xs
    if xs[-1] < w * 0.98:
        xs = xs + [float(w - 1)]
    if ys[0] > h * 0.02:
        ys = [0.0] + ys
    if ys[-1] < h * 0.98:
        ys = ys + [float(h - 1)]
    return xs, ys


def assign_to_grid(boxes, xs, ys):  # noqa: C901
    """把每个 OCR 框塞进它中心所在的格子：返回 grid[row][col] = [box, ...]。"""
    nr, nc = len(ys) - 1, len(xs) - 1
    grid = [[[] for _ in range(nc)] for _ in range(nr)]

    def find(v, edges):
        for i in range(len(edges) - 1):
            if edges[i] <= v < edges[i + 1]:
                return i
        return len(edges) - 2 if v >= edges[-1] else 0

    for b in boxes:
        r = find(b["cy"], ys)
        c = find(b["cx"], xs)
        grid[r][c].append(b)
    for r in grid:
        for cell in r:
            cell.sort(key=lambda b: (round(b["cy"], 1), b["x0"]))
    return grid


def grid_to_cells(grid):
    """把格子里的多行文本按阅读序拼成一串（用空格连接，保留折行痕迹）。"""
    out = []
    for row in grid:
        out.append([" ".join(b["text"] for b in cell).strip() for cell in row])
    return out


def read_grid(img_path, boxes, min_line_ratio=0.10, roi=None, anchor=None):
    """一步到位：返回 (cells, xs, ys)；检测不到表格线时 cells 为 []。"""
    xs, ys = detect_lines(img_path, min_line_ratio, roi=roi, anchor=anchor)
    if not xs or not ys:
        return [], [], []
    grid = assign_to_grid(boxes, xs, ys)
    return grid_to_cells(grid), xs, ys
