# -*- coding: utf-8 -*-
"""梅苗苗（mmm920）拉单客户端：订单列表 + 随货单 JSON。

为什么不用「点导出随货单 + 收下载文件」：
Edge 131 上 Browser.setDownloadBehavior 的几种组合都会让同名下载互判 canceled
（allowAndName 存 guid 名、eventsEnabled:true 直接 canceled），只能靠目录快照
轮询去猜文件，偶发失败且无法区分「没下下来」与「下到一半」。而 getOrderExport
的响应体本身就是随货单的完整 JSON（data:[{billNo,shipedDate,details:[...]}]），
一次请求拿全部明细，没有中间态可言。

鉴权：Authorization = 站点 Cookie 里的 token（JWT）。它在 Cookie 里而不是
localStorage 里，能直接 document.cookie 读到，所以由 cdp_token.mjs 之类的
CDP 侧脚本抓一次写进 config/mmm_token.json，Python 侧纯 HTTP 使用。
"""
import io, json, os, re, sys, time, urllib.request, urllib.error

BASE = "https://w.mmm920.com"
LIST_URL = BASE + "/authapi/order/getOrders2"
EXPORT_URL = BASE + "/authapi/order/getOrderExport"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pacing  # noqa: E402  翻页限速（用户要求「禁止爬虫类，会导致封禁」）

TOKEN_FILE = os.path.join(ROOT, "config", "mmm_token.json")
SOURCE_FILE = os.path.join(ROOT, "config", "platform_sources.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")


class MmmError(RuntimeError):
    pass


class MmmAuthError(MmmError):
    """token 缺失或失效：调用方应停下来让用户登录，重试没有意义。"""


def load_token(path=None):
    p = path or TOKEN_FILE
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
        if isinstance(j, str):
            return j
        if j.get("token"):
            return j["token"]
    return os.environ.get("MMM_TOKEN") or ""


def save_token(token, extra=None, path=None):
    p = path or TOKEN_FILE
    os.makedirs(os.path.dirname(p), exist_ok=True)
    d = {"token": token, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    d.update(extra or {})
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    return p


def _post(url, body, token=None, timeout=30):
    tok = token or load_token()
    if not tok:
        raise MmmAuthError("没有梅苗苗 token（config/mmm_token.json 不存在或为空），请先登录")
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", tok)
    req.add_header("Origin", "https://www.mmm920.com")
    req.add_header("Referer", "https://www.mmm920.com/user/my_order")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise MmmAuthError("HTTP %s：梅苗苗登录态失效，请重新登录" % e.code)
        raise MmmError("HTTP %s %s" % (e.code, e.reason))
    try:
        j = json.loads(raw)
    except ValueError:
        raise MmmError("响应不是 JSON：%s" % raw[:200])
    if j.get("isSuccess") is False:
        msg = j.get("message") or j.get("action") or ""
        if re.search(r"登录|token|授权|失效", str(msg), re.I):
            raise MmmAuthError(str(msg))
        raise MmmError("接口返回失败：%s" % (msg or raw[:200]))
    return j


def list_orders(start, end, status=-1, page_size=20, max_pages=50, token=None):
    """按日期区间（YYYY-MM-DD，服务端原生支持）翻页拉订单，返回 [{orderNo, createTime, ...}]。"""
    out, page = [], 1
    # 同 lib_yae：有间隔、有硬上限、不重试。服务端本来支持日期区间，
    # 所以正常一张月单也就 1–3 页，这个上限只是防「参数写错变成全站扫描」。
    pages = pacing.clamp_pages(max_pages)
    while page <= pages:
        pacing.between_pages(page, tag="梅苗苗拉单")
        j = _post(LIST_URL, {"pageIndex": page, "status": status,
                             "pageSize": page_size, "startTime": start, "endTime": end},
                  token=token)
        data = j.get("data") or {}
        lst = data.get("list") or []
        out.extend(lst)
        total_page = int(data.get("totalPage") or 0)
        if not lst or page >= total_page:
            break
        page += 1
    return out


def get_order_export(order_no, token=None):
    """取随货单 JSON，返回 [{companyName,address,billNo,shipedDate,storeName,details:[...]}]。"""
    j = _post(EXPORT_URL, {"orderNo": order_no}, token=token)
    data = j.get("data")
    if isinstance(data, dict):
        data = [data]
    return data or []


def _load_sources():
    if os.path.exists(SOURCE_FILE):
        with open(SOURCE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _date(v):
    """梅苗苗两个日期字段类型不一致：makeDate 是 '20260301' 字符串、overDate 是 20280229 数字，
    统一成 YYYY-MM-DD；无法解析就原样返回，让下游报告 warned 而不是丢字段。"""
    if v is None or v == "":
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d{8}", s):
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:])
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    return s


def to_extracted(export_rows, order_meta=None, platform="mmm"):
    """把 getOrderExport 的响应转成与照片/表格通道完全一致的 {document, lines, warnings}。

    下游 build_import 读的就是这个结构，因此拉单与识别两条路在 build 之前合流，
    不需要为平台单独写一套建档逻辑。
    """
    rows = export_rows or []
    if not rows:
        raise MmmError("随货单为空")
    head = rows[0]
    cfg = (_load_sources().get(platform) or {})
    warnings = []
    # companyName 是收货方（本诊所）；供货方是平台自身，只能来自配置——
    # 单据里没有印供货方名称，靠猜会在美亚建出错误的供应商。
    buyer = head.get("companyName") or cfg.get("buyer")
    supplier = cfg.get("supplier")
    if not supplier:
        warnings.append("[配置缺失] config/platform_sources.json 未配置 %s 的 supplier，"
                        "供货单位留空需人工指定" % platform)
    details = []
    for r in rows:
        details.extend(r.get("details") or [])
    lines = []
    for i, d in enumerate(details, 1):
        qty = d.get("amount")
        price = d.get("taxPrice")
        amt = d.get("taxBalance")
        try:
            if amt in (None, "") and qty not in (None, "") and price not in (None, ""):
                amt = round(float(qty) * float(price), 2)
        except (TypeError, ValueError):
            pass
        lines.append({
            "line_no": i,
            "product_name": (d.get("productName") or "").strip() or None,
            "spec": (d.get("stand") or "").strip() or None,
            "brand": None,
            "manufacturer": (d.get("factory") or "").strip() or None,
            "lot_no": (d.get("batchNo") or "").strip() or None,
            "quantity": _num(qty),
            "unit": (d.get("unit") or "").strip() or None,
            "unit_price": _num(price),
            "amount": _num(amt),
            "mfg_date": _date(d.get("makeDate")),
            "expiry_date": _date(d.get("overDate")),
            "license_no_raw": _strip_license(d.get("passNo")),
            "confidence": None,
            "bbox": None,
            "extras": {"ybbm": d.get("ybbm"), "permit_holder": d.get("permitHolder"),
                       "storage": d.get("chuYunTiaoJian"), "kind": d.get("kind")},
        })
    total = 0.0
    for ln in lines:
        if ln.get("amount") not in (None, ""):
            try:
                total += float(ln["amount"])
            except (TypeError, ValueError):
                pass
    qty_total = 0.0
    for ln in lines:
        if ln.get("quantity") not in (None, ""):
            try:
                qty_total += float(ln["quantity"])
            except (TypeError, ValueError):
                pass
    doc = {
        "doc_type": "随货同行单",
        "doc_no": head.get("billNo") or (order_meta or {}).get("orderNo"),
        "doc_date": _date(head.get("shipedDate")),
        "supplier_raw": supplier,
        "buyer": buyer,
        "warehouse": head.get("storeName"),
        "total_amount": round(total, 2) if total else None,
        "total_amount_cn": None,
        "total_qty": _num(qty_total),
        "image_quality": None,
        "source_file": (order_meta or {}).get("orderNo"),
        "source_sheet": "platform:" + platform,
        "platform_order_no": (order_meta or {}).get("orderNo"),
        "ship_address": head.get("address"),
    }
    if not doc["doc_date"]:
        warnings.append("[缺日期] 随货单没有发货日期（shipedDate 为空）")
    return {"document": doc, "lines": lines, "warnings": warnings}


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else f


def _strip_license(v):
    """passNo 常带括号历史证号，如「国械注进20183170277(原注册证号国械注进20183630277)」。
    主号取括号前的部分，括号内容留在 extras 里备查——注册证是追溯依据，不能整串丢掉。"""
    s = (v or "").strip()
    if not s:
        return None
    m = re.match(r"^([^（(]+)", s)
    return (m.group(1).strip() if m else s) or None


def write_extracted(ext, out_dir, stem=None):
    os.makedirs(out_dir, exist_ok=True)
    stem = stem or _safe(ext["document"].get("doc_no") or "mmm")
    p = os.path.join(out_dir, stem + "_extracted.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(ext, f, ensure_ascii=False, indent=1)
    return p


def _safe(s):
    return re.sub(r"[^0-9A-Za-z_\-]+", "_", str(s))[:60] or "doc"


def pull(start, end, out_dir, platform="mmm", token=None, status=-1, dry_run=False):
    """拉一批订单并存成 extracted.json，返回 ([(orderNo, path, warnings)], skipped)。"""
    orders = list_orders(start, end, status=status, token=token)
    # 目录是「本平台最近一次拉取」的快照位，不是累积仓库。
    # 不清理的后果实测过：按 --pull-from/--pull-to 拉了 1 单，但目录里还躺着上一次
    # 拉的 15 单，下游 run.py 扫目录时把那 15 单又跑了一遍——日期区间等于失效，
    # 而且会重复建档。
    if os.path.isdir(out_dir):
        for fn in os.listdir(out_dir):
            if fn.lower().endswith("_extracted.json"):
                try:
                    os.remove(os.path.join(out_dir, fn))
                except OSError:
                    pass
    res, skipped = [], []
    for o in orders:
        no = o.get("orderNo")
        if not no:
            continue
        # 有些订单（待付款/已取消/纯运费单）点「导出随货单」也是空的。
        # 这里跳过而不是整个批中断——一张空单不该让其余 8 张拉不下来；
        # 但必须把单号记进 skipped 由调用方打印，否则会以为「本来就这么多单」。
        try:
            rows = get_order_export(no, token=token)
            ext = to_extracted(rows, order_meta=o, platform=platform)
        except MmmError as e:
            skipped.append({"orderNo": no, "status": o.get("statusString"),
                            "why": str(e)})
            continue
        p = write_extracted(ext, out_dir, stem=_safe(no))
        res.append((no, p, ext["warnings"]))
    return res, skipped


def main(argv=None):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", required=True)
    ap.add_argument("--to", dest="d_to", required=True)
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "pull"))
    ap.add_argument("--check", action="store_true", help="只验证 token 可用")
    ap.add_argument("--save-token", help="写入 config/mmm_token.json")
    a = ap.parse_args(argv)
    if a.save_token:
        print("token 已写入", save_token(a.save_token))
        return 0
    if a.check:
        j = _post(LIST_URL, {"pageIndex": 1, "status": -1, "pageSize": 1,
                             "startTime": a.d_from, "endTime": a.d_to})
        print("TOKEN_OK isSuccess=%s" % j.get("isSuccess"))
        return 0
    res, skipped = pull(a.d_from, a.d_to, a.out)
    for no, p, w in res:
        print("%s -> %s%s" % (no, p, ("  警告:%d" % len(w)) if w else ""))
    for s in skipped:
        print("跳过 %s（%s）：%s" % (s["orderNo"], s.get("status"), s["why"]))
    print("共 %d 单，跳过 %d 单" % (len(res), len(skipped)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
