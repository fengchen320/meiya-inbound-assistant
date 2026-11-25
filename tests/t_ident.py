# -*- coding: utf-8 -*-
"""身份歧义消歧回归（2026-11-24，用户 m12444 上传后遗留的 IDENT_MULTI）。

跑法：python tmp/t_ident.py

本地产品库里有 24 组「同一身份挂多条档案」（按行算 44 条）：注册证与规格相同，
只是物品名不同（啄木鸟 P1/P3/P33/P95 工作尖、大/中/小号托盘、大小棉棒）。
还有 43 个身份键的注册证与规格都是空、只剩品牌。这两种都可能把货静默入错，
而入库表看起来完全正常 —— 所以要在 build() 里按住：

  1. 同身份多条档案，物品名恰好对上一条 → 用那一条（并留 [消歧] 证据）；
  2. 同身份多条档案，名字也对上多条 → 仍然判歧义，内部编号留空阻断；
  3. 身份只剩品牌、且物品名对不上 → 不许命中，判待建档；
  4. 身份只剩品牌、物品名对上 → 允许命中（那是唯一可靠的确认）。
"""
import io, json, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_import

ok = True


def chk(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print("%-30s %-34s %s" % (label, got, "OK" if good else "WANT " + str(want)))


def line(name, spec, lic, brand="测试牌"):
    return {"product_name": name, "spec": spec, "brand": brand, "license_no": lic,
            "unit": "盒", "quantity": 1, "unit_price": 10.0, "amount": 10.0,
            "lot_no": "L1", "expiry_date": "2028-01-01", "manufacturer": "测试厂"}


def make(doc_no, lines):
    d = {"document": {"doc_no": doc_no, "doc_date": "2026-04-01",
                      "supplier_raw": "测试供应商甲"},
         "lines": lines}
    p = os.path.join(os.path.join(ROOT, "tmp"), doc_no + "_extracted.json")
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    return p


def lib_of(by_ident, names=None):
    """构造 build() 认的 4 元组 lib：mapping / by_ident / used / idx。"""
    used = set()
    for hits in by_ident.values():
        for _, inner in hits:
            if inner:
                used.add(str(inner))
    return ({}, by_ident, used, {"cats": {}, "pending": {}, "names": names or {}})


def run(path, lib):
    doc, rows, warns, prods = build_import.build(path, lib=lib, fixes=False)
    item_no = rows[0][1] if rows else "NOROW"
    return item_no, " ".join(warns), prods


# ── 1. 同身份两条档案，物品名对上一条：应当唯一命中 ──
LIC1, SPEC1, BR1 = "川械注准20259301", "1支/盒", "测试牌"
ident1 = build_import.identity_key(LIC1, SPEC1, BR1)
by1 = {ident1: [("10301", 9001), ("10302", 9002)]}
names1 = {ident1: {"10301": "测试 工作尖 P1", "10302": "测试 工作尖 P3"}}
no1, w1, _ = run(make("TESTIDENT0001", [line("测试 工作尖 P3", SPEC1, LIC1)]),
                 lib_of(by1, names1))
chk("名字唯一对上→命中", no1, "10302")
chk("留了消歧证据", "[消歧]" in w1, True)
chk("没报歧义", "[歧义]" in w1, False)

# ── 2. 同身份两条档案，名字也一样：程序无从判断，必须继续阻断 ──
ident2 = build_import.identity_key("川械注准20259302", "2支/盒", "测试牌")
by2 = {ident2: [("10311", 9011), ("10312", 9012)]}
names2 = {ident2: {"10311": "测试 胶囊", "10312": "测试 胶囊"}}
no2, w2, _ = run(make("TESTIDENT0002", [line("测试 胶囊", "2支/盒", "川械注准20259302")]),
                 lib_of(by2, names2))
chk("同名多条→仍阻断", no2, "")
chk("报了歧义", "[歧义]" in w2, True)

# ── 3. 身份只剩品牌（注册证与规格都空）、名字对不上：不许命中 ──
ident3 = build_import.identity_key("", "", "测试牌B")
by3 = {ident3: [("10321", 9021)]}
names3 = {ident3: {"10321": "测试 完全不同的东西"}}
no3, w3, _ = run(make("TESTIDENT0003", [line("测试 另一样东西", "3支/盒", "")]),
                 lib_of(by3, names3))
chk("品牌-only 名字不符→不命中", no3, "")
chk("判成待建档", "[待建档]" in w3, True)

# ── 4. 身份只剩品牌、名字对上：那是唯一可靠的确认，应当命中 ──
ident4 = build_import.identity_key("", "", "测试牌C")
by4 = {ident4: [("10331", 9031)]}
names4 = {ident4: {"10331": "测试 有名字的东西"}}
no4, w4, _ = run(make("TESTIDENT0004", [line("测试 有名字的东西", "", "", brand="测试牌C")]),
                 lib_of(by4, names4))
chk("品牌-only 名字对上→命中", no4, "10331")
chk("没有误报待建档", "[待建档]" in w4, False)

for f in os.listdir(os.path.join(ROOT, "tmp")):
    if f.startswith("TESTIDENT") and f.endswith("_extracted.json"):
        os.remove(os.path.join(os.path.join(ROOT, "tmp"), f))

print("ALL_OK", ok)
sys.exit(0 if ok else 1)
