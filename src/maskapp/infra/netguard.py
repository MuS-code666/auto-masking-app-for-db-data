"""外部への通信を遮断する。

業務データを扱うプロセスでは、起動直後に install_network_guard() を呼ぶ。
Python の監査フックを使うため、一度入れると同じプロセス内では解除できない。
モデルの取得など通信が必要な処理は、業務データを扱わない別プロセスで行う（設計書 02 章 2）。

同一端末内の通信（ループバック）は許可する。Windows の asyncio などが内部で使うため。
"""

from __future__ import annotations

import ipaddress
import sys

_installed = False


class NetworkBlockedError(RuntimeError):
    """外部への通信を試みたときの例外。"""


def _is_local_host(host: object) -> bool:
    if host is None:
        return True  # bind 用の getaddrinfo(None, ...)
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _is_local_address(address: object) -> bool:
    if isinstance(address, (str, bytes)):
        return True  # AF_UNIX のパス
    if isinstance(address, tuple) and address:
        return _is_local_host(address[0])
    return False


def _hook(event: str, args: tuple) -> None:
    if event == "socket.connect" or event == "socket.sendto":
        if not _is_local_address(args[1]):
            raise NetworkBlockedError("external network access is disabled in this process")
    elif event == "socket.getaddrinfo":
        if not _is_local_host(args[0]):
            raise NetworkBlockedError("external name resolution is disabled in this process")
    elif event in ("socket.gethostbyname", "socket.gethostbyname_ex", "socket.gethostbyaddr"):
        if not _is_local_host(args[0]):
            raise NetworkBlockedError("external name resolution is disabled in this process")
    elif event == "urllib.Request":
        raise NetworkBlockedError("urllib requests are disabled in this process")


def install_network_guard() -> None:
    global _installed
    if _installed:
        return
    sys.addaudithook(_hook)
    _installed = True


def is_installed() -> bool:
    return _installed
