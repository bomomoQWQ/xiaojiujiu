"""在前端容器里用前端自己的 _WebSocket 连接，把真实异常打出来。
这能区分：是它自己的握手代码有问题，还是连上了被对端关掉。
"""
import socket
import sys
import time
import traceback

sys.path.insert(0, "/framework")
from cf.onebot import _WebSocket, OneBotError  # noqa: E402

URL = "ws://astrbot:6199/ws"
HEADERS = {
    "X-Client-Role": "Universal",
    "X-Self-ID": "10001",
    "User-Agent": "cf-onebot/1.0 (xiaojiujiu test frontend)",
    "Authorization": "Bearer test-frontend-token",
}

print("目标:", URL)
try:
    print("  DNS ->", socket.gethostbyname("astrbot"))
except Exception as exc:  # noqa: BLE001
    print("  DNS 失败:", exc)

for attempt in range(1, 4):
    print("--- 第 %d 次 ---" % attempt)
    holder = _WebSocket(URL, HEADERS, timeout=6)
    try:
        holder.connect()
        print("  握手成功，连接保持 10 秒看会不会被关 …")
        holder._sock.settimeout(2)
        closed_at = None
        for i in range(5):
            time.sleep(2)
            try:
                data = holder._sock.recv(200)
                if not data:
                    closed_at = (i + 1) * 2
                    break
                print("    t=%2ds 收到 %d 字节" % ((i + 1) * 2, len(data)))
            except socket.timeout:
                print("    t=%2ds 无数据（连接仍在）" % ((i + 1) * 2))
            except OSError as exc:
                closed_at = (i + 1) * 2
                print("    t=%2ds OSError: %s" % ((i + 1) * 2, exc))
                break
        print("  结果: %s" % ("对端在 %ds 关闭了连接" % closed_at if closed_at else "20 秒内保持"))
    except OneBotError as exc:
        print("  握手被拒:", exc)
    except Exception as exc:  # noqa: BLE001
        print("  其它异常:", type(exc).__name__, exc)
        traceback.print_exc()
    finally:
        try:
            holder.close()
        except Exception:  # noqa: BLE001
            pass
    time.sleep(2)
