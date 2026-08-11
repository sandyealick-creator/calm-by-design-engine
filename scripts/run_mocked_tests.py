#!/usr/bin/env python3
"""Run the complete mocked suite with outbound Python sockets denied.

The guard is installed before pytest is imported, so collection, plugins, and
tests are covered. The only subprocess in the suite is a Node VM harness whose
fetch function is replaced in-process and which is not given a network API.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
from collections.abc import Callable
from typing import Any


class OutboundSocketBlocked(RuntimeError):
    """Raised when the mocked suite attempts a non-loopback connection."""


ATTEMPTS: list[str] = []


def _is_loopback(host: Any) -> bool:
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


def _record_and_block(operation: str) -> None:
    ATTEMPTS.append(operation)
    raise OutboundSocketBlocked(
        f"outbound socket denied during mocked tests ({operation})"
    )


def install_socket_guard() -> Callable[[], None]:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo

    def guarded_connect(sock: socket.socket, address: Any) -> Any:
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            host = address[0] if isinstance(address, tuple) and address else address
            if not _is_loopback(host):
                _record_and_block("socket.connect")
        return original_connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: Any) -> Any:
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            host = address[0] if isinstance(address, tuple) and address else address
            if not _is_loopback(host):
                _record_and_block("socket.connect_ex")
        return original_connect_ex(sock, address)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        host = address[0] if isinstance(address, tuple) and address else address
        if not _is_loopback(host):
            _record_and_block("socket.create_connection")
        return original_create_connection(address, *args, **kwargs)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not _is_loopback(host):
            _record_and_block("socket.getaddrinfo")
        return original_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.create_connection = guarded_create_connection
    socket.getaddrinfo = guarded_getaddrinfo

    def restore() -> None:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        socket.create_connection = original_create_connection
        socket.getaddrinfo = original_getaddrinfo

    return restore


def sanitize_test_environment() -> None:
    os.environ.update(
        {
            "AIRTABLE_API_KEY": "test-airtable-key",
            "GEMINI_API_KEY": "test-gemini-key",
            "WEBHOOK_SECRET": "test-webhook-secret",
            "SESSION_SECRET": "test-session-secret",
            "GEMINI_MODEL": "test-gemini-model",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    os.environ.pop("GHL_ROUTING_WEBHOOK", None)
    os.environ.pop("GHL_CRISIS_WEBHOOK", None)


def verify_guard() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.connect(("192.0.2.1", 9))
    except OutboundSocketBlocked:
        pass
    else:
        raise RuntimeError("socket guard self-test did not block the probe")
    finally:
        probe.close()
    if ATTEMPTS != ["socket.connect"]:
        raise RuntimeError("socket guard self-test produced an unexpected result")
    ATTEMPTS.clear()
    print("socket_guard_self_test=passed")


def main() -> int:
    sanitize_test_environment()
    restore = install_socket_guard()
    try:
        verify_guard()
        import pytest

        pytest_args = sys.argv[1:] or ["-p", "no:cacheprovider", "tests"]
        pytest_status = pytest.main(pytest_args)
    finally:
        restore()

    print(f"outbound_socket_attempts={len(ATTEMPTS)}")
    if ATTEMPTS:
        print("outbound_socket_operations=" + ",".join(ATTEMPTS))
        return 1
    return int(pytest_status)


if __name__ == "__main__":
    raise SystemExit(main())
