"""按 OneBot v11 反向 WS 规范握手：带 X-Self-ID / X-Client-Role。
顺便把「是否被接受」和 AstrBot 是否打印"适配器已连接"对上。
"""
import base64
import os
import socket

HOST, PORT = "astrbot", 6199


def handshake(self_id: str | None) -> str:
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        "GET /ws HTTP/1.1\r\n"
        "Host: %s:%d\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Authorization: Bearer test-frontend-token\r\n" % (HOST, PORT, key)
    )
    if self_id:
        request += "X-Self-ID: %s\r\nX-Client-Role: Universal\r\n" % self_id
    request += "\r\n"
    with socket.create_connection((HOST, PORT), timeout=6) as sock:
        sock.sendall(request.encode())
        sock.settimeout(6)
        try:
            data = sock.recv(400)
        except socket.timeout:
            return "(超时)"
    return data.decode("utf-8", "replace").split("\r\n")[0]


for label, self_id in (("带 X-Self-ID=10001", "10001"), ("不带 X-Self-ID", None)):
    print("  %-20s -> %s" % (label, handshake(self_id)))
