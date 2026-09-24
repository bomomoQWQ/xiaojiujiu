"""从舰队容器里对 astrbot:6199 做一次原始 WebSocket 握手，看返回码。
101 = 接受；401/403 = token 不匹配或其它的拒绝原因。只读探测。
"""
import base64
import os
import socket

HOST, PORT = "astrbot", 6199
KEY = base64.b64encode(os.urandom(16)).decode()


def handshake(token: str | None, path: str = "/ws") -> str:
    request = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s:%d\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n" % (path, HOST, PORT, KEY)
    )
    if token:
        request += "Authorization: Bearer %s\r\n" % token
    request += "\r\n"
    with socket.create_connection((HOST, PORT), timeout=6) as sock:
        sock.sendall(request.encode())
        sock.settimeout(6)
        try:
            data = sock.recv(400)
        except socket.timeout:
            return "(没有响应，超时)"
    return data.decode("utf-8", "replace").split("\r\n")[0]


for label, token in (("正确 token", "test-frontend-token"),
                     ("错误 token", "wrong-token"),
                     ("不带 token", None)):
    print("  %-12s -> %s" % (label, handshake(token)))
