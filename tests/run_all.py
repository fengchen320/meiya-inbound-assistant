# -*- coding: utf-8 -*-
"""跑全部回归用例。

用法：
    python tests/run_all.py            # 全跑
    python tests/run_all.py t_emit     # 只跑名字含 t_emit 的

为什么用子进程而不是 import：每个用例都自己改模块级常量（台账路径、审批文件路径、
本地库路径），同一个进程里跑会互相污染 —— 实测漏跑一个 finally 就让后面三个用例
莫名其妙地 FAIL。
"""
import io
import os
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

# 顺序无关紧要，但按「先底层后链路」排，出错时更容易判断是哪一层坏了
CASES = [
    ("t_pkg.py", "拆包数量/单位"),
    ("t_cat3.py", "三级分类推断"),
    ("t_expiry.py", "效期判定"),
    ("t_gift.py", "非医疗赠品识别"),
    ("t_mfrlen.py", "生产厂家超 50 字截断"),
    ("t_mfralias.py", "厂家别名表与复合注册证"),
    ("t_lib_commit.py", "产品库导入与提交"),
    ("t_ident.py", "身份匹配与名字消歧"),
    ("t_alloc.py", "整批发号不重复"),
    ("t_emit.py", "emit 状态判定"),
    ("t_amb.py", "同身份多档案 → ambiguous"),
    ("t_approve.py", "审批放行 + 重跑不重拉"),
    ("t_holdline.py", "待审批不出建档表 + 单行禁入"),
    ("t_reject.py", "禁止入库全链"),
    ("t_gate.py", "禁令闸门"),
    ("t_one_doc.py", "step_for 状态机"),
    ("t_group.py", "按供应商分组出表"),
    ("t_goods_fix.py", "订单接口对账修补"),
    ("t_recon.py", "对账口径：订单时间/来源/排序"),
    ("t_confirm.py", "确认入库与台账补录"),
]


def main(argv):
    want = [a for a in argv[1:] if not a.startswith("-")]
    cases = [c for c in CASES if not want or any(w in c[0] for w in want)]
    if not cases:
        print("没有匹配的用例：%s" % " ".join(want))
        return 1
    failed = []
    for fn, desc in cases:
        p = os.path.join(HERE, fn)
        if not os.path.exists(p):
            print("%-16s MISSING" % fn)
            failed.append(fn)
            continue
        r = subprocess.run([PY, p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        good = r.returncode == 0
        print("%-16s %-6s %s" % (fn, "PASS" if good else "FAIL", desc))
        if not good:
            failed.append(fn)
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-12:]
            for line in tail:
                print("    | " + line)
    print()
    print("ALL %d/%d" % (len(cases) - len(failed), len(cases)))
    if failed:
        print("失败：%s" % ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
