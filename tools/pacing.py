# -*- coding: utf-8 -*-
"""拉单限速：把「翻页」变成有节奏的、有上限的、单进程串行的行为。

用户 2026-11-23（m09105）：「禁止爬虫类，会导致封禁」。

这条要求不是道德表态，是**技术约束**。牙e 与梅苗苗的订单接口确实是公开的
HTTP 接口（浏览器里点一下「下一页」就是这个请求），正常翻页不违规；真正会
触发封禁的是爬虫特征：高频、并发、无上限翻页、失败重试轰炸、遍历你没权限的
路径。所以这里的做法不是「不许联网」，而是给联网加上三样东西：

  1. 节奏：每翻一页强制 sleep PAGE_GAP 秒（默认 1.2s，约等于人手点下一页）；
  2. 上限：硬上限 MAX_PAGES_HARD 页（默认 40 页），传更大的值也只跑这么多；
     订单再多就让人按月份分开拉，不要一次扫全站；
  3. 单进程串行：本模块只服务单线程顺序调用，**不提供任何并发接口**，
     也不对失败做重试——失败就抛，让人看见，而不是偷偷再打一遍。

为什么不做指数退避重试：重试在风控眼里就是「被打回来还继续打」，是最典型的
爬虫行为。宁可这一次拉失败、人看一眼再手动重跑。
"""

import os
import sys
import time

# 每页之间固定间隔（秒）。可用环境变量覆盖，但下限锁死在 MIN_GAP。
PAGE_GAP = float(os.environ.get("PLATFORM_PAGE_GAP", "1.2"))
MIN_GAP = 0.5

# 硬上限：任何调用方传再大的 max_pages 也不会超过这个数。
MAX_PAGES_HARD = int(os.environ.get("PLATFORM_MAX_PAGES_HARD", "40"))

# 单次运行里已经翻了多少页（只为日志与自检，不做并发控制——
# 本模块假定调用方是单线程顺序翻页，多线程同时调 list_orders 本身就是违规用法）。
_stats = {"pages": 0, "sleep_s": 0.0}


def clamp_pages(max_pages):
    """把调用方想要的页数夹到硬上限内。返回实际允许的页数。"""
    try:
        n = int(max_pages or 0)
    except (TypeError, ValueError):
        n = MAX_PAGES_HARD
    if n <= 0:
        n = MAX_PAGES_HARD
    return min(n, MAX_PAGES_HARD)


def gap():
    """实际使用的间隔。低于 MIN_GAP 的配置一律抬到 MIN_GAP。"""
    g = PAGE_GAP
    try:
        g = float(g)
    except (TypeError, ValueError):
        g = 1.2
    if g < MIN_GAP:
        g = MIN_GAP
    return g


def between_pages(page, total_hint=None, tag=""):
    """发第 page 页请求**之前**调用。

    第一页不等待——还没构成「连续请求」。之后的每一页都先等 gap 秒再发，
    这样即使最后一页是空页（服务端已经没有数据了），也照样有间隔，
    不会出现「连打两次」的瞬间。
    """
    g = 0.0
    if page > 1:
        g = gap()
        time.sleep(g)
    _stats["pages"] += 1
    _stats["sleep_s"] += g
    if os.environ.get("PLATFORM_PAGE_VERBOSE"):
        sys.stderr.write("[限速] %s 第 %d 页后等待 %.1fs%s\n"
                         % (tag or "拉单", page, g,
                            ("（共 %s 页）" % total_hint) if total_hint else ""))
    return g


def stats():
    return dict(_stats)


def reset():
    _stats["pages"] = 0
    _stats["sleep_s"] = 0.0


def summary():
    """一行话，给界面/日志用。"""
    return "本次共翻 %d 页、限速等待 %.1f 秒（每页间隔 %.1fs，硬上限 %d 页）" % (
        _stats["pages"], _stats["sleep_s"], gap(), MAX_PAGES_HARD)
