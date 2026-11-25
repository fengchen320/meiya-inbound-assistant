# -*- coding: utf-8 -*-
import sys, io
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import lib_norm
cases = [
    ('SE01（150*6.5mm，100支/袋）', (100, '支', '袋')),
    ('(5包/袋)', (5, '包', '袋')),
    ('1101C（中号）', None),
    ('50支/盒(盒式瓶包装)(1.7ml:盐酸阿替卡因68mg,肾上腺素17ug)', (50, '支', '盒')),
    ('6g  668221CN', None),
    ('10*1', None),
    ('0.5cc', None),
    ('(100片/瓶)', (100, '片', '瓶')),
]
out = []
ok = True
for raw, exp in cases:
    got = lib_norm.split_package(raw)
    flag = 'OK' if got == exp else 'FAIL'
    if got != exp: ok = False
    out.append('%s  %-60s -> %s (期望 %s)' % (flag, raw, got, exp))
io.open(os.path.join(ROOT, "tmp", "pkg_test.txt"), 'w', encoding='utf-8').write('\n'.join(out) + '\nALL_OK=' + str(ok))
print('ALL_OK', ok)