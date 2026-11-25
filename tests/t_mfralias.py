# -*- coding: utf-8 -*-
"""生产厂家别名表 + 复合注册证匹配回归（m13396 根治）。

背景：单据上的注册证常是复合值（斜杠/空格/逗号连写多个号），而别名表里登记的是单号。
修前 `lic in a["license_nos"]` 精确整串比对，96 行真实复合号全部漏到 raw_only，
DMG 那条 53 字原文原样写进建档表 → 美亚回「生产厂家最多支持输入 50 个字」。
"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import lib_mfr

ok = True


def chk(label, cond, extra=""):
    global ok
    if cond:
        print("  OK   " + label)
    else:
        ok = False
        print("  FAIL " + label + ("  " + str(extra) if extra else ""))


# ---------- ① _lic_tokens / _lic_hit：复合号三种写法 ----------
chk("斜杠复合号拆成两个号",
    lib_mfr._lic_tokens("国械注进20173637237/国械注进20172177237")
    == ["国械注进20173637237", "国械注进20172177237"],
    lib_mfr._lic_tokens("国械注进20173637237/国械注进20172177237"))
chk("斜杠复合号命中子号",
    lib_mfr._lic_hit("国械注进20173637237/国械注进20172177237", "国械注进20172177237"))
# 空格复合号：真实数据里 `国械注进20182170121 国械注进20163632246` 这两个号都还没登记进别名表，
# 所以拿一个已登记号的复合写法验这条路径。
chk("空格复合号（两侧都是完整证号）拆开命中",
    lib_mfr._lic_hit("国械注进20153172425 国械注进20163632246", "国械注进20153172425"))
chk("逗号复合号命中子号",
    lib_mfr._lic_hit("鲁械注准20182170360,鲁日械备20170014号", "鲁日械备20170014"))
chk("号内排版空格不拆（晋晋中械备 20250013 是一个号）",
    lib_mfr._lic_tokens("晋晋中械备 20250013") == ["晋晋中械备20250013"],
    lib_mfr._lic_tokens("晋晋中械备 20250013"))
chk("号内空格照样命中",
    lib_mfr._lic_hit("晋晋中械备 20250013", "晋晋中械备20250013"))
chk("数字边界：2016217412 不得命中 20162174128",
    not lib_mfr._lic_hit("国械注进20162174128", "2016217412"))
chk("不同注册体系不互相命中（准 vs 进，数字相同）",
    not lib_mfr._lic_hit("国械注进20162174128", "国械注准20162174128"))
chk("空值不命中",
    not lib_mfr._lic_hit("", "国械注进20153172425"))
chk("别名证号为空不命中",
    not lib_mfr._lic_hit("国械注进20153172425", ""))

# ---------- ② DMG：复合注册证命中，且名字不超 50 字 ----------
RAW = "德国DMG化学医药集团公司DMG Chemisch-Pharmazeutische Fabrik GmbH"
LIC = "国械注进20173637237/国械注进20172177237"
r = lib_mfr.resolve(RAW, LIC)
chk("DMG 复合号 -> license_exact", r["method"] == "license_exact", r["method"])
chk("DMG 置信度 0.99", r["confidence"] == 0.99, r["confidence"])
chk("DMG 不需要人工复核", r["needs_review"] is False)
chk("DMG 法定名 <= 50 字（美亚建档表列长上限）", len(r["manufacturer"] or "") <= 50,
    len(r["manufacturer"] or ""))
chk("DMG 原文单独保留未被覆盖", r["manufacturer_raw"] == RAW)

r2 = lib_mfr.resolve(RAW, "国械注进20172177237")
chk("只写一个子号也命中", r2["method"] == "license_exact" and r2["manufacturer"] == r["manufacturer"],
    r2["method"])

r3 = lib_mfr.resolve(RAW, None)
chk("无注册证时按别名精确命中（原文归一后等于 matches）", r3["method"] == "alias_exact", r3["method"])
chk("无注册证时同样 <= 50 字", len(r3["manufacturer"] or "") <= 50)

r4 = lib_mfr.resolve("德国DMG化学医药集团", None)
chk("截断原文按前缀补全（prefill_fill 语义，仍需人工确认）",
    r4["method"] == "prefix_fill" and r4["needs_review"] is True, r4["method"])

# ---------- ③ 老条目不受影响 ----------
r5 = lib_mfr.resolve("Dentall Implants Industries L", "国械注进20153172425")
chk("Cortex 注册证仍精确命中", r5["method"] == "license_exact"
    and r5["manufacturer"] == "Cortex Dental Implants Industries Ltd.", r5["method"])
r6 = lib_mfr.resolve("OrmcoCorporation", "国械注进20162174128")
chk("Ormco 无空格写法仍命中", r6["manufacturer"] == "Ormco Corporation", r6["manufacturer"])

# ---------- ④ 别名表不变量：任何 canonical 都不得超过建档表列长上限 ----------
from build_import import MAX_LEN
bad = [(a["canonical"], len(a["canonical"])) for a in lib_mfr.load_aliases()
       if len(a["canonical"]) > MAX_LEN["生产厂家"]]
chk("别名表 canonical 全部 <= 生产厂家上限", not bad, bad)

print()
print("ALL_OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
