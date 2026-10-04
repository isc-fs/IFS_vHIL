#!/usr/bin/env python3
"""The workers' only way out (docs/deploy.md, "Networks"): an HTTP CONNECT
proxy that lets through TLS to an allowlist of hosts and nothing else.

    egress_proxy.py [--port 3128]

The workers and the workspace one-shot sit on an `internal` Docker network
with no route off the host; this proxy is on that network and on one with a
route. git honours `https_proxy`, so firmware clones (`vhil.system build`)
and the workspace's fetch go through it; anything else a worker tries (a
firmware build script, a test, a compromised Renode model) has no route
at all, including the cloud metadata service at 169.254.169.254.

Policy, per CONNECT request:
  - the host must be in VHIL_EGRESS_ALLOW (comma-separated; "github.com"
    matches that name only, ".githubusercontent.com" any subdomain) and the
    port in VHIL_EGRESS_PORTS (default 443);
  - every address the name resolves to must be global unicast: a name that
    resolves to a private, loopback, link-local (metadata), multicast or
    reserved address is refused, so DNS can't point the allowlist inside;
  - the proxy connects to the address it checked, not to the name again.
Plain HTTP requests (GET http://...) are refused: only CONNECT.

Only the Python standard library: runs in the ifs-vhil image, as the
unprivileged user, with a read-only root filesystem.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import socket
import sys
from typing import Awaitable, Callable, Iterable

DEFAULT_ALLOW = "github.com,.githubusercontent.com"
IDLE_S = 300            # a tunnel with no traffic either way for this long is closed
HEADER_TIMEOUT_S = 15   # the CONNECT request line and headers must arrive in this
MAX_HEADER = 8192
MAX_CLIENTS = 128

Resolver = Callable[[str, int], Awaitable[list[str]]]


def log(msg: str) -> None:
    print(f"egress: {msg}", file=sys.stderr, flush=True)


class Policy:
    def __init__(self, hosts: Iterable[str], ports: Iterable[int] = (443,),
                 allow_nonglobal: bool = False):
        self.exact, self.suffixes = set(), []
        for h in hosts:
            h = h.strip().lower().rstrip(".")
            if not h:
                continue
            (self.suffixes.append(h) if h.startswith(".") else self.exact.add(h))
        self.ports = set(ports)
        # Tests only: lets a test reach a listener on 127.0.0.1.
        self.allow_nonglobal = allow_nonglobal

    @classmethod
    def from_env(cls) -> "Policy":
        hosts = os.environ.get("VHIL_EGRESS_ALLOW", DEFAULT_ALLOW).split(",")
        ports = [int(p) for p in os.environ.get("VHIL_EGRESS_PORTS", "443").split(",") if p.strip()]
        return cls(hosts, ports)

    def host_ok(self, host: str, port: int) -> bool:
        host = host.lower().rstrip(".")
        if port not in self.ports:
            return False
        return host in self.exact or any(host.endswith(s) for s in self.suffixes)

    def addr_ok(self, addr: str) -> bool:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return self.allow_nonglobal or (ip.is_global and not ip.is_multicast)


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def parse_connect(head: bytes) -> tuple[str, int]:
    """(host, port) of a `CONNECT host:port HTTP/1.x` request; ValueError otherwise."""
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split()
    if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
        raise ValueError(line[:100])
    target = parts[1]
    if target.startswith("["):                         # [v6]:port
        host, _, rest = target[1:].partition("]")
        port = rest.removeprefix(":")
    else:
        host, _, port = target.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(line[:100])
    return host, int(port)


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await asyncio.wait_for(src.read(65536), IDLE_S)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            dst.close()
        except Exception:  # noqa: BLE001 - already gone
            pass


class Proxy:
    def __init__(self, policy: Policy, resolver: Resolver = system_resolver,
                 max_clients: int = MAX_CLIENTS):
        self.policy, self.resolver = policy, resolver
        self._slots = asyncio.Semaphore(max_clients)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        async with self._slots:
            try:
                await self._handle(reader, writer, peer)
            except Exception as e:  # noqa: BLE001 - one bad client never stops the proxy
                log(f"{peer}: {type(e).__name__}: {e}")
            finally:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _reply(self, writer, status: str) -> None:
        writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()

    async def _handle(self, reader, writer, peer) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT_S)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return
        try:
            host, port = parse_connect(head)
        except ValueError as e:
            log(f"{peer}: refused (not CONNECT): {e}")
            return await self._reply(writer, "405 Method Not Allowed")
        if not self.policy.host_ok(host, port):
            log(f"{peer}: refused {host}:{port} (not allowed)")
            return await self._reply(writer, "403 Forbidden")
        try:
            addrs = await asyncio.wait_for(self.resolver(host, port), HEADER_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError) as e:
            log(f"{peer}: {host}: does not resolve: {e}")
            return await self._reply(writer, "502 Bad Gateway")
        bad = [a for a in addrs if not self.policy.addr_ok(a)]
        if bad or not addrs:
            log(f"{peer}: refused {host}:{port} (resolves to non-global {bad or 'nothing'})")
            return await self._reply(writer, "403 Forbidden")
        upstream = None
        for addr in addrs:
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(addr, port),
                                                  HEADER_TIMEOUT_S)
                break
            except (OSError, asyncio.TimeoutError):
                continue
        if upstream is None:
            log(f"{peer}: {host}:{port}: no address answered")
            return await self._reply(writer, "502 Bad Gateway")
        up_reader, up_writer = upstream
        log(f"{peer}: tunnel {host}:{port} ({addr})")
        await self._reply_tunnel(writer)
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))

    async def _reply_tunnel(self, writer) -> None:
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()


async def serve(policy: Policy, host: str, port: int) -> None:
    proxy = Proxy(policy)
    server = await asyncio.start_server(proxy.handle, host, port, limit=MAX_HEADER)
    log(f"listening on {host}:{port}; allow {sorted(policy.exact)} + "
        f"*{sorted(policy.suffixes)} on ports {sorted(policy.ports)}")
    async with server:
        await server.serve_forever()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=3128)
    a = p.parse_args(argv)
    try:
        asyncio.run(serve(Policy.from_env(), a.host, a.port))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
