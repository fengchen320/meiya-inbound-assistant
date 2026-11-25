# -*- coding: utf-8 -*-
"""Windows 桌面提醒 —— 程序需要人做一件事（拖滑块 / 换网络 / 补供应商资质）时用。

为什么要一个独立小模块：风控（601001）期间浏览器可能停在后台、人也在做别的事，
只在控制台打一行 [风控] 等于没提醒（用户明确要求「下次提示风控可以弹窗提醒我」）。

实现选择：用 PowerShell + WinForms 的 NotifyIcon 气泡，不用 MessageBox。
  1) MessageBox 是模态的，会卡住调用它的进程直到人点确定 —— pipeline 正在跑批量，
     不能因为弹个提示就停在半路；
  2) 气泡 12 秒后自己消失，人不在也能自己收场，不会留下一堆要点掉的窗口；
  3) 全程 Popen 后台跑，主流程一个字都不等。
失败（没装 PowerShell / 被策略禁用）时静默返回 False，绝不能因为「提醒失败」把主流程带崩。
"""
import os
import subprocess
import sys


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def notify(title, text, seconds=12, wait=False):
    """弹一条 Windows 气泡提醒；返回是否成功把命令发出去。

    wait=True 仅供自测用（等进程退出好拿到退出码），正常调用一律不等待。
    """
    if os.name != "nt":
        return False
    title = (title or "提示")[:60]
    text = (text or "")[:240]
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Warning;"
        "$n.Visible = $true;"
        "$n.ShowBalloonTip(%d, %s, %s, [System.Windows.Forms.ToolTipIcon]::Warning);"
        "Start-Sleep -Seconds %d;"
        "$n.Dispose();"
    ) % (int(seconds) * 1000, _ps_quote(title), _ps_quote(text), int(seconds))
    try:
        p = subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                              "-Command", ps],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        print("  [提醒] 弹窗失败（%s），下面是文字提醒：" % e)
        print("  [提醒] %s —— %s" % (title, text))
        return False
    if wait:
        try:
            return p.wait(timeout=seconds + 20) == 0
        except Exception:
            return True
    return True


def notify_blocked(reason, text, seconds=12):
    """只在真的需要人动手时调用；纯日志类信息不要弹，弹多了人就不看了。"""
    return notify(reason, text, seconds=seconds)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ok = notify(sys.argv[1] if len(sys.argv) > 1 else "美亚入库助手",
                sys.argv[2] if len(sys.argv) > 2 else "测试一条提醒",
                seconds=8, wait=True)
    print("NOTIFY_OK" if ok else "NOTIFY_FAIL")