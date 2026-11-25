# -*- coding: utf-8 -*-
"""禁令：禁止本程序以任何方式访问美亚美牙（myyun.com）。

用户 2026-11-23 明确定位（m09105）：
  「再次重申禁止读取官方美亚美牙数据」

所以这不是「默认不跑」，而是**代码级短路**：先把 socket 与 urllib 的出口包住，
任何代码路径（包括第三方库）试图连 myyun.com 都会当场抛 MeiyaForbidden，
并往 logs/no_meiya.log 记一笔（时间 + 目标 + 调用栈前几帧）。

用法：三个入口（pipeline.py / run.py / ui.py）在 import 其它本地模块之前
先 `import no_meiya`，装完钩子再往下跑。

为什么要包 socket 而不是只删 lib_meiya.py：
  删文件只挡住了「现在写着的那几处调用」。以后有人为了查一个历史 bug
  从 tools/_legacy_meiya/ 里把 lib_meiya.py 拷回来、或者随手写个
  urllib 去试一下，就又是偷偷对接了。用户的要求是「禁止」，不是「尽量别」。
"""

import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "logs")
LOG_PATH = os.path.join(LOG_DIR, "no_meiya.log")

# 子串匹配即可：myyun.com 同时覆盖 api./www./img. 等所有子域，
# 也覆盖 http://myyun.com.evil.com 这种尾巴——宁可误杀也不放过。
BLOCKED_HOSTS = ("myyun.com",)

_installed = False


class MeiyaForbidden(RuntimeError):
    """有人试图访问美亚美牙。这属于硬性违规，不是可以 catch 掉继续的普通异常。"""


def _is_blocked(host):
    h = str(host or "").strip().lower()
    if not h:
        return False
    return any(b in h for b in BLOCKED_HOSTS)


def _log(target):
    try:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR, exist_ok=True)
        stack = "".join(traceback.format_stack(limit=8)[:-1])
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n[%s] BLOCKED %s\n%s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), target, stack))
    except Exception:
        # 记日志失败绝不能变成「放行」。静默即可。
        pass


def _block(target):
    _log(target)
    raise MeiyaForbidden(
        "禁止访问美亚美牙：%s\n"
        "本程序定位已改为「生成美亚可导入的两张表」，不再读取也不再写入美亚任何数据。\n"
        "如果你确实需要查历史行为，请读 docs/dental-inbound-prompt.md，不要去连接口。"
        % target)


def install():
    """装钩子。重复调用安全。"""
    global _installed
    if _installed:
        return True

    # ── 1. socket 层：create_connection 是所有 TCP 出口的必经之路 ────────
    import socket
    _orig_cc = socket.create_connection

    def _cc(address, *a, **kw):
        try:
            host = address[0] if isinstance(address, (tuple, list)) and address else address
        except Exception:
            host = address
        if _is_blocked(host):
            _block(host)
        return _orig_cc(address, *a, **kw)

    socket.create_connection = _cc

    # create_connection 之外还有直接用 connect 的老代码（urllib 走的就是
    # create_connection，但 http.client 在某些分支会直接 socket.connect）。
    _orig_connect = socket.socket.connect

    def _connect(self, address):
        try:
            host = address[0] if isinstance(address, (tuple, list)) and address else address
        except Exception:
            host = address
        if _is_blocked(host):
            _block(host)
        return _orig_connect(self, address)

    socket.socket.connect = _connect

    # ── 2. urllib 层：给出比「连接被拒」更像人话的报错，并在 DNS 之前拦住 ──
    try:
        import urllib.request as _ur
        _orig_urlopen = _ur.urlopen

        def _urlopen(url, *a, **kw):
            try:
                from urllib.parse import urlparse
                host = urlparse(url if isinstance(url, str) else getattr(url, "full_url", "")).hostname
            except Exception:
                host = ""
            if _is_blocked(host):
                _block(host)
            return _orig_urlopen(url, *a, **kw)

        _ur.urlopen = _urlopen
    except Exception:
        pass

    _installed = True
    return True


def probe():
    """自检：返回 (ok, 说明)。不联网，只验证钩子是否生效。"""
    if not _installed:
        return False, "钩子未安装（先 import no_meiya 并调用 install()）"
    try:
        import socket
        socket.create_connection(("api.myyun.com", 443), timeout=1)
    except MeiyaForbidden:
        return True, "钩子生效：连 api.myyun.com 被拦下"
    except Exception as ex:
        return False, "钩子没生效（换成了别的异常：%s: %s）" % (type(ex).__name__, ex)
    return False, "钩子没生效：居然连上了"


# import 即安装。入口文件不需要记得调 install()。
install()


if __name__ == "__main__":
    ok, msg = probe()
    print(("PROBE_OK " if ok else "PROBE_FAIL ") + msg)
    sys.exit(0 if ok else 1)
