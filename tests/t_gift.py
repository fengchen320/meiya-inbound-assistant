# -*- coding: utf-8 -*-
import sys, io, json, os
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import build_import as bi

cases = [
    ("魔芋爽", "", True),
    ("烟台红富士苹果", "5斤装", True),
    ("香蕉", "", True),
    ("伊利纯牛奶", "250ml*12", True),
    ("一次性使用无菌手术组件/手术包/种植包（低配款）", "II型 低配款", False),
    ("葡萄糖注射液", "500ml", False),      # 含「葡萄」但有「注射液」→ 药品类，不能误杀
    ("吸唾管100*1", "SE01（150*6.5mm，100支/袋）", False),
    ("医用橡胶外科手套", "无粉 7# 中号", False),
    ("康田正 吸唾管", "100支/袋", False),
    ("定制笔筒", "", False),               # 赠品但是耗材类（一次性耗材规则命中）
]
ok = True
for n, s, want in cases:
    got = bi.is_non_medical(n, s)
    flag = "OK " if got == want else "FAIL"
    if got != want: ok = False
    print(flag, n, "|", s, "->", got, "(want", want, ")")
print("ALL_OK", ok)
