#!/usr/bin/env python3
"""Native TCP/UDP forwarder bridging the WireGuard VPN interface to OME.

Podman/Docker on macOS publishes container ports via gvproxy, which does NOT
serve the WireGuard utun interface - so the VM cannot reach OME's ports directly
over the tunnel (see VPN-MIGRATION.md). This daemon binds 0.0.0.0 (which *does*
receive tunnel traffic) and relays to OME on 127.0.0.1, where gvproxy works. It
is the role frpc used to play, reimplemented minimally.

Note: socat was tried first but its listening socket does not receive utun
traffic on macOS (a plain-Python socket does), hence this.

Ports (must match start-ome.sh loopback publish + nginx/DNAT on the VM):
  TCP 3355         WebRTC signaling
  UDP 10000-10004  WebRTC media

Launched/torn down by stream-vpn.sh alongside the tunnel. Exits on SIGTERM/SIGINT.
"""
import socket
import sys
import threading

SIG_PORT = 3355
MEDIA_PORTS = range(10000, 10005)  # 10000-10004 inclusive
BACKEND = "127.0.0.1"
BUF = 65536


def _tcp_pipe(src, dst):
    try:
        while True:
            data = src.recv(BUF)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.close()
            except OSError:
                pass


def tcp_server(port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(64)
    while True:
        try:
            client, _ = srv.accept()
        except OSError:
            break
        try:
            upstream = socket.create_connection((BACKEND, port))
        except OSError:
            client.close()
            continue
        threading.Thread(target=_tcp_pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=_tcp_pipe, args=(upstream, client), daemon=True).start()


def udp_server(port):
    """Relay UDP with a per-client backend socket so OME's replies (STUN/DTLS/RTP)
    return to the correct peer. WebRTC needs the browser<->VM 5-tuple stable;
    the internal remap to a per-client socket is invisible to the browser."""
    front = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    front.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    front.bind(("0.0.0.0", port))
    clients = {}  # client_addr -> backend socket
    lock = threading.Lock()

    def reply_loop(back, client_addr):
        while True:
            try:
                data, _ = back.recvfrom(BUF)
            except OSError:
                break
            try:
                front.sendto(data, client_addr)
            except OSError:
                break

    while True:
        try:
            data, client_addr = front.recvfrom(BUF)
        except OSError:
            break
        with lock:
            back = clients.get(client_addr)
            if back is None:
                back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                back.connect((BACKEND, port))
                clients[client_addr] = back
                threading.Thread(
                    target=reply_loop, args=(back, client_addr), daemon=True
                ).start()
        try:
            back.send(data)
        except OSError:
            pass


def main():
    threading.Thread(target=tcp_server, args=(SIG_PORT,), daemon=True).start()
    for p in MEDIA_PORTS:
        threading.Thread(target=udp_server, args=(p,), daemon=True).start()
    print(
        f"stream forwarders up: tcp/{SIG_PORT} + udp/{MEDIA_PORTS.start}-{MEDIA_PORTS.stop - 1} "
        f"-> {BACKEND}",
        flush=True,
    )
    # Block forever; daemon threads do the work. SIGTERM/SIGINT end the process.
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
