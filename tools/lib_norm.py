# -*- coding: utf-8 -*-
"""字段归一化工具：日期、注册证、规格拆包、品牌别名、单位白名单。"""
import json
import os
import re
from datetime import datetime, timezone, timedelta

TZ8 = timezone(timedelta(hours=8))
NULL_TOKENS = {"", "/", "--", "-", "无", "N/A", "n/a", "None", "null"}

UNIT_WHITELIST = {"个", "支", "盒", "袋", "包", "瓶", "套", "片", "卷", "对", "台", "件", "枚", "板", "箱", "筒", "副"}

# 内置兜底别名；正式数据在 config/brand_aliases.json（诊所日常叫法，用户可改）
BRAND_ALIAS = {
    "ormcocorporation": "奥美科/ORMCO",
    "ormco": "奥美科/ORMCO",
    "奥美科": "奥美科/ORMCO",
    "dentall": "以色列Cortex",
    "cortex": "以色列Cortex",
    "mani": "马尼/MANI",
    "马尼": "马尼/MANI",
}

BRAND_CFG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "config", "brand_aliases.json")
_BRAND_CACHE = None


def _bkey(s):
    """品牌比较键：小写、去空格、去点号与括号。'Dentall Implants Industries Ltd.' -> dentallimplantsindustriesltd"""
    if s is None:
        return ""
    return re.sub(r"[\s\.\,\u3001\uff0c\u3002\u00b7\uff08\uff09\(\)]+", "", str(s).strip().lower())


def load_brand_aliases(path=None):
    """返回 (by_match, by_license, canonical_set)。by_match/by_license 的键都是 _bkey 归一形式。"""
    global _BRAND_CACHE
    if path is None and _BRAND_CACHE is not None:
        return _BRAND_CACHE
    by_match, by_lic, canon = {}, {}, set()
    try:
        with open(path or BRAND_CFG, encoding="utf-8") as f:
            cfg = json.load(f)
        for b in cfg.get("brands", []):
            c = clean(b.get("canonical"))
            if c is None:
                continue
            canon.add(_bkey(c))
            for m in (b.get("matches") or []):
                by_match[_bkey(m)] = c
            for lic in (b.get("license_nos") or []):
                by_lic[_bkey(lic)] = c
    except FileNotFoundError:
        pass
    res = (by_match, by_lic, canon)
    if path is None:
        _BRAND_CACHE = res
    return res


def norm_brand_by_license(license_no):
    """按注册证号推品牌（品牌与注册证一一对应时的确定性兜底）。"""
    _, by_lic, _ = load_brand_aliases()
    return by_lic.get(_bkey(license_no))


def clean(v):
    """空值归一：/ -- 无 N/A 一律 None（§15 第 16 条）。"""
    if v is None:
        return None
    t = str(v).strip()
    return None if t in NULL_TOKENS else t


def norm_date(raw):
    """返回 (ISO 日期 或 None, 精度 day|month|None)。"""
    t = clean(raw)
    if t is None:
        return None, None
    if re.fullmatch(r"\d{13}", t):
        return datetime.fromtimestamp(int(t) / 1000, TZ8).strftime("%Y-%m-%d"), "day"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        return t, "day"
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "day"
    m = re.fullmatch(r"(\d{4})(\d{2})", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-01", "month"
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", t)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", "day"
    return None, None


def split_license(raw):
    """注册证复合值拆分：主号 / 历史号列表。"""
    t = clean(raw)
    if t is None:
        return None, []
    hist = [m.strip() for m in re.findall(r"原注册证号[：:]\s*([^）)]+)", t)]
    main = re.sub(r"[（(]\s*原注册证号[：:][^）)]*[）)]", "", t).strip()
    return (main or None), hist


def split_package(raw):
    """从规格串里解析包装：'(5包/袋)' -> (5, '包', '袋')。

    必须取**最后一个**「数字+单位/单位」，不能取第一个。
    反例（牙e在线 SC260105135151015050 实测）：规格「SE01（150*6.5mm，100支/袋）」的第一段
    是尺寸 150*6.5mm，老实现把它当成了包装数 150、把「*6.5mm，100支」当成拆包单位，
    建档表导入直接被美亚拒（failCount=1）。真实包装是 100支/袋。
    另外数字与单位之间不允许出现 * ，否则「150*6.5mm」也会被当成「150 × *」。
    """
    t = clean(raw)
    if t is None:
        return None
    pat = r"(\d+)\s*([^\s\d/（）()，,*]{1,4})\s*/\s*([^\s（）()，,/]{1,4})"
    hits = re.findall(pat, t)
    if not hits:
        return None
    n, inner, outer = hits[-1]
    return int(n), inner, outer


def norm_brand(raw):
    """品牌归一：配置文件优先（诊所叫法）> 已是规范名则原样 > 内置兜底。"""
    t = clean(raw)
    if t is None:
        return None
    k = _bkey(t)
    by_match, _, canon = load_brand_aliases()
    if k in by_match:
        return by_match[k]
    if k in canon:
        return t
    return BRAND_ALIAS.get(k, BRAND_ALIAS.get(t.lower(), t))


def unit_ok(u):
    return clean(u) in UNIT_WHITELIST


def money2(v):
    if v is None or v == "":
        return None
    return round(float(v), 2)
