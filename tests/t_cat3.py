# -*- coding: utf-8 -*-
import io, sys
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import build_import as B
cases = [
    ("牙医公园 M10豆瓣成形片套装 XVI型 30*1", ""),
    ("湃生/艾瑞金 胶原蛋白海绵/胶原塞", "φ7*15mm"),
    ("湃生/艾瑞金 胶原蛋白海绵/胶原片", "25*25*2mm"),
    ("朗力生物 口洁宝抑菌含漱液/氯己定漱口水250ml", "250ml"),
    ("金光 代型材料/真空成型片/压膜片（方形）127mm*127mm", "硬 1.5mm（15片/包）"),
    ("联辉 手术刀片（T10A碳钢）10*1", "15#"),
    ("鑫尔乐 一次性使用口镜50*1", "50支/包"),
    ("科尔 聚羧酸锌水门汀/无酸水门汀", "4111420"),
]
bad = 0
for n, s in cases:
    l1 = B.infer_category(n, s)[0]
    ok = bool(l1)
    bad += 0 if ok else 1
    print(("OK  " if ok else "FAIL") + " %-30s -> %s" % (n[:28], l1))
print("--- 回归（不许被新词抢走）")
reg = [("三比 陶瓷托槽", "", "正畸类"), ("氢氧化钙根管消毒剂", "", "修复类"),
       ("种植体系统（含封闭螺丝）", "", "种植类"), ("麦迪康 赛思劲PE薄膜检查手套500*1", "1101C（中号）", "一次性耗材"),
       ("魔芋爽", "", None), ("康田正 吸唾管100*1", "SE01（150*6.5mm，100支/袋）", "一次性耗材"),
       # 水门汀挂到修复类最后一条，不能抢走这两条真实库里的「补牙类」
       ("松风 BeautiCem SA自粘接用树脂水门汀（调拌型）9ml", "PN3216（9ml/支 透明色）", "补牙类"),
       ("齿科粘接用树脂水门汀", "Hand Mixing，9ml，CLEAR", "补牙类"),
       ("丁香油水门汀", "", "修复类")]
for n, s, want in reg:
    got = B.infer_category(n, s)[0]
    ok = got == want
    bad += 0 if ok else 1
    print(("OK  " if ok else "FAIL") + " %-30s -> %s (want %s)" % (n[:28], got, want))
print("ALL_OK", bad == 0)
