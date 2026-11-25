# -*- coding: utf-8 -*-
"""牙e在线（yae920）拉单客户端：订单列表 + 导出入库模板（e看牙版式 .xls）。

为什么要「拉导出文件」而不是自己拼明细：
牙e的订单接口（listOrderData）只返回下单时的快照（商品名/规格/数量/单价/批号），
没有单位、没有分类、没有生产厂家与注册证的规范化结果，而导出入库模板
（exportByOrderIdAndType?type=ekanya）返回的是**牙e自己按入库口径整理好的 17 列**，
已经有 config/template_mapping/yae_ekanya_v1.yaml 能逐列映射。
用它的另一层原因：这份 xls 正是用户在页面上点「导出入库模板」会拿到的同一样东西，
走同一条解析链，就不存在「脚本算出来的单价跟平台对不上」这类扯皮。

鉴权：站点 Cookie 里的 Authorization_web（128 字符密文），**请求头键名要写成
小写开头的 authToken**（不是 Authorization，也不是 Authorization_web）。
Cookie 由 cdp_token_yae.mjs 经 CDP 读一次写进 config/yae_token.json。

日期过滤：listOrderData 的 startTime/endTime/beginTime/orderStatus 参数**全部被服务端
忽略**（实测四种写法返回同一批最新 10 单），所以只能翻页 + 客户端按 createTime 过滤。
翻页靠 _pageNum 递增，直到返回空或与上一页完全重复。
"""
import io, json, os, re, sys, time, urllib.request, urllib.error

LIST_URL = "https://apps.yae920.com/mallGoodsOrder/listOrderData"
EXPORT_URL = ("https://os-wms.yae920.com/service/ordersDto/k3Api/"
              "exportByOrderIdAndType")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pacing  # noqa: E402  翻页限速（用户要求「禁止爬虫类，会导致封禁」）

TOKEN_FILE = os.path.join(ROOT, "config", "yae_token.json")
SOURCE_FILE = os.path.join(ROOT, "config", "platform_sources.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")
PAGE_SIZE = 10          # 服务端写死；传 pageSize=20/50 无效
TPL_EKANYA = "ekanya"   # 睿齿云=ruichiyun / e看牙=ekanya / 牙医管家=yygj / 其他=qita
MAPPING_ID = "yae_ekanya_v1"


class YaeError(RuntimeError):
    pass


class YaeAuthError(YaeError):
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
    return os.environ.get("YAE_TOKEN") or ""


def save_token(token, extra=None, path=None):
    p = path or TOKEN_FILE
    os.makedirs(os.path.dirname(p), exist_ok=True)
    d = {"token": token, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    d.update(extra or {})
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    return p


def _headers(token=None, json_mode=True):
    tok = token or load_token()
    if not tok:
        raise YaeAuthError("没有牙e在线 token（config/yae_token.json 不存在或为空），请先在浏览器登录")
    h = {"authToken": tok, "Origin": "https://www.yae920.com",
         "Referer": "https://www.yae920.com/", "User-Agent": UA}
    if json_mode:
        h["Accept"] = "application/json, text/plain, */*"
    return h


def _get_json(url, token=None, timeout=30):
    req = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise YaeAuthError("HTTP %d：token 失效，请重新登录牙e在线" % e.code)
        raise YaeError("HTTP %d %s" % (e.code, url))
    except urllib.error.URLError as e:
        raise YaeError("连不上 %s：%s" % (url, e))
    try:
        j = json.loads(raw)
    except ValueError:
        raise YaeError("返回不是 JSON（前 200 字）：%s" % raw[:200])
    if isinstance(j, dict) and j.get("success") is False:
        msg = str(j.get("msg") or "")
        # 登录态过期在牙e这里表现为 success:false + 中文提示，不一定是 401
        if re.search(r"登录|token|授权|失效|未授权", msg, re.I):
            raise YaeAuthError(msg)
        raise YaeError(msg)
    return j


def list_page(page_num, token=None):
    """拉一页订单（固定 10 条）。返回 data 列表。"""
    j = _get_json("%s?_pageNum=%d" % (LIST_URL, int(page_num)), token=token)
    return j.get("data") or []


def list_orders(start, end, token=None, max_pages=80, on_page=None):
    """翻页拉全部订单，再按 createTime 的本地日期过滤 [start, end]。

    start/end 是 YYYY-MM-DD。服务端不支持日期参数，所以这里是「先全量再筛」，
    靠 max_pages 兜底防止无上限翻页；返回的订单按时间倒序（平台原序）。
    """
    seen, orders, prev = set(), [], None
    # 翻页要像人手点「下一页」：有间隔、有硬上限、失败不重试。
    # 传再大的 max_pages 也只跑到 pacing.MAX_PAGES_HARD（默认 40 页）；
    # 订单跨度大就按月分开拉，不要一次扫全站。
    pages = pacing.clamp_pages(max_pages)
    for p in range(1, pages + 1):
        pacing.between_pages(p, tag="牙e拉单")
        try:
            data = list_page(p, token=token)
        except YaeError as e:
            # 翻到超出范围时有的接口会给 500/空体，这时当作翻完，而不是整批失败
            if p > 1:
                break
            raise
        if not data:
            break
        nos = tuple(o.get("orderNo") for o in data)
        if nos == prev:      # 服务端忽略 _pageNum 时会重复返回同一页
            break
        prev = nos
        for o in data:
            no = o.get("orderNo")
            if not no or no in seen:
                continue
            seen.add(no)
            orders.append(o)
        if on_page:
            on_page(p, len(orders))
        if len(data) < PAGE_SIZE:
            break
    d1, d2 = _ymd_i(start), _ymd_i(end)
    out = []
    for o in orders:
        d = _ymd_i(_date(o.get("createTime")))
        if not d:
            continue
        if d1 and d < d1:
            continue
        if d2 and d > d2:
            continue
        out.append(o)
    return out


def export_order(order_id, tpl=TPL_EKANYA, dst=None, token=None, timeout=60):
    """下载「导出入库模板」的 .xls（真 BIFF8）。返回落盘路径。"""
    url = "%s?orderId=%s&type=%s" % (EXPORT_URL, order_id, tpl)
    req = urllib.request.Request(url, headers=_headers(token, json_mode=False))
    req.add_header("Accept", "*/*")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            ct = (r.headers.get("content-type") or "").lower()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise YaeAuthError("HTTP %d：token 失效，请重新登录牙e在线" % e.code)
        raise YaeError("导出 HTTP %d（orderId=%s）" % (e.code, order_id))
    except urllib.error.URLError as e:
        raise YaeError("导出发请求失败（orderId=%s）：%s" % (order_id, e))
    if not body:
        # 实测有少数订单（已取消/未发货/纯服务类）导出端点返回 200 + 空体。
        # 这不是 token 问题也不是解析问题，单独报出来，避免被当成「模板没选对」去反复试。
        raise YaeError("该订单没有可导出的入库模板（服务端返回空文件，通常是已取消或未发货）")
    if body[:4] in (b"\xd0\xcf\x11\xe0", b"PK\x03\x04"):
        pass
    elif "json" in ct or body[:1] in (b"{", b"["):
        # 服务端有时用 200 + JSON 报错（例如模板参数不认识）
        raise YaeError("导出返回的不是表格：%s" % body[:200].decode("utf-8", "replace"))
    if dst:
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(body)
    return dst, len(body)


def _date(v):
    """平台日期三种形态：unix 秒 / YYYY-MM-DD / YYYYMMDD 数字。统一成 YYYY-MM-DD。"""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit() and len(v) >= 9):
        try:
            return time.strftime("%Y-%m-%d", time.localtime(int(v)))
        except (ValueError, OSError):
            return None
    s = str(v).strip()
    m = re.match(r"^(\d{4})[-/]?(\d{2})[-/]?(\d{2})", s)
    if m:
        return "%s-%s-%s" % m.groups()
    return None


def _ymd_i(s):
    return int(s.replace("-", "")) if s else None


def _sources():
    if os.path.exists(SOURCE_FILE):
        with open(SOURCE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


# ── 用订单明细（goodsList）校正导出模板 ──────────────────────────────
# 为什么必须做这件事：牙e 的「导出入库模板」偶发把数量/金额印成 0。实测
# MS260127163114015050：订单页写「x2  ¥29.90  总计 ¥59.80」，导出的 xls 里
# 采购数量 0、金额 0、批号空、效期 1900-01-01。用户明确要求「先读页面订单数量
# 更正模板信息（包括单价、总价）」——所以这里拿 order.goodsList 覆盖模板值。
# 每一次更正都要留一条告警：静默改数比不改更危险，人得知道数被动过。
GOODS_QTY_KEYS = ("goodsNum", "shipQty", "sendNum")
# 模板里 Excel 空日期会写成 1900-01-01（序列号 0 的显示值），必须当成「没有」
_EMPTY_DATES = ("", "1900-01-01", "1900/1/1", "1899-12-30")


# 接口里的 goodsName 带状态前缀（实测「【已发货】萨尼 不锈钢K锉/手用锉6*1」），
# 模板印的是干净名字。不剥掉前缀，同名行一条也配不上，退化成按顺序硬配，
# 把 A 的数量写到 B 上（实测把威舞车针的 3 支配给了萨尼 K 锉）。
_STATUS_PREFIX = re.compile(r"^\s*【[^】]{1,8}】\s*")


def _norm_name(s):
    return re.sub(r"[\s（）()【】\[\]/、,，.·\-]+", "", _STATUS_PREFIX.sub("", str(s or ""))).lower()


def _num_or_none(v):
    try:
        if v in (None, ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _goods_qty(g):
    for k in GOODS_QTY_KEYS:
        v = _num_or_none(g.get(k))
        if v is not None:
            return v
    return None


def _goods_price(g):
    """平台价格字段的单位是「分」：salePrice 2990 就是 ¥29.90。"""
    for k in ("salePrice", "paidPrice"):
        v = _num_or_none(g.get(k))
        if v is not None:
            return round(v / 100.0, 4)
    return None


def _goods_amount(g):
    for k in ("goodsMoney", "orderMoney", "payMoney", "adjustMoney"):
        v = _num_or_none(g.get(k))
        if v is not None:
            return round(v / 100.0, 2)
    # 实测很多明细只有 salePrice 没有 goodsMoney（萨尼 K 锉那张单 5 行全是 None），
    # 这时用 数量 × 单价 推金额；单价是分，数量是件，乘积再除 100 就是元。
    q, p = _goods_qty(g), _goods_price(g)
    if q is not None and p is not None:
        return round(q * p, 2)
    return None


def fix_with_goods_list(lines, meta, warnings=None):
    """用订单明细（goodsList）校正导出模板里的数量/单价/金额/批号/效期。

    返回 (lines, notes)。匹配顺序：先按商品名归一化后相等配；配不上的行，
    只有在「模板行数 == 明细条数」时才按剩余顺序补——行数不等说明模板多印或
    少印了行，硬按顺序配会把 A 的数量写到 B 上，那比 0 还糟。

    只覆盖「模板与接口不一致」的字段，一致时一个字都不改（否则正常单据也会
    满屏告警，真问题就被淹掉了）。批号/效期只在模板为空时补：模板一般是仓库
    实物捡货时填的，比接口的预报值更准。
    """
    notes = []
    gl = [g for g in ((meta or {}).get("goodsList") or []) if isinstance(g, dict)]
    if not gl or not lines:
        return lines, notes

    by_name = {}
    for g in gl:
        by_name.setdefault(_norm_name(g.get("goodsName")), g)
    # 按名字分组：同一张单里同名不同批号是常态（萨尼 K 锉一张单 3 行、3 个批号），
    # 用 dict 只留一条会让后两行配不上，退化成「按顺序硬配」，把 A 的数量写到 B 上。
    by_name_multi = {}
    for g in gl:
        by_name_multi.setdefault(_norm_name(g.get("goodsName")), []).append(g)

    # 同名多行怎么挑：先认批号，再认数量，最后才按顺序。只看顺序会配错——
    # 实测萨尼 K 锉一张单 3 行 3 个批号，模板行序与接口行序并不一致，
    # 按顺序配会把 3 支那行的批号留在 2 支那行上（数量改了批号没改）。
    def _pick(ln, cands):
        lot_t = str(ln.get("lot_no") or "").strip()
        q_t = _num_or_none(ln.get("quantity"))
        if lot_t:
            for g in cands:
                gl_ = str(g.get("goodsLot") or "")
                if gl_ and (lot_t in gl_ or gl_ in lot_t):
                    return g, "lot"
        if q_t is not None:
            for g in cands:
                if _goods_qty(g) is not None and abs(_goods_qty(g) - q_t) < 0.001:
                    return g, "qty"
        return cands[0], "order"

    pairs, used, left = [], set(), list(lines)
    for ln in left[:]:
        cands = [g for g in by_name_multi.get(_norm_name(ln.get("product_name")), [])
                 if id(g) not in used]
        if cands:
            g, how = _pick(ln, cands)
            if how == "order" and str(ln.get("lot_no") or "").strip():
                notes.append("[批号需核对] 第 %s 行 %s 模板批号 %s 在订单接口里找不到对应明细，"
                             "数量/单价是按顺序取自「%s」，请核对"
                             % (ln.get("_row"), ln.get("product_name"), ln.get("lot_no"),
                                g.get("goodsName")))
            pairs.append((ln, g))
            used.add(id(g))
            left.remove(ln)
    if len(gl) == len(lines):
        rest = [g for g in gl if id(g) not in used]
        # 名字完全不同（模板印的名字与接口差得远）时只能按顺序配，但必须说，
        # 因为这种配法错位了也看不出来。
        for ln, g in zip(left, rest):
            n1, n2 = _norm_name(ln.get("product_name")), _norm_name(g.get("goodsName"))
            if n1 != n2:
                notes.append("[顺序匹配] 第 %s 行「%s」与接口「%s」名称不一致，"
                             "只能按顺序对应，请核对数量/单价是否这一行"
                             % (ln.get("_row"), ln.get("product_name"), g.get("goodsName")))
        pairs.extend(zip(left, rest))

    for ln, g in pairs:
        row = ln.get("_row")
        name = ln.get("product_name")
        gq, gp, ga = _goods_qty(g), _goods_price(g), _goods_amount(g)
        q = _num_or_none(ln.get("quantity"))
        p = _num_or_none(ln.get("unit_price"))
        a = _num_or_none(ln.get("amount"))
        if gq is not None and (q is None or abs(q - gq) > 0.001):
            ln["quantity"] = gq
            notes.append("[数量更正] 第 %s 行 %s 模板数量 %s → %g（以订单页/接口 goodsNum 为准）"
                         % (row, name, "空" if q is None else ("%g" % q), gq))
        if gp is not None and (p is None or abs(p - gp) > 0.005):
            ln["unit_price"] = gp
            notes.append("[单价更正] 第 %s 行 %s 模板单价 %s → %g（以订单页/接口 salePrice 为准，单位已从分换算）"
                         % (row, name, "空" if p is None else ("%g" % p), gp))
        if ga is not None and (a is None or abs(a - ga) > 0.01):
            ln["amount"] = ga
            notes.append("[金额更正] 第 %s 行 %s 模板金额 %s → %g（以订单页/接口 goodsMoney 为准）"
                         % (row, name, "空" if a is None else ("%.2f" % a), ga))
        lot = str(g.get("goodsLot") or "").strip()
        if lot and not str(ln.get("lot_no") or "").strip():
            ln["lot_no"] = lot
            notes.append("[批号补全] 第 %s 行 %s 模板无批号 → %s（取订单明细 goodsLot）"
                         % (row, name, lot))
        cur_exp = str(ln.get("expiry_date") or "").strip()
        d = _date(g.get("expiryDate")) if g.get("expiryDate") not in (None, "") else ""
        if d and cur_exp in _EMPTY_DATES:
            ln["expiry_date"] = d
            notes.append("[效期补全] 第 %s 行 %s 模板无有效期 → %s（取订单明细 expiryDate）"
                         % (row, name, d))
        # 厂家两个来源打架时必须说：模板印的是「河南省博越」、接口写的是「河南省超亚」，
        # 猜哪个都可能错，出厂家最终以注册证为准。
        f1 = str(ln.get("manufacturer") or "").strip()
        f2 = str(g.get("factory") or "").strip()
        if f1 and f2 and _norm_name(f1) not in _norm_name(f2) and _norm_name(f2) not in _norm_name(f1):
            notes.append("[厂家冲突] 第 %s 行 %s 模板「%s」与订单接口「%s」不一致，"
                         "出厂家以注册证为准，请核对" % (row, name, f1, f2))
    return lines, notes


def to_extracted(xls_path, order_meta=None, platform="yae"):
    """把导出的 xls 解析成标准 extracted.json 结构。

    明细完全交给 config/template_mapping/yae_ekanya_v1.yaml（17 列逐列映射），
    这里只补单据头：单号/日期来自订单元数据，供货方来自行内「供应商」列（平台自己印的），
    行内取不到才退回 config/platform_sources.json——平台单据上没有「供货单位：」标签，
    按文本猜供货方会把买方记成卖方。
    """
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import read_template
    doc, lines, warns = read_template.read(xls_path, MAPPING_ID)
    src = (_sources().get(platform) or {})
    warnings = list(warns or [])

    # 供货方：优先行内「供应商」列（牙e 印的是「四川牙易在线网络科技有限公司」）
    sup_rows = sorted({(ln.get("supplier") or "").strip() for ln in lines} - {""})
    if len(sup_rows) == 1:
        supplier = sup_rows[0]
    elif len(sup_rows) > 1:
        supplier = sup_rows[0]
        warnings.append("[供应商不一致] 同一单据出现了 %d 个供应商（%s），取第一个，请核对"
                        % (len(sup_rows), "、".join(sup_rows)))
    else:
        supplier = src.get("supplier")
        if supplier:
            warnings.append("[供应商配置] 导出表里没有供应商列值，按 config/platform_sources.json 记为「%s」"
                            % supplier)
    if not supplier:
        warnings.append("[缺供货单位] 导出表与配置里都没有供货方，入库前必须人工指定")

    meta = order_meta or {}
    no = meta.get("orderNo")
    d = _date(meta.get("createTime")) or _date(meta.get("orderCreateTime"))
    # 导出模板偶发把数量/金额印成 0（实测 MS260127163114015050：页面 x2 / ¥29.90 /
    # 总计 ¥59.80，模板却是 0 / 0 / 批号空 / 效期 1900-01-01）。以订单页与接口
    # 的 goodsList 为准更正，并把每一处改动写成告警——静默改数比不改更危险。
    # 必须在下面「[0 元行]」判定之前做，否则更正完还会挂一条「0 元」告警。
    lines, fix_notes = fix_with_goods_list(lines, meta, warnings)
    warnings.extend(fix_notes)
    # 明细里的 supplier / warehouse 是列原值，跟 doc 级 supplier_raw 语义重复，
    # 留着会让人以为入库单可以按行换供应商（采购入库是一个单据一个供应商）。
    for ln in lines:
        ln.pop("supplier", None)
        if (ln.get("amount") in (0, 0.0)) and (ln.get("unit_price") in (0, 0.0)) and ln.get("quantity"):
            warnings.append("[0 元行] 第 %s 行 %s 单价与总金额都是 0（赠品或活动品），"
                            "入库单价需人工确认" % (ln.get("_row"), ln.get("product_name")))

    total = 0.0
    qty_total = 0.0
    for ln in lines:
        for k, acc in (("amount", "a"), ("quantity", "q")):
            v = ln.get(k)
            if v in (None, ""):
                continue
            try:
                if acc == "a":
                    total += float(v)
                else:
                    qty_total += float(v)
            except (TypeError, ValueError):
                pass

    doc.update({
        "doc_type": doc.get("doc_type") or "入库模板导出",
        "doc_no": no,
        "doc_date": d,
        "supplier_raw": supplier,
        "buyer": src.get("buyer"),
        "total_amount": round(total, 2) if lines else None,
        "total_amount_cn": None,
        "total_qty": round(qty_total, 4),
        "source_sheet": doc.get("source_sheet") or "exportExcel",
        "source_file": os.path.basename(xls_path),
        "source_sheet_origin": "platform:" + platform,
        "platform_order_no": no,
        "platform_order_status": meta.get("orderStatus"),
        "warehouse": lines[0].get("warehouse") if lines else None,
    })
    if not d:
        warnings.append("[缺日期] 订单元数据里没有 createTime，单据日期留空")
    return {"document": doc, "lines": lines, "warnings": warnings}


def write_extracted(ext, out_dir, stem=None):
    os.makedirs(out_dir, exist_ok=True)
    stem = stem or _safe(ext["document"].get("doc_no") or "yae")
    p = os.path.join(out_dir, stem + "_extracted.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(ext, f, ensure_ascii=False, indent=1)
    return p


def _safe(s):
    return re.sub(r"[^0-9A-Za-z_\-]+", "_", str(s))[:60] or "doc"


def pull(start, end, out_dir, platform="yae", token=None, tpl=TPL_EKANYA,
         keep_xls=True, max_pages=80, dry_run=False):
    """按日期区间拉一批单据，落成 extracted.json。返回 (res, skipped)。"""
    orders = list_orders(start, end, token=token, max_pages=max_pages)
    # 目录是「本平台最近一次拉取」的快照位，不是累积仓库；不清空会让日期区间失效
    if os.path.isdir(out_dir):
        for fn in os.listdir(out_dir):
            if fn.lower().endswith("_extracted.json"):
                try:
                    os.remove(os.path.join(out_dir, fn))
                except OSError:
                    pass
    xls_dir = os.path.join(out_dir, "_xls")
    res, skipped = [], []
    for o in orders:
        no, oid = o.get("orderNo"), o.get("orderId")
        if not no or not oid:
            continue
        try:
            if dry_run:
                res.append((no, None, []))
                continue
            xp = os.path.join(xls_dir, "%s.xls" % _safe(no))
            export_order(oid, tpl=tpl, dst=xp, token=token)
            ext = to_extracted(xp, order_meta=o, platform=platform)
            if not ext["lines"]:
                skipped.append({"orderNo": no, "why": "导出的表格没有明细行"})
                continue
            p = write_extracted(ext, out_dir, stem=_safe(no))
            if not keep_xls:
                try:
                    os.remove(xp)
                except OSError:
                    pass
            res.append((no, p, ext["warnings"]))
        except YaeError as e:
            skipped.append({"orderNo": no, "why": str(e)})
    return res, skipped


def main(argv=None):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", required=True)
    ap.add_argument("--to", dest="d_to", required=True)
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "pull", "yae"))
    ap.add_argument("--tpl", default=TPL_EKANYA,
                    help="模板：ekanya(默认)/ruichiyun/yygj/qita")
    ap.add_argument("--check", action="store_true", help="只验证 token 可用")
    ap.add_argument("--save-token", help="写入 config/yae_token.json")
    a = ap.parse_args(argv)
    if a.save_token:
        print("token 已写入", save_token(a.save_token))
        return 0
    if a.check:
        n = len(list_page(1))
        print("TOKEN_OK 第 1 页 %d 条" % n)
        return 0
    res, skipped = pull(a.d_from, a.d_to, a.out, tpl=a.tpl)
    for no, p, w in res:
        print("%s -> %s%s" % (no, p, ("  警告:%d" % len(w)) if w else ""))
    for s in skipped:
        print("跳过 %s：%s" % (s["orderNo"], s["why"]))
    print("共 %d 单，跳过 %d 单" % (len(res), len(skipped)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
