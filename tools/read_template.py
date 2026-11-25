# -*- coding: utf-8 -*-
"""按 template_mapping/*.yaml 把一份供应商表格读成 extracted.json。

这是「读别人的表」这一环的落地：在此之前 extracted_*.json 是手工敲的，
一份单据几行还能对，几百行就没法用了，自动化也就无从谈起。

设计要点：
1. 表头按**名字**定位列，不按列号。供应商模板改版只改列序不改列名的场景很常见，
   按列号会静默错列——错一列就是一整批错账。
2. dst 名先归一到 schema 字段名（qty→quantity、batch_no→lot_no 等），
   下游 build_import.py 只认 schema 名，不认识各模板自己的叫法。
3. 每个 post 处理器都是纯函数，输入输出都是字符串，方便单测。
4. 校验只告警不改数：金额不平、日期精度不足一律保留原值并记 warning，
   绝不反算——反算出来的数字在纸面上找不到出处，事后无法对账。
"""

import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import xlsread
import yamlite

__all__ = ["read", "to_schema", "DST_ALIAS", "POST"]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAPPING_DIR = os.path.join(ROOT, "config", "template_mapping")

# 各模板 dst 名 → schema 字段名。schema 是唯一真源，模板侧的叫法必须收敛到这里。
DST_ALIAS = {
    "qty": "quantity",
    "batch_no": "lot_no",
    "produce_date": "mfg_date",
    "expire_date": "expiry_date",
    "medical_insurance_code": "insurance_code",
    "category_l1": "category_l1",
    "category_l2": "category_l2",
    "category_l3": "category_l3",
}

_SCHEMA_LINE_KEYS = {
    "product_name", "spec", "spec_hint", "package_spec", "brand", "unit", "quantity",
    "unit_price", "amount", "lot_no", "mfg_date", "expiry_date", "license_no",
    "license_no_raw", "manufacturer", "manufacturer_raw", "dosage_form", "insurance_code",
    "product_code", "category_l1", "category_l2", "category_l3", "mah", "storage_condition",
    "prod_license_no", "barcode",
}


# ---------------------------------------------------------------- post 处理器

def p_slash_to_null(v, ctx=None):
    return "" if v.strip() in ("/", "-", "—", "无") else v.strip()


def p_as_text(v, ctx=None):
    """批号必须按文本保留：20260506 当日期会被改成 2026-05-06 甚至丢前导零。"""
    return v.strip()


def p_strip_parenthetical(v, ctx=None):
    """「国械注进20162174128（原注册证号：国械注进20162634128）」→ 主号 + 历史号。"""
    v = v.strip()
    m = re.search(r"[（(]\s*原(?:注册证号|证号|医疗器械注册证号)[:：]?\s*([^）)]+)[）)]", v)
    if m and ctx is not None:
        ctx.setdefault("license_hist", []).append(m.group(1).strip())
    return re.split(r"[（(]", v)[0].strip()


def p_decimal(v, ctx=None, nd=None):
    s = re.sub(r"[^0-9.\-]", "", v or "")
    if not s or s in ("-", "."):
        return None
    try:
        x = float(s)
    except ValueError:
        return None
    return round(x, nd) if nd is not None else x


def p_date_epoch_ms(v, ctx=None):
    """e看牙模板的生产日期是 Excel epoch 毫秒（1777996800000 → 2026-05-06）。"""
    import datetime
    s = re.sub(r"[^0-9]", "", v or "")
    if len(s) >= 12:
        ts = int(s[:13])
        return datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")
    return v.strip()


def p_date_yyyy_mm_dd(v, ctx=None):
    return _norm_date_loose(v, ctx)


def p_date_yyyy_mm_dd_compact(v, ctx=None):
    s = re.sub(r"[^0-9]", "", v or "")
    if len(s) == 8:
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:])
    return _norm_date_loose(v, ctx)


def p_date_yyyy_mm_partial(v, ctx=None):
    """「202506」只到月 → 补 -01，并在 ctx 里标记精度，供下游降级置信度。"""
    s = re.sub(r"[^0-9]", "", v or "")
    if len(s) == 6:
        if ctx is not None:
            ctx.setdefault("date_precision", {})["mfg_date"] = "month"
        return "%s-%s-01" % (s[:4], s[4:])
    return _norm_date_loose(v, ctx)


def _norm_date_loose(v, ctx=None):
    s = (v or "").strip()
    if not s:
        return ""
    if "T" in s or s.replace(".0", "").isdigit() and len(s) > 10:
        return p_date_epoch_ms(s, ctx)
    m = re.match(r"(\d{4})[-/.年]?(\d{1,2})[-/.月]?(\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
        return "%s-%02d-%02d" % (y, int(mo), int(d))
    return s


def p_split_spec(v, ctx=None):
    """「630-0030牵引橡皮圈…(5包/袋)」→ 规格原文保留，包装信息单独给出。"""
    s = v.strip()
    m = re.search(r"[（(]\s*(\d+)\s*(包|支|个|片|贴|袋|只|卷|瓶|盒)\s*/\s*(袋|盒|箱|包|瓶)\s*[）)]", s)
    if m and ctx is not None:
        ctx.setdefault("package", []).append({
            "qty": int(m.group(1)), "unit": m.group(2), "per": m.group(3),
        })
    return s


def p_strip_marks(v, ctx=None):
    return v.strip()


def p_brand_alias(v, ctx=None):
    return v.strip()


def p_manufacturer_alias(v, ctx=None):
    return v.strip()


def p_license_normalize(v, ctx=None):
    return p_strip_parenthetical(v, ctx)


def p_unit_whitelist(v, ctx=None):
    return v.strip()


def p_normalize_spec(v, ctx=None):
    return v.strip()


def p_decimal4(v, ctx=None):
    return p_decimal(v, ctx, 4)


def p_decimal2(v, ctx=None):
    return p_decimal(v, ctx, 2)


def p_identity(v, ctx=None):
    return v.strip()


POST = {
    "slash_to_null": p_slash_to_null,
    "as_text": p_as_text,
    "strip_parenthetical": p_strip_parenthetical,
    "strip_marks": p_strip_marks,
    "brand_alias": p_brand_alias,
    "manufacturer_alias": p_manufacturer_alias,
    "license_normalize": p_license_normalize,
    "unit_whitelist": p_unit_whitelist,
    "normalize_spec": p_normalize_spec,
    "split_spec": p_split_spec,
    "decimal": p_decimal,
    "decimal(4)": p_decimal4,
    "decimal(2)": p_decimal2,
    "date_epoch_ms": p_date_epoch_ms,
    "date_yyyy_mm_dd": p_date_yyyy_mm_dd,
    "date_yyyy_mm_dd_compact": p_date_yyyy_mm_dd_compact,
    "date_yyyy_mm_partial": p_date_yyyy_mm_partial,
    # 下列 post 属于下游（供应商档案、仓库档案）职责，读取阶段原样透传
    "supplier_match_or_create": p_identity,
    "warehouse_match": p_identity,
}


def to_schema(dst_name):
    return DST_ALIAS.get(dst_name, dst_name)


def _apply_post(spec_post, value, ctx):
    if not spec_post:
        return value
    fn = POST.get(spec_post)
    if fn is None:
        raise KeyError("未知 post 处理器 %r（请先在 read_template.POST 里实现）" % spec_post)
    return fn(value, ctx)


def load_mapping(mapping_id):
    path = mapping_id
    if not os.path.isabs(path):
        path = os.path.join(MAPPING_DIR, mapping_id if mapping_id.endswith(".yaml") else mapping_id + ".yaml")
    return yamlite.load(path)


def _parse_inline_map(s):
    """把 "{col: 16, key: total_amount}" 解析成 {"col": "16", "key": "total_amount"}。"""
    body = s.strip().lstrip("{").rstrip("}")
    out = {}
    for part in body.split(","):
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        out[k.strip()] = v.strip()
    return out


def _header_index(header, name):
    """按列名精确定位；找不到再退回「包含」匹配。

    精确优先是必须的：供应商销售清单里同时存在「器械生产许可证号/备案凭证号」和
    「批准文号/注册证号/备案凭证号」，用包含匹配会命错。
    """
    for i, h in enumerate(header):
        if h.strip() == name:
            return i
    for i, h in enumerate(header):
        if name and name in h:
            return i
    return -1


def read(path, mapping_id, sheet=None, strict=True):
    """把一份表格读成 extracted.json 结构。返回 (doc, lines, warnings)。"""
    mp = load_mapping(mapping_id)
    sheet = sheet or mp.get("sheet")
    header_row = int(mp.get("header_row") or 1)

    name, head_block, header, data = xlsread.read_rows(path, sheet=sheet, header_row=header_row)

    warnings = []
    # 单据头区：header_row 之前的行，按 yaml 的 header_block 取 key
    doc = {"doc_type": mp.get("doc_type") or "模板导出", "doc_no": None, "doc_date": None,
           "supplier_raw": None, "buyer": None, "image_quality": None,
           "source_file": os.path.basename(path), "source_sheet": name}

    for r, spec in (mp.get("header_block") or {}).items():
        # yamlite 只支持行内列表，不支持行内 map，所以 "1: {col: 1, key: title}"
        # 会原样变成字符串 "{col: 1, key: title}"，这里就地转成 dict。
        if isinstance(spec, str) and spec.strip().startswith("{"):
            spec = _parse_inline_map(spec)
        if not isinstance(spec, dict):
            continue
        try:
            row_i = int(r) - 1
            col_i = int(spec.get("col", 1)) - 1
            raw = head_block[row_i][col_i] if row_i < len(head_block) else ""
        except (IndexError, TypeError, ValueError):
            continue
        key = spec.get("key")
        m = re.search(r"[:：]\s*(.+)$", raw)
        val = (m.group(1) if m else raw).strip()
        if key and val:
            doc[key] = val

    cols = []
    for c in (mp.get("columns") or []):
        idx = _header_index(header, c["src"])
        if idx < 0:
            msg = "[表头缺失] 模板要求的列「%s」在文件里找不到；该字段全部留空待人工补" % c["src"]
            if strict:
                warnings.append(msg)
            continue
        cols.append((idx, c))

    stop_key = None
    soe = mp.get("stop_on_empty")
    if soe:
        stop_key = soe.split(" ")[0]

    lines = []
    for r_offset, row in enumerate(data):
        row_no = header_row + 1 + r_offset
        if not any(c.strip() for c in row):
            continue
        if stop_key:
            si = _header_index(header, stop_key)
            if si >= 0 and not row[si].strip():
                break

        ctx = {}
        line = {}
        for idx, c in cols:
            raw = row[idx] if idx < len(row) else ""
            dsts = c.get("dst") or []
            primary = to_schema(dsts[0]) if dsts else None
            val = _apply_post(c.get("post"), raw, ctx)
            if primary:
                line[primary] = val
            # 附加 dst（spec_hint / package_spec 等）留原文，供审核台展示
            for extra in dsts[1:]:
                line[to_schema(extra)] = raw.strip()

        if not line.get("product_name"):
            warnings.append("[空品名] 第 %d 行有数据但品名为空，已跳过（可能是小计/合计行）" % row_no)
            continue

        line["_row"] = row_no
        for h in ctx.get("license_hist") or []:
            line.setdefault("license_hist", []).append(h)
        if ctx.get("date_precision"):
            line["date_precision"] = ctx["date_precision"]
        if ctx.get("package"):
            line["package_spec"] = ctx["package"][0]
        lines.append(line)

    _validate(doc, lines, warnings)
    return doc, lines, warnings


def _validate(doc, lines, warnings):
    """只告警不改数：金额不平、缺批号、缺注册证等一律上报审核。"""
    total = 0.0
    for ln in lines:
        q = ln.get("quantity") or 0
        p = ln.get("unit_price")
        a = ln.get("amount")
        if p is not None and q and a is not None:
            calc = round(float(p) * float(q), 2)
            if abs(calc - float(a)) > 0.01:
                warnings.append(
                    "[金额不平] 第 %s 行 %s 单价x数量=%.2f 与单据金额 %.2f 差 %.2f；"
                    "按硬约束禁止反算，原样保留待人工确认"
                    % (ln.get("_row"), ln.get("product_name"), calc, float(a), abs(calc - float(a))))
        if a is not None:
            total += float(a)
        if not (ln.get("lot_no") or "").strip():
            warnings.append("[缺批号] 第 %s 行 %s 无批号，禁止直接入库" % (ln.get("_row"), ln.get("product_name")))
    doc["total_amount_calc"] = round(total, 2)
    doc["line_count"] = len(lines)


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    src, mapping = sys.argv[1], sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else None
    doc, lines, warnings = read(src, mapping)
    result = {"document": doc, "lines": lines, "warnings": warnings}
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print("OK", len(lines), "lines ->", out, "|", len(warnings), "warnings")
    else:
        print(text)


if __name__ == "__main__":
    main()
