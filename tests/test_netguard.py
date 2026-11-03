"""通信遮断の試験（段階0 受入条件）。"""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from maskapp.infra import netguard
from maskapp.infra.netguard import NetworkBlockedError

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_guard_is_active_in_test_process():
    assert netguard.is_installed()


@pytest.mark.parametrize("address", [("192.0.2.1", 80), ("2001:db8::1", 443)])
def test_external_connect_is_blocked(address):
    family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        with pytest.raises(NetworkBlockedError):
            s.connect(address)


def test_external_name_resolution_is_blocked():
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(NetworkBlockedError):
        socket.gethostbyname("example.com")


def test_udp_sendto_external_is_blocked():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(NetworkBlockedError):
            s.sendto(b"x", ("192.0.2.1", 53))


def test_loopback_is_allowed():
    a, b = socket.socketpair()
    with a, b:
        a.sendall(b"ping")
        assert b.recv(4) == b"ping"
    socket.getaddrinfo("localhost", 0)


def test_urllib_is_blocked_in_fresh_process():
    """アプリと同じく起動直後に遮断を入れたプロセスで、urllib による通信が失敗すること。"""
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT / "src")!r})
        from maskapp.infra.netguard import install_network_guard, NetworkBlockedError
        install_network_guard()
        import urllib.request
        try:
            urllib.request.urlopen("https://example.com", timeout=5)
        except NetworkBlockedError:
            print("blocked")
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert result.stdout.strip() == "blocked", result.stderr
