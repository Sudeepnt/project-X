"""Fetching user-supplied URLs without opening an SSRF hole.

Only http(s) on standard ports. The connection is made by a guarded network backend that
resolves the hostname itself, refuses if *any* address is private, loopback, link-local,
multicast or reserved (cloud metadata endpoints live there), and then connects to the vetted IP.
Because the check and the connection use the same resolution, DNS rebinding can't slip an
internal address in between. TLS still verifies the original hostname. Redirects are followed by
hand so every hop is checked; bodies are size-capped and time-limited.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpcore

from projectx.kernel.errors import ValidationFailed

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 4
_ALLOWED_PORTS = (80, 443, 8080, 8443)


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    text: str


def is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return is_public_address(str(ip.ipv4_mapped))
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


async def resolve_public(host: str, port: int) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValidationFailed(f"Couldn't find {host}") from exc
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses or not all(is_public_address(a) for a in addresses):
        raise ValidationFailed("That address points to a private network")
    return addresses


class _GuardedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await resolve_public(host, port)
        return await self._inner.connect_tcp(
            addresses[0], port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.AsyncNetworkStream:
        raise ValidationFailed("Unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValidationFailed("Only public http(s) addresses can be fetched")
    if parts.port is not None and parts.port not in _ALLOWED_PORTS:
        raise ValidationFailed("That port is not allowed")
    try:
        if not is_public_address(parts.hostname):
            raise ValidationFailed("That address points to a private network")
    except ValueError:
        pass  # a hostname, not a literal IP: checked at connect time


async def fetch_public(url: str, *, timeout_s: float = 10.0, user_agent: str = "ProjectX/0.1") -> FetchResult:
    timeouts = {"connect": timeout_s, "read": timeout_s, "write": timeout_s, "pool": timeout_s}
    async with httpcore.AsyncConnectionPool(
        network_backend=_GuardedBackend(), ssl_context=ssl.create_default_context(), max_connections=4, retries=0
    ) as pool:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            _check_url(current)
            headers = [
                (b"User-Agent", user_agent.encode()),
                (b"Accept", b"text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"),
            ]
            async with pool.stream("GET", current, headers=headers, extensions={"timeout": timeouts}) as response:
                header_map = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in response.headers}
                if response.status in (301, 302, 303, 307, 308) and "location" in header_map:
                    current = urljoin(current, header_map["location"])
                    continue
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_stream():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        break
                    chunks.append(chunk)
                content_type = header_map.get("content-type", "")
                charset = "utf-8"
                if "charset=" in content_type:
                    charset = content_type.split("charset=")[-1].split(";")[0].strip() or "utf-8"
                try:
                    text = b"".join(chunks).decode(charset, errors="replace")
                except LookupError:
                    text = b"".join(chunks).decode("utf-8", errors="replace")
                return FetchResult(url=current, status=response.status, content_type=content_type, text=text)
    raise ValidationFailed("Too many redirects")
