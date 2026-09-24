"""按住一条合规连接 20 秒，发一个 OneBot lifecycle meta 事件，看：
  1) AstrBot 是否打印"适配器已连接"（即服务端认不认这条连接）
  2) 连接能不能保持（被关 vs 稳定）
同时每 5 秒探一次端口是否还在听（前端报的 ConnectionRefused 是不是真的）。
"""
import base64
import json
import os
import socket
import struct
import time


def ws_frame(payload: str) -> bytes:
    data = payload.encode()
    header = bytearray([0x81])
    length = len(data)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    mask = os.urandom(4)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return bytes(header) + masked


key = base64.b64encode(os.urandom(16)).decode()
request = (
    "GET /ws HTTP/1.1\r\nHost: astrbot:6199\r\nUpgrade: websocket\r\n"
    "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n"
    "Authorization: Bearer test-frontend-token\r\n"
    "X-Self-ID: 10001\r\nX-Client-Role: Universal\r\n\r\n" % key)

sock = socket.create_connection(("astrbot", 6199), timeout=6)
sock.sendall(request.encode())
sock.settimeout(6)
print("  握手:", sock.recv(200).decode("utf-8", "replace").split("\r\n")[0])

lifecycle = json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle",
                        "sub_type": "connect", "time": int(time.time()),
                        "self_id": 10001})
sock.sendall(ws_frame(lifecycle))
print("  已发 lifecycle")

for i in range(4):
    time.sleep(5)
    try:
        sock.settimeout(1)
        got = sock.recv(200)
        if got:
            print("  t=%2ds 收到服务端帧 %d 字节" % ((i + 1) * 5, len(got)))
        else:
            print("  t=%2ds 连接被对端关闭" % ((i + 1) * 5))
            break
    except socket.timeout:
        print("  t=%2ds 连接仍然开着（无数据）" % ((i + 1) * 5))
    except OSError as exc:
        print("  t=%2ds 连接出错: %s" % ((i + 1) * 5, exc))
        break
sock.close()
