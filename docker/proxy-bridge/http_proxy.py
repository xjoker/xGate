#!/usr/bin/env python3
"""HTTP CONNECT → SOCKS5(h) bridge (stdlib only).

Usage: python3 http_proxy.py <socks5_url> [listen_port] [bind_host]
  socks5_url: socks5://user:pass@host:port  or  socks://...
"""
import asyncio
import struct
import sys
from urllib.parse import urlparse


def _parse(url: str):
    p = urlparse(url.split("#")[0].strip())
    host = p.hostname or ""
    port = p.port or 1080
    user = p.username or ""
    passwd = p.password or ""
    return host, port, user, passwd


async def _socks5_connect(host: str, port: int, s5h: str, s5p: int, user: str, pw: str):
    r, w = await asyncio.open_connection(s5h, s5p)
    w.write(b"\x05\x01\x02" if user else b"\x05\x01\x00")
    await w.drain()
    resp = await r.readexactly(2)
    if resp[1] == 0xFF:
        raise ConnectionError("SOCKS5: no acceptable auth")
    if resp[1] == 2:
        ub, pb = user.encode(), pw.encode()
        w.write(bytes([1, len(ub)]) + ub + bytes([len(pb)]) + pb)
        await w.drain()
        ar = await r.readexactly(2)
        if ar[1] != 0:
            raise ConnectionError("SOCKS5: auth failed")
    hb = host.encode()
    w.write(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", port))
    await w.drain()
    hdr = await r.readexactly(4)
    if hdr[1] != 0:
        raise ConnectionError(f"SOCKS5: connect error {hdr[1]}")
    atyp = hdr[3]
    if atyp == 1:
        await r.readexactly(6)
    elif atyp == 3:
        n = (await r.readexactly(1))[0]
        await r.readexactly(n + 2)
    elif atyp == 4:
        await r.readexactly(18)
    return r, w


async def _relay(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
    try:
        while chunk := await src.read(65536):
            dst.write(chunk)
            await dst.drain()
    except Exception:
        pass
    finally:
        try:
            dst.close()
            await dst.wait_closed()
        except Exception:
            pass


async def _handle(cr, cw, s5h, s5p, user, pw):
    try:
        first = await cr.readline()
        parts = first.decode(errors="replace").split()
        if not parts or parts[0].upper() != "CONNECT":
            cw.close()
            return
        host, _, port_s = parts[1].rpartition(":")
        port = int(port_s)
        while True:
            line = await cr.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        sr, sw = await _socks5_connect(host, port, s5h, s5p, user, pw)
        cw.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await cw.drain()
        await asyncio.gather(_relay(cr, sw), _relay(sr, cw))
    except Exception:
        try:
            cw.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await cw.drain()
        except Exception:
            pass
    finally:
        try:
            cw.close()
        except Exception:
            pass


async def serve(socks5_url: str, listen_port: int, bind_host: str = "127.0.0.1"):
    s5h, s5p, user, pw = _parse(socks5_url)
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, s5h, s5p, user, pw),
        bind_host,
        listen_port,
    )
    print(f"http-proxy {bind_host}:{listen_port} -> socks5 {s5h}:{s5p}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    lport = int(sys.argv[2]) if len(sys.argv) > 2 else 8118
    host = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"
    if not url:
        print("usage: http_proxy.py <socks5_url> [port] [bind_host]", file=sys.stderr)
        sys.exit(1)
    asyncio.run(serve(url, lport, host))
