#!/usr/bin/env python3
"""IPv6 loopback forwarder: 127.0.0.1:17890 -> [::1]:17890 (mihomo IPv6-pool egress).

根因：Playwright/Camoufox 无法正确应用 IPv6 字面量代理 URL
（http://[::1]:17890），浏览器退回直连 -> 域名解析失败 -> NS_ERROR_UNKNOWN_HOST
（curl / requests 都能用该代理，唯独浏览器不能）。而 mihomo 的 Docker 端口发布
当前只把 17890 绑到 IPv6 loopback（127.0.0.1 不通）。

本 forwarder 在 IPv4 loopback 127.0.0.1:17890 提供入口，转发到 [::1]:17890，
让 panel 继续使用浏览器验证过能用的 IPv4 代理 URL（http://127.0.0.1:17890）。
纯 TCP 中继，多线程，支持多并发。

启动（后台）：
  .venv/Scripts/python.exe -u scripts/ipv6_loopback_forwarder.py
停止：Ctrl+C 或 kill 进程。
"""
import socket
import socketserver
import threading

LISTEN = ("127.0.0.1", 17890)
UPSTREAM = ("::1", 17890)
BUFSIZE = 65536


def _pipe(src, dst):
    """单向搬运，直到 EOF/异常。"""
    try:
        while True:
            data = src.recv(BUFSIZE)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass


class Fwd(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            up = socket.create_connection(UPSTREAM, timeout=10)
        except Exception as e:
            print(f"[fwd] upstream {UPSTREAM} connect failed: {e}", flush=True)
            return
        # 客户端->上游 和 上游->客户端 双向，各一个线程
        t = threading.Thread(target=_pipe, args=(self.request, up), daemon=True)
        t.start()
        _pipe(up, self.request)
        t.join(timeout=5)
        try:
            up.close()
        except Exception:
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    srv = Server(LISTEN, Fwd)
    print(f"[fwd] listening {LISTEN} -> {UPSTREAM}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[fwd] stopped", flush=True)
