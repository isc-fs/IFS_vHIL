"""deploy/egress_proxy.py: the workers' CONNECT-only allowlist proxy."""
import asyncio
import importlib.util

import pytest

from vhil.system import REPO

_spec = importlib.util.spec_from_file_location("egress_proxy", REPO / "deploy" / "egress_proxy.py")
ep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ep)


def test_the_allowlist_matches_names_and_subdomains_on_allowed_ports():
    p = ep.Policy(["github.com", ".githubusercontent.com"], [443])
    assert p.host_ok("github.com", 443) and p.host_ok("GitHub.com.", 443)
    assert p.host_ok("objects.githubusercontent.com", 443)
    assert not p.host_ok("api.github.com", 443)         # exact name only
    assert not p.host_ok("evilgithub.com", 443)
    assert not p.host_ok("githubusercontent.com.evil.org", 443)
    assert not p.host_ok("github.com", 22)


@pytest.mark.parametrize("addr", ["169.254.169.254", "10.0.0.1", "172.17.0.1", "192.168.1.1",
                                  "127.0.0.1", "::1", "fd00::1", "fe80::1", "::ffff:169.254.169.254",
                                  "100.64.0.1", "224.0.0.1", "0.0.0.0"])
def test_non_global_addresses_are_refused(addr):
    assert not ep.Policy(["github.com"]).addr_ok(addr)


def test_global_addresses_pass():
    p = ep.Policy(["github.com"])
    assert p.addr_ok("140.82.121.4") and p.addr_ok("2606:50c0:8000::153")


@pytest.mark.parametrize("line, want", [
    (b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com\r\n\r\n", ("github.com", 443)),
    (b"CONNECT [2606:50c0::1]:443 HTTP/1.1\r\n\r\n", ("2606:50c0::1", 443)),
])
def test_connect_requests_parse(line, want):
    assert ep.parse_connect(line) == want


@pytest.mark.parametrize("line", [b"GET http://github.com/ HTTP/1.1\r\n\r\n",
                                  b"CONNECT github.com HTTP/1.1\r\n\r\n",
                                  b"CONNECT github.com:x HTTP/1.1\r\n\r\n"])
def test_anything_but_connect_is_refused(line):
    with pytest.raises(ValueError):
        ep.parse_connect(line)


async def _through(policy, resolver, request: bytes, payload: bytes = b""):
    """Start an echo upstream and the proxy; send `request` (+ payload once
    tunnelled); return (status line, echoed bytes, upstream connections)."""
    hits = []

    async def echo(r, w):
        hits.append(1)
        w.write(await r.read(100))
        await w.drain()
        w.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]
    proxy = ep.Proxy(policy, resolver=resolver)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(request.replace(b"PORT", str(up_port).encode()))
        await w.drain()
        status = (await r.readuntil(b"\r\n\r\n")).decode().split("\r\n", 1)[0]
        echoed = b""
        if status.endswith("Established"):
            w.write(payload)
            await w.drain()
            echoed = await asyncio.wait_for(r.read(100), 5)
        w.close()
        return status, echoed, len(hits)
    finally:
        server.close()
        upstream.close()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 10))


async def local(host, port):
    return ["127.0.0.1"]


def test_an_allowed_host_is_tunnelled():
    policy = ep.Policy(["git.example"], ports=range(1, 65536), allow_nonglobal=True)
    status, echoed, hits = run(_through(policy, local,
                                        b"CONNECT git.example:PORT HTTP/1.1\r\n\r\n", b"hello"))
    assert status == "HTTP/1.1 200 Connection Established" and echoed == b"hello" and hits == 1


def test_a_host_off_the_list_is_403_and_never_dialled():
    policy = ep.Policy(["git.example"], ports=range(1, 65536), allow_nonglobal=True)
    status, _, hits = run(_through(policy, local, b"CONNECT other.example:PORT HTTP/1.1\r\n\r\n"))
    assert status.startswith("HTTP/1.1 403") and hits == 0


def test_an_allowed_name_that_resolves_to_the_metadata_address_is_403():
    async def rebind(host, port):
        return ["169.254.169.254"]
    policy = ep.Policy(["git.example"], ports=range(1, 65536))
    status, _, _ = run(_through(policy, rebind, b"CONNECT git.example:PORT HTTP/1.1\r\n\r\n"))
    assert status.startswith("HTTP/1.1 403")


def test_a_plain_http_request_is_405():
    policy = ep.Policy(["git.example"], ports=range(1, 65536), allow_nonglobal=True)
    status, _, hits = run(_through(policy, local, b"GET http://git.example:PORT/ HTTP/1.1\r\n\r\n"))
    assert status.startswith("HTTP/1.1 405") and hits == 0


def test_the_policy_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("VHIL_EGRESS_ALLOW", "gitlab.example, .cdn.example")
    monkeypatch.setenv("VHIL_EGRESS_PORTS", "443,8443")
    p = ep.Policy.from_env()
    assert p.host_ok("gitlab.example", 8443) and p.host_ok("a.cdn.example", 443)
    assert not p.host_ok("github.com", 443)
