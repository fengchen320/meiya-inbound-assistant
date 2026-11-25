# -*- coding: utf-8 -*-
"""ocr_photo.py — 照片类对账单 → extracted.json（PaddleOCR 通道）。

为什么单独一条通道：表格类单据能按表头读列，照片不行。照片里没有「列」这个概念，
只有一堆带坐标的文字框。所以这里的顺序反过来：先 OCR 拿到 (文本, 四点坐标)，
再用坐标把散字重新拼成行列，最后按 profile 里的表头关键字认列。

三个必须做的预处理（缺一个识别率就崩）：
1. 纠偏：手机拍的纸几乎总是歪的。用 OCR 行框的中位角度旋转回去，
   否则同一行的字会跨到两行，行列聚类全乱。
2. 放大：原文小字（批号、注册证号）在 1000px 宽的图上只有十来个像素高，
   det 模型直接漏检。统一短边放大到 ≥1600px。
3. 增强：手机照片常偏灰。拉对比度比二值化稳——二值化会把细笔画（「8」「6」）
   直接吃掉，而批号恰恰全是数字。

识别不出来的一律进 warnings 并阻断，不猜。照片通道的可信度天然低于表格，
所以 confidence 按 OCR 分数与行列完整性双重给分。
"""
import json
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import table_grid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_DIR = os.path.join(ROOT, "config", "photo_profiles")
CACHE = os.environ.setdefault("PADDLE_PDX_CACHE_HOME", os.path.join(ROOT, "models"))

OCR_BIN = os.environ.get("OCR_PYTHON") or sys.executable

_OCR = None


def get_ocr():
    """惰性构建；enable_mkldnn=False 是本机必须（oneDNN 3.6.2 与 Paddle 3.3.1 PIR 不兼容）。

    三个 use_* 全关，不是省时间，是为了「坐标系一致」：
    doc_orientation 会在内部把图转 90/180 度，doc_unwarping 会把图做透视变形，
    但 predict() 返回的 rec_polys 是变换后图上的坐标。表格线检测（table_grid）吃的
    却是 preprocess 存下来的那张原图——两边坐标系一旦不同，字就会被塞进错误的格子
    （实测 A 单：明细 1 行、22 列空网格、把注册证号 15317242 当成金额）。
    所以几何一律在 preprocess 里做一次，OCR 只负责认字。
    """
    global _OCR
    if _OCR is None:
        from paddleocr import PaddleOCR
        _OCR = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            lang="ch",
            enable_mkldnn=False,
        )
    return _OCR


_ORI = None


def get_ori():
    """文档方向分类模型（PP-LCNet_x1_0_doc_ori，已缓存）。只输出 0/90/180/270，不动像素。"""
    global _ORI
    if _ORI is None:
        from paddleocr import DocImgOrientationClassification
        _ORI = DocImgOrientationClassification()
    return _ORI


def detect_orientation(path):
    """返回 (需逆时针旋转的角度, 置信度)。

    label 语义是实测标定的，不是文档抄的：A 单原图 label=90 → PIL rotate(90) 后变 0；
    rotate(-90) 后变 180；rotate(180) 后变 270。即 label L 表示「原图再逆时针转 L 度就正立」。
    真单据常常是纸条横放、字沿纸长方向印的（A/B 两单实测都是 label=90，置信度 0.84/0.90），
    这种 90 度旋转 deskew 的 max_deg=10 完全管不到，必须单独判。
    """
    r = get_ori().predict(path)[0]
    d = getattr(r, "json", None) or {}
    d = d.get("res") or d if isinstance(d, dict) else {}
    labels = d.get("label_names") or ["0"]
    scores = d.get("scores") or [0.0]
    try:
        deg = int(str(labels[0]).strip()) % 360
    except Exception:
        deg = 0
    return deg, float(scores[0]) if scores else 0.0


# ---------------------------------------------------------------- 图像预处理

def _pil():
    from PIL import Image, ImageEnhance, ImageOps
    return Image, ImageEnhance, ImageOps


def _cv2():
    import cv2
    return cv2


def deskew(gray, max_deg=10.0):
    """按「横线倾角中位数」把整张图摆正。返回 (摆正后的图, 纠正角度)。

    为什么非纠偏不可：手机拍纸几乎不可能正，1 度倾斜在 2000px 高的图上
    就是 35px 的纵向漂移——同一行的字会被判成两行，同一根表格线在
    Canny 里断成一串短段。先摆正，后面行聚与线检测都才有意义。
    倾角只用「接近水平」的线段估，因为表格里横线最多、最可靠；
    竖线数量少且容易被文字边缘干扰。
    """
    cv2 = _cv2()
    h, w = gray.shape[:2]
    edges = cv2.Canny(gray, 50, 150)
    segs = cv2.HoughLinesP(edges, 1, np.pi / 720, 60,
                           minLineLength=int(w * 0.12), maxLineGap=15)
    angs = []
    if segs is not None:
        for x1, y1, x2, y2 in segs[:, 0]:
            if x2 == x1:
                continue
            a = float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
            if abs(a) <= max_deg:
                angs.append(a)
    ang = float(np.median(angs)) if angs else 0.0
    if abs(ang) < 0.15:
        return gray, 0.0
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), ang, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=255), ang


def preprocess(src, dst=None, min_side=1600):
    """方向转正 → 放大 → 拉对比度 → 纠偏。返回新图路径。

    顺序不能换：方向判定必须在放大之前（模型吃原图足够，放大 4 倍只是白烧时间），
    而纠偏必须在转正之后（deskew 只认 ±10 度内的倾斜，图还横躺着的时候它量到的是
    「竖线的倾角」，纠出来的角度毫无意义）。
    """
    Image, ImageEnhance, ImageOps = _pil()
    cv2 = _cv2()
    im0 = Image.open(src)
    im = ImageOps.exif_transpose(im0)
    # EXIF 里带旋转时，模型必须看转正后的像素，否则转两次
    probe = src
    if im.size != im0.size:
        probe = os.path.join(ROOT, "tmp", "photos", "_ori_" + os.path.basename(src) + ".jpg")
        os.makedirs(os.path.dirname(probe), exist_ok=True)
        im.convert("RGB").save(probe, quality=90)
    deg, osc = detect_orientation(probe)
    if deg:
        im = im.rotate(deg, expand=True)
    if os.environ.get("OCR_DEBUG"):
        print("[方向] 逆时针 %.0f 度 (conf=%.2f)" % (deg, osc))
    im = im.convert("L")
    w, h = im.size
    scale = max(1.0, float(min_side) / float(min(w, h)))
    if scale > 1.0:
        im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    im = ImageEnhance.Contrast(im).enhance(1.6)
    # 纠偏必须在这里做：OCR 与表格线检测都吃这张图，
    # 若只给其中一方纠偏，两边的坐标系就对不上了。
    arr, ang = deskew(np.array(im))
    im = Image.fromarray(arr)
    if os.environ.get("OCR_DEBUG"):
        print("[纠偏] %.2f 度" % ang)
    im = im.convert("RGB")
    dst = dst or os.path.join(ROOT, "tmp", "photos", "_pre_" + os.path.basename(src) + ".png")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    im.save(dst)
    return dst


# ---------------------------------------------------------------- OCR 原始输出

def ocr_raw(path):
    """返回 [{'text','score','x0','y0','x1','y1','cx','cy','h'}, ...]，已按 y 升序。"""
    res = get_ocr().predict(path)
    boxes = []
    for r in res:
        d = getattr(r, "json", None)
        d = d.get("res") if isinstance(d, dict) else d
        if not d:
            continue
        texts = d.get("rec_texts") or []
        scores = d.get("rec_scores") or []
        polys = d.get("rec_polys") or d.get("dt_polys") or []
        for i, t in enumerate(texts):
            if i >= len(polys):
                continue
            pts = polys[i]
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            boxes.append({
                "text": (t or "").strip(),
                "score": float(scores[i]) if i < len(scores) else 0.0,
                "x0": min(xs), "x1": max(xs),
                "y0": min(ys), "y1": max(ys),
                "cx": (min(xs) + max(xs)) / 2.0,
                "cy": (min(ys) + max(ys)) / 2.0,
                "h": max(ys) - min(ys),
            })
    boxes = [b for b in boxes if b["text"]]
    boxes.sort(key=lambda b: (b["cy"], b["x0"]))
    return boxes


# ---------------------------------------------------------------- 行列重建

def group_rows(boxes, tol_ratio=0.55):
    """按 y 中心聚成行。

    容差取「本行字高中位数 × 0.55」：同一行的字因字号不同会有几十像素纵向偏移，
    卡片机拍歪时更明显；用固定像素值在不同分辨率下必错。
    """
    if not boxes:
        return []
    hs = sorted(b["h"] for b in boxes)
    med_h = hs[len(hs) // 2] or 20.0
    tol = med_h * tol_ratio
    rows, cur = [], [boxes[0]]
    for b in boxes[1:]:
        if abs(b["cy"] - cur[-1]["cy"]) <= tol or abs(b["cy"] - sum(x["cy"] for x in cur) / len(cur)) <= tol:
            cur.append(b)
        else:
            rows.append(cur)
            cur = [b]
    rows.append(cur)
    for r in rows:
        r.sort(key=lambda b: b["x0"])
    return rows


def row_text(r):
    return " ".join(b["text"] for b in r)


def cluster_columns(rows, ncols_hint=12):
    """由「多字行」的字框左边界聚类出列区间。

    只用多字行：合计行、单号行往往只有一两个字，会把列边界拉歪。
    """
    xs = []
    for r in rows:
        if len(r) >= 3:
            xs.extend(b["x0"] for b in r)
    if not xs:
        return []
    xs.sort()
    gap = max(30.0, (xs[-1] - xs[0]) / max(ncols_hint, 1) * 0.45)
    cols, cur = [], [xs[0]]
    for x in xs[1:]:
        if x - cur[-1] <= gap:
            cur.append(x)
        else:
            cols.append((cur[0], cur[-1]))
            cur = [x]
    cols.append((cur[0], cur[-1]))
    return cols


def assign_cells(rows, cols, pad=None):
    """把每行的字按左边界塞进列，得到二维表。"""
    out = []
    for r in rows:
        cells = [""] * len(cols)
        for b in r:
            best, bestd = -1, 1e18
            for i, (lo, hi) in enumerate(cols):
                if lo - (pad or 40) <= b["x0"] <= hi + (pad or 40):
                    d = abs(b["x0"] - lo)
                    if d < bestd:
                        best, bestd = i, d
            if best >= 0:
                cells[best] = (cells[best] + " " + b["text"]).strip()
        out.append(cells)
    return out


def _bands(boxes, tol):
    """把字框的 y 区间合并成「行带」。返回 [(y0, y1), ...]。

    为什么不用 group_rows 的中心距聚类：真实手机照片有透视，同一物理行的字
    在左右两端能差 30–50px（A 单实测：「种植体系统」y=714–756，「306574」y=745–778，
    差 31px 而中位字高才 33px）。按中心距聚，这一行就被劈成两条。
    按「区间是否重叠」聚则是尺度无关的：只要这一带里还有字没结束，就还是同一行。
    """
    segs = sorted((b["y0"], b["y1"]) for b in boxes)
    bands = []
    for y0, y1 in segs:
        if bands and y0 <= bands[-1][1] + tol:
            bands[-1][1] = max(bands[-1][1], y1)
        else:
            bands.append([y0, y1])
    return [(a, b) for a, b in bands]


def _row_cells(r, cols, bounds, colidx):
    row = [[] for _ in cols]
    for b in r:
        row[colidx(b)].append(b)
    return row


_HDR_EXACT = True


def header_columns(hdr_boxes, col_defs):
    """由表头关键字框的中心定列。返回 [(dst, center), ...] 按 x 升序。

    用「关键字」而不是「表格线」定列，是因为真实单据的表格线会被折行文字切断、
    被印章盖住，而表头文字一定印在它那列的正上方。实测 A/B 两张真图：
    线检测在背景干扰下切出 21 列（大部分是桌面木纹），关键字法稳定给出 13–15 列。
    匹配规则：短词（≤3 字）必须整格等于，长词才允许包含——
    否则「购货单位:某某口腔诊所…」会把「单位」列拽到地址行上去（实测踩过）。
    """
    out = []
    for dst, names in col_defs:
        best = None
        for b in hdr_boxes:
            t = b["text"].replace(" ", "")
            for nm in names:
                n = nm.replace(" ", "")
                ok = (t == n) if (_HDR_EXACT and len(n) <= 3) else (n in t)
                if ok:
                    if best is None or b["x0"] < best["x0"]:
                        best = b
                    break
        if best is not None:
            out.append((dst, (best["x0"] + best["x1"]) / 2.0, best["x0"], best["x1"], best))
    out.sort(key=lambda c: c[1])
    return out


def grid_by_header(boxes, hdr_boxes, col_defs, tol_ratio=0.35, keep_above=False, keep_below=False):
    """表头定列 + 行带切行 → cells。返回 (cells, doc_cols) 或 ([], []).

    这是照片通道的主路径。合成样本上曾用表格线切格（table_grid），
    那张图是程序画的：线直、无背景、无透视。真实手机照片三个条件全不成立，
    所以真图改走这一条；线检测保留给扫描件与合成样本作备选。
    """
    cols = header_columns(hdr_boxes, col_defs)
    if len(cols) < 3:
        return [], []
    cs = [c[1] for c in cols]
    bounds = [cs[0] - (cs[1] - cs[0]) / 2.0 if len(cs) > 1 else cs[0] - 100]
    for i in range(len(cs) - 1):
        bounds.append((cs[i] + cs[i + 1]) / 2.0)
    bounds.append(cs[-1] + (cs[-1] - cs[-2]) / 2.0 if len(cs) > 1 else cs[-1] + 100)

    hs = sorted(b["h"] for b in boxes)
    med_h = hs[len(hs) // 2] or 20.0
    tol = med_h * tol_ratio
    # 表头底边 = 全部命中框的最低边。不能只取「命中最多的那一行」：
    # 这张单据是横着拍的，同一行表头的字因为透视在 y 上能差 76px
    # （「失效日期」cy=591 对「金额」cy=631，中位字高才 40px），按行聚类会把它
    # 拆成好几行，取最长那行就会把「批号/生产日期/失效日期」漏在表头外，
    # 于是它们跑到第一条记录带里，日期和注册证号全被当成明细值。
    hdr_bottom = max(c[4]["y1"] for c in cols)
    # 印刷噪点（「L+d:」这种 14px 高的碎字，正常字高 40px）会把厂家名污染成
    # 「Implants Industries L+d:」。按字高中的位数砍掉过矮的碎块。
    keep = [b for b in boxes if b["h"] >= med_h * 0.42]
    boxes = keep or boxes

    def colidx(b):
        for k in range(len(cols)):
            if bounds[k] <= b["cx"] < bounds[k + 1]:
                return k
        return 0 if b["cx"] < bounds[0] else len(cols) - 1

    # 切记录带：用「锚点列」的字框当行心，不用全局 y 聚类。
    # 原因（实测 A/B 两单）：明细单元格是多行折行的，各列行数不同且上下对齐方式
    # 不一致——A 单「规格型号」占 693–778 三行，「品名」只占 714–778 两行，
    # 而「数量」就是一个单行数字。按 y 中心聚类会把一格折行拆成两条记录，
    # 锚点列（数量/金额，一行一个数字，一行一条记录）才是可靠的行标。
    anchor_dst = None
    for want in ("quantity", "amount", "unit_price"):
        for i, c in enumerate(cols):
            dst = c[0]
            if dst == want:
                anchor_dst = (want, i)
                break
        if anchor_dst:
            break
    if not anchor_dst:
        anchor_dst = (cols[0][0], 0)
    _ = med_h

    abox = [b for b in boxes if colidx(b) == anchor_dst[1] and b["cy"] > hdr_bottom - tol]
    abox.sort(key=lambda b: b["cy"])
    groups = []
    for b in abox:
        if groups and b["cy"] - groups[-1][-1]["cy"] <= med_h * 1.15:
            groups[-1].append(b)
        else:
            groups.append([b])
    if not groups:                       # 没有锚点列可用，退回全局行聚类
        rows = group_rows(boxes)
        return ([[ " ".join(x["text"] for x in sorted(c2, key=lambda b: (round(b["cy"], 1), b["x0"]))).strip()
                   for c2 in _row_cells(r, cols, bounds, colidx)]
                 for r in rows if keep_above or max(b["cy"] for b in r) > hdr_bottom - tol], cols)
    anchors = [sum(x["cy"] for x in g) / len(g) for g in groups]

    # 表头底边必须「逐列」算，不能用全局 hdr_bottom。
    # 原因（实测 A 单）：这张单横着拍，表头一行在 y 上散开 96px——「或注册证号」y1=617，
    # 而「商品编号」的「编号」被折到下一行、y1=704。取全局最大 704 当第一条记录带的
    # 上界，就会把 cy=690 的注册证号 153172425 和 cy=698 的日期
    # 「2026-05-182031-05-17」挡在记录带外，A 单因此丢了效期与注册证。
    # 每列的表头字就在自己那列正上方，逐列取底边天然抵消透视。
    col_bottom = [c[4]["y1"] for c in cols]
    cells = []
    span = (anchors[-1] - anchors[0]) / max(len(anchors) - 1, 1) if len(anchors) > 1 else med_h * 4
    for i in range(len(anchors)):
        ylo = hdr_bottom if i == 0 else (anchors[i - 1] + anchors[i]) / 2.0
        yhi = (anchors[i] + anchors[i + 1]) / 2.0 if i + 1 < len(anchors) else anchors[i] + span * 0.55
        yhi_last = yhi                 # 循环结束后即为「最后一条记录带的下界」，供 keep_below 用
        row = [[] for _ in cols]
        for b in boxes:
            k = colidx(b)
            # 第一条记录带的上界也逐列取：这一格的表头字结束之后，同一格里的
            # 明细字就是数据。用全局 hdr_bottom（表头里最低的那个字，A 单是「编号」704）
            # 会把同列里 cy=690 的注册证号、cy=698 的日期一起挡在带外。
            lo = (col_bottom[k] + med_h * 0.12) if i == 0 else ylo
            if b["cy"] < lo or b["cy"] >= yhi:
                continue
            row[k].append(b)
        cells.append([" ".join(x["text"] for x in sorted(c2, key=lambda b: (round(b["cy"], 1), b["x0"]))).strip()
                      for c2 in row])
    if keep_above:
        # 单据头区（表头之上）仍按普通行聚类给出，供 doc_fields 取销售单号/日期等
        pre = [r for r in group_rows([b for b in boxes if b["cy"] <= hdr_bottom])
               if max(b["cy"] for b in r) <= hdr_bottom]
        cells = [[" ".join(x["text"] for x in sorted(c2, key=lambda b: (round(b["cy"], 1), b["x0"]))).strip()
                  for c2 in _row_cells(r, cols, bounds, colidx)] for r in pre] + cells
    if keep_below:
        # 表格下方的窄带（批号/生产日期/失效日期/注册证号/合计）会落在最后一条记录带的
        # 下界之外，不补回来 extract 就取不到它们（实测多行样本：3 行明细全部缺批号+效期）。
        # 这里只负责把字框还原成行，怎么取字段交给 extract 的 tail_txt 逻辑。
        tail = [r for r in group_rows([b for b in boxes if b["cy"] > yhi_last]) if r]
        cells = cells + [[" ".join(x["text"] for x in sorted(c2, key=lambda b: (round(b["cy"], 1), b["x0"]))).strip()
                          for c2 in _row_cells(r, cols, bounds, colidx)] for r in tail]
    return cells, cols


# ---------------------------------------------------------------- profile 与字段映射

def load_profile(pid):
    for name in (pid, pid + ".json"):
        p = os.path.join(PROFILE_DIR, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    raise FileNotFoundError("找不到照片版式 profile：%s（应放在 %s）" % (pid, PROFILE_DIR))


def _norm(s):
    return re.sub(r"[\s:：]+", "", str(s or "")).lower()


def find_header(cells, keywords):
    """在二维表里找表头行：命中关键词最多的那行。"""
    best_i, best_n = -1, 0
    for i, c in enumerate(cells):
        t = _norm("".join(c))
        n = sum(1 for k in keywords if _norm(k) in t)
        if n > best_n:
            best_i, best_n = i, n
    return best_i, best_n


def col_of(cells_header, names):
    """按名字在该行的单元格里找列号（精确优先，再包含）。"""
    for nm in names:
        n = _norm(nm)
        for i, c in enumerate(cells_header):
            if _norm(c) == n:
                return i
        for i, c in enumerate(cells_header):
            if n and n in _norm(c):
                return i
    return -1


# ---------------------------------------------------------------- 抽取主流程

_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_DATE = re.compile(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})")


def _num(s):
    if s is None:
        return None
    m = _NUM.search(str(s).replace(",", ""))
    return float(m.group(0)) if m else None


def _date(s):
    if not s:
        return None, None
    m = _DATE.search(str(s))
    if m:
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3))), "day"
    m = re.search(r"(\d{4})[-/.年](\d{1,2})", str(s))
    if m:
        return "%s-%02d-01" % (m.group(1), int(m.group(2))), "month"
    return None, None


_LIC = re.compile(r"国[械药]注[进准许]\s*[A-Za-z0-9]{6,}")


def _lic(s):
    """把注册证号那一格里混进来的表头文字剥掉，并把被折行拆开的号码接起来。

    实测 A 单这一格是「或注册证号 国械注进20 153172425」——表头「或注册证号」和号码
    印在同一格（单据上「或注册证号」是表头里的说明字），号码本身又被折成两段。
    直接存原文会让入库表带着「或注册证号 」六个汉字去提交，必被拒。
    """
    if not s:
        return None
    m = _LIC.search(str(s).replace(" ", "").replace("　", ""))
    return m.group(0) if m else str(s).strip() or None


def _load_prefixes():
    p = os.path.join(ROOT, "config", "supplier_doc_prefixes.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return {str(k).upper(): v for k, v in (d.get("prefixes") or {}).items()}
    except Exception:
        return {}


def extract(img_path, profile_id, do_preprocess=True, debug_dir=None):
    """照片 → (doc, lines, warnings)，结构与 read_template.read 一致，可直接喂 build_import。"""
    prof = load_profile(profile_id)
    src = preprocess(img_path) if do_preprocess else img_path
    boxes = ocr_raw(src)
    warnings = []
    if not boxes:
        raise RuntimeError("OCR 未识别到任何文字：%s" % img_path)

    # 首选：表头关键字定列 + 行带切行。这是真实手机照片的主路径——
    # 表格线在真图上不可靠（折行把竖线切断、印章压住格线、透视让横线倾斜，
    # 实测 A 单的线检测切出 21 列，大半是桌面木纹），而表头文字一定印在它那列正上方。
    col_defs = [(c["dst"], c.get("names") or []) for c in (prof.get("columns") or [])]
    kw = prof.get("header_keywords") or []
    rb = group_rows(boxes)
    bi, bn = 0, 0
    for i, r in enumerate(rb):
        t = _norm(row_text(r))
        n = sum(1 for k in kw if _norm(k) in t)
        if n > bn:
            bi, bn = i, n
    cells, gcols = [], []
    if col_defs and bn >= int(prof.get("min_header_hits") or 3):
        hr = rb[bi]
        ht = min(b["y0"] for b in hr)
        hb = max(b["y1"] for b in hr)
        hs = sorted(b["h"] for b in boxes)
        tol0 = (hs[len(hs) // 2] or 20.0) * 0.6
        # 表头文字可能被行带切到相邻两带里，把 y 与表头带重叠的框都收进来当定列依据
        hdr_boxes = [b for b in boxes
                     if b["cy"] > ht - tol0 and b["cy"] < hb + tol0 and b["y0"] < hb]
        # 再以「已命中列名框」的 y 中位为轴扩一圈：横拍/斜拍时同一行表头在 y 上
        # 散得很开（实测 76px），一次聚类抓不全，标题里的「批号/生产日期/失效日期」
        # 就会漏在定列依据之外。扩一圈后再定列，列才齐。
        c0 = header_columns(hdr_boxes, col_defs)
        if c0:
            hs2 = sorted(b["h"] for b in boxes)
            mh2 = hs2[len(hs2) // 2] or 20.0
            cys = sorted(c[4]["cy"] for c in c0)
            cy_mid = cys[len(cys) // 2]
            wide = [b for b in boxes if abs(b["cy"] - cy_mid) <= mh2 * 1.25]
            if wide:
                hdr_boxes = wide
        cells, gcols = grid_by_header(boxes, hdr_boxes, col_defs, keep_above=True, keep_below=True)
    grid_mode = bool(cells)
    if not grid_mode:
        # 备选一：表格线切格（合成样本与扫描件；线直、无背景、无透视时才成立）
        cells = table_grid.read_grid(src, boxes, float(prof.get("min_line_ratio") or 0.10))[0]
        grid_mode = bool(cells)
    if not grid_mode:
        # 备选二：字框聚类（最后兜底，会把折行拆成多行、把邻列粘连，只用于应急）
        rows = group_rows(boxes)
        cols = cluster_columns(rows, int(prof.get("ncols_hint") or 12))
        cells = assign_cells(rows, cols)

    if debug_dir:
        os.makedirs(debug_dir, exist_ok=True)
        with open(os.path.join(debug_dir, "ocr_cells.json"), "w", encoding="utf-8") as f:
            json.dump({"grid_mode": grid_mode, "cells": cells}, f, ensure_ascii=False, indent=1)

    # 定位表头行
    hi, hit = find_header(cells, prof.get("header_keywords") or [])
    min_hit = int(prof.get("min_header_hits") or 3)
    if hi < 0 or hit < min_hit:
        raise RuntimeError(
            "[版式不符] 未在图片里找到表头（命中关键词 %d 个，要求 ≥%d）。"
            "请换 profile 或改用人工录入；禁止按猜测的列序解析。" % (hit, min_hit))
    header = cells[hi]

    # 单据头字段：表头上方按关键字取值
    doc = {"doc_type": prof.get("doc_type") or "对账单", "doc_no": None, "doc_date": None,
           "supplier_raw": None, "buyer": None, "warehouse": None,
           "total_amount": None, "total_amount_cn": None, "total_qty": None,
           "image_quality": None, "source_file": os.path.basename(img_path),
           "source_sheet": "photo:%s" % profile_id}

    # 单据头区。网格模式与聚类模式的数据形态不同：网格模式下每一行就是一串单元格，
    # 没有字框列表，所以这里统一先折成文本再匹配，两种模式共用一套取值逻辑。
    head_texts = [" ".join(c) for c in cells[:hi]] if grid_mode else [row_text(r) for r in rows[:hi]]
    for key, names in (prof.get("doc_fields") or {}).items():
        for hr in head_texts:
            t = hr
            for nm in names:
                if nm in t:
                    val = t.split(nm, 1)[1].strip("  :：")
                    # 同一行常并排放两个字段（「销售单号：XSAZ…　制单日期：2026-11-16」），
                    # 切出来的尾巴会把下一个字段一起带上。在下一个「XX：」处截断。
                    # 单元格拼接用的是单空格，所以不能用「连续两个空白」当分隔；
                    # 改成「空格 + 下一个中文字段名 + 冒号」前瞻。
                    val = re.split(r"\s+(?=[\u4e00-\u9fa5]{2,10}\s*[:：])|\s{2,}", val)[0].strip("  :：")
                    if val:
                        doc[key] = val
                        break
            if doc.get(key):
                break

    # 供货单位兜底：很多随货单不写「供货单位：」标签，而是把公司名印在标题里
    # （实测合成样本标题就是「某某医疗器械有限公司 随货同行对账单」）。
    # 只认带公司后缀的机构名，且必须跳过购货单位所在行——把买方当卖方，
    # 入库单的供应商就会记成自家诊所。命中后照样告警，交审核台确认。
    if not doc.get("supplier_raw"):
        pat = re.compile(r"[\u4e00-\u9fa5A-Za-z（）()]{2,40}?(?:有限责任公司|有限公司)")
        for hr in head_texts:
            if doc.get("buyer") and doc["buyer"] in hr:
                continue
            m = pat.search(hr)
            if m:
                doc["supplier_raw"] = m.group(0)
                warnings.append(
                    "[标题推断] 供货单位「%s」取自单据标题（单据上没有「供货单位：」标签），请核对"
                    % m.group(0))
                break

    # 供应商第三级兜底：单据编号前缀。随货单上的供货单位常常只印在红章里
    # （实测 B 单全图没有公司名文字，只有「出库专用章」），而单号前缀由该供应商的
    # 系统生成，是印刷体。仍要告警，因为这是「按单号推断」而不是读到的。
    if not doc.get("supplier_raw") and doc.get("doc_no"):
        pfx = _load_prefixes()
        up = str(doc["doc_no"]).upper()
        for k, v in pfx.items():
            if up.startswith(k):
                doc["supplier_raw"] = v
                warnings.append("[单号推断] 供货单位「%s」按单据编号前缀 %s 推得，请核对" % (v, k))
                break

    # 明细行
    lines = []
    stop_kw = prof.get("stop_keywords") or []
    colmap = {}
    if gcols:
        # 表头定列法已经给出「字段 → 列号」，直接用它。不能再按单元格文本二次匹配：
        # 真实单据的表头是两行印的（「商品」在上、「编号」在下，「医保编码」「商品代码」
        # 挤在同一格），单行表头里根本凑不齐这些词。
        colmap = {c[0]: i for i, c in enumerate(gcols)}
    for c in (prof.get("columns") or []):
        if c["dst"] in colmap:
            continue
        idx = col_of(header, c.get("names") or [])
        if idx >= 0:
            colmap[c["dst"]] = idx
        elif c.get("required"):
            warnings.append("[表头缺失] 列「%s」在照片里没定位到，该字段全部留空待人工补"
                            % (c.get("names") or [c["dst"]])[0])

    if "product_name" not in colmap:   # 注意是成员判断：列号 0 也是合法值
        raise RuntimeError("[版式不符] 没定位到「商品名称」列，无法解析明细")

    # 明细区。网格模式与聚类模式的行对象不同：
    # 网格模式下「行」就是一串单元格，没有字框列表，置信度与 bbox 只能另取。
    # 表格下方那几行窄带（批号/生产日期/失效日期/注册证号/合计）现在也被 keep_below
    # 拼进了 cells。它们不是明细行，一旦进明细循环就会产出「第 5 个明细块…没有数量
    # 与金额」这种噪声告警，干扰真正的阻断项（实测多行样本）。遇到表外标签就收尾，
    # 这些行留给后面的 tail_txt 逻辑去取字段。
    _TAIL_HEAD = re.compile(r"^\s*(批号|批次号|LOT|生产日期|生产批号|失效日期|有效期|"
                            r"注册证号|医保编码|合计|本页小计|备注)")
    body_all = cells[hi + 1:]
    if grid_mode:
        cut = len(body_all)
        for _i, _c in enumerate(body_all):
            if _TAIL_HEAD.match(" ".join(_c)):
                cut = _i
                break
        body = body_all[:cut]
    else:
        body = body_all
    body_rows = rows[hi + 1:] if not grid_mode else [[] for _ in body]
    for i, (cells_r, r) in enumerate(zip(body, body_rows)):
        txt = " ".join(cells_r) if grid_mode else row_text(r)
        if any(k in txt for k in stop_kw):
            break
        if not txt.strip():
            continue
        def cell(dst):
            j = colmap.get(dst)
            return cells_r[j].strip() if j is not None and j < len(cells_r) else ""

        name = cell("product_name")
        if not name:
            continue
        qty = _num(cell("quantity"))
        price = _num(cell("unit_price"))
        amount = _num(cell("amount"))
        # 金额列被红章/折痕盖掉时整格只剩章文字，_num 取不到数。只要数量和单价
        # 都在，金额是可算的——但必须告警，因为折扣/赠品行会让乘积与实收不等
        # （实测多行样本：第三行被「出库专用章」压住，金额列读不出来）。
        if amount is None and qty is not None and price is not None:
            amount = round(qty * price, 2)
            warnings.append("[金额推算] %s 金额列未读出（可能被印章或折痕遮挡），"
                            "按 数量 %s × 单价 %s 推得 %s，请核对" % (name[:20], qty, price, amount))
        # 没有任何数量/金额的行多半是备注或签名，跳过但留痕
        if qty is None and amount is None:
            warnings.append("[跳过] 第 %d 个明细块「%s」没有数量与金额，按非明细行跳过" % (i + 1, name[:20]))
            continue

        # 生产日期与失效日期在真实单据上常挤成一格（「2026-05-182031-05-17」，
        # 两个日期之间连空格都没有）。此时按印刷先后拆：先印生产、后印失效。
        _mtxt = cell("mfg_date") or ""
        _etxt = cell("expiry_date") or ""
        _all_d = [m.group(0) for m in _DATE.finditer(_mtxt + " " + _etxt)]
        if len(_all_d) == 2 and _mtxt != _etxt:
            mfg, mfg_p = _date(_all_d[0])
            exp, exp_p = _date(_all_d[1])
            warnings.append("[日期拆分] 生产日期与失效日期印在同一格（%s / %s），按印刷先后拆分，请核对"
                            % (_all_d[0], _all_d[1]))
        else:
            mfg, mfg_p = _date(_mtxt)
            exp, exp_p = _date(_etxt)
        dp = {}
        if mfg_p == "month":
            dp["mfg_date"] = "month"
        if exp_p == "month":
            dp["expiry_date"] = "month"

        ln = {
            "line_no": len(lines) + 1,
            "product_name": name,
            "spec": cell("spec") or None,
            "brand": cell("brand") or None,
            "manufacturer": cell("manufacturer") or None,
            "lot_no": cell("lot_no") or None,
            "quantity": qty,
            "unit": cell("unit") or None,
            "unit_price": price,
            "amount": amount,
            "mfg_date": mfg,
            "expiry_date": exp,
            "license_no_raw": _lic(cell("license_no")),
            "confidence": round(sum(b["score"] for b in r) / max(len(r), 1), 3) if r else None,
            "bbox": {"x0": min(b["x0"] for b in r), "y0": min(b["y0"] for b in r),
                     "x1": max(b["x1"] for b in r), "y1": max(b["y1"] for b in r)} if r else None,
        }
        # 采购单位是必填列，漏一个字整单会被美亚拒。真图上「个」这种单字容易被
        # 表格线/印章压掉（实测 B 单 63 个字框里没有「个」），此时按 profile 的
        # default_unit 兜底并强制告警——不许悄悄填一个看起来合理的值。
        if not ln.get("unit") and prof.get("default_unit"):
            ln["unit"] = prof["default_unit"]
            warnings.append("[单位缺省] %s 未识别到采购单位，按版式默认值「%s」填，请核对"
                            % (ln["product_name"], prof["default_unit"]))
        if dp:
            ln["date_precision"] = dp
        # 照片通道的低置信字段单独列出，供审核台高亮
        low = [k for k in ("spec", "lot_no", "mfg_date", "expiry_date", "license_no")
               if ln.get(k) is None]
        if low:
            ln["missing"] = low
        lines.append(ln)

    if not lines:
        raise RuntimeError("[无明细] 表头下方没有解析出任何明细行")

    # 表外的批号/效期/注册证小字：真实单据里这几项常常印在表格下方的窄条里，
    # 一格装不下就折行，或干脆整段落在表格线之外。按出现顺序回填到明细行，
    # 顺序对不上就只写单据级字段，绝不按「位置猜」硬塞到某一行。
    tail_txt = " ".join(" ".join(c) for c in cells[hi + 1 + len(lines):]) if grid_mode else ""
    if tail_txt:
        # 真实单据把多行的批号/日期写在同一条窄带里，用「 / 」分隔：
        # 「批号 W0261474 / W0261385 生产日期 2026-06-23 / 2026-06-16 有效期至 …」
        # 关键是要先把这条带子按「下一个标签」切开——标签后面没有冒号，
        # 靠「空格+冒号」是切不动的，会把这个日期当成上一个字段的值（实测踩过：
        # 第二行的批号被填成了 2026-06-16）。
        # 「失效日期」必须在表里：这张单把「生产日期 2026-05-18 / …」和
        # 「失效日期 2031-05-17 / …」印在相邻两行，早期只列了「有效期至」，
        # 于是失效日期那串被当成了生产日期的续值，按「/」切出 5 个日期，
        # 与 3 行明细对不上，生产日期与效期一起没回填（实测多行样本踩过）。
        _LABELS = (r"批号|批次号|LOT|生产日期|生产批号|失效日期|有效期至|有效期|"
                   r"注册证号|医保编码|商品ID|合计数量|本页小计|合计金额|备注")
        segments = {}
        for m in re.finditer(r"(" + _LABELS + r")\s*[:：]?\s*", tail_txt):
            start = m.end()
            nxt = re.search(_LABELS, tail_txt[start:])
            end = start + nxt.start() if nxt else len(tail_txt)
            key = m.group(1)
            val = tail_txt[start:end].strip("  :：")
            if key in segments:  # 同一标签出现多次时保留最完整的一条
                if len(val) > len(segments[key]):
                    segments[key] = val
            else:
                segments[key] = val

        def take_multi(*labels):
            for lb in labels:
                if lb in segments:
                    seg = segments[lb]
                    return [v.strip() for v in re.split(r"\s*/\s*", seg) if v.strip()]
            return []

        # 批号必须是「字母数字混排或纯数字但不能是日期形状」的串：
        # 纯数字批号（如 20260506）在纸面上真实存在，所以不能一律排除数字，
        # 但 YYYY-MM-DD 这种显然是日期，被误抓到批号列会直接污染入库表。
        def _is_lot(v):
            return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-]{2,}", v)) and not _DATE.fullmatch(v)

        lots = [v for v in take_multi("批号", "批次号", "LOT") if _is_lot(v)]
        mfgs = take_multi("生产日期")
        exps = take_multi("有效期至", "有效期", "失效日期")

        for field, vals, label in (("lot_no", lots, "批号"), ("mfg_date", mfgs, "生产日期"),
                                   ("expiry_date", exps, "有效期至")):
            if not vals:
                continue
            if len(vals) != len(lines):
                warnings.append("[表外小字] %s 在表格下方识别到 %d 个，与明细行数 %d 不匹配，未回填"
                                % (label, len(vals), len(lines)))
                continue
            for ln, v in zip(lines, vals):
                if ln.get(field):
                    continue
                if field == "lot_no":
                    ln[field] = v
                else:
                    d, prec = _date(v)
                    if d:
                        ln[field] = d
                        if prec == "month":
                            ln.setdefault("date_precision", {})[field] = "month"
                warnings.append("[表外回填] %s %s 取自表格下方小字「%s」，请核对"
                                % (ln.get("product_name"), label, v))
        lic = segments.get("注册证号") or ""
        if lic:
            # 注意不能写成字符类 [国械注准许进]——那只匹配「一个」汉字，
            # 结果整串被截成「进20153172425」。注册证是「国械注进/准/许 + 年份 + 编号」。
            m = re.search(r"国[械药]注[进准许]\s*[A-Za-z0-9]{6,}", lic.replace(" ", ""))
            if m:
                for ln in lines:
                    if not ln.get("license_no_raw"):
                        ln["license_no_raw"] = m.group(0)

    # missing 必须在回填之后重算：先算出来的话，表外补上的批号/日期仍会被标成缺失，
    # 审核台就会把已经补齐的行也标红，人一旦习惯忽略红标，红标就失去意义了。
    for ln in lines:
        low = [k for k in ("spec", "lot_no", "mfg_date", "expiry_date", "license_no_raw")
               if not ln.get(k)]
        if low:
            ln["missing"] = low
        else:
            ln.pop("missing", None)

    _validate(doc, lines, warnings)
    if doc.get("total_amount") is None:
        doc["total_amount"] = round(sum(l["amount"] for l in lines if l.get("amount")), 2)
    doc["total_qty"] = round(sum(l["quantity"] for l in lines if l.get("quantity")), 4)

    # 图像质量：取中位置信度，低于 0.80 判 fair，低于 0.65 判 poor
    cs = sorted(b["score"] for b in boxes)
    med = cs[len(cs) // 2]
    doc["image_quality"] = "good" if med >= 0.90 else ("fair" if med >= 0.75 else "poor")
    if doc["image_quality"] != "good":
        warnings.append("[图像质量] OCR 中位置信度 %.2f，判为 %s；该单据建议整单人工复核"
                        % (med, doc["image_quality"]))
    return doc, lines, warnings


def _validate(doc, lines, warnings):
    """只告警不改数，与 read_template._validate 同口径。"""
    for ln in lines:
        q, p, a = ln.get("quantity"), ln.get("unit_price"), ln.get("amount")
        if p is not None and q and a is not None:
            calc = round(float(p) * float(q), 2)
            if abs(calc - float(a)) > 0.01:
                warnings.append(
                    "[金额不平] %s 单价x数量=%.2f 与单据金额 %.2f 差 %.2f；按硬约束禁止反算，原样保留待人工确认"
                    % (ln.get("product_name"), calc, float(a), calc - float(a)))
        if not ln.get("lot_no"):
            warnings.append("[缺批号] %s 未识别到批号，入库表该列为空将阻断提交" % ln.get("product_name"))
        if not ln.get("expiry_date"):
            warnings.append("[缺效期] %s 未识别到有效期至，入库表该列为空将阻断提交" % ln.get("product_name"))


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "photo_extracted.json"))
    ap.add_argument("--debug", default=None, help="把重建出的行列落到该目录，便于核对")
    a = ap.parse_args(argv)
    doc, lines, warns = extract(a.image, a.profile, debug_dir=a.debug)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"document": doc, "lines": lines, "warnings": warns}, f, ensure_ascii=False, indent=1)
    print("明细 %d 行，告警 %d 条 -> %s" % (len(lines), len(warns), a.out))
    for w in warns[:12]:
        print("  !", w)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))

