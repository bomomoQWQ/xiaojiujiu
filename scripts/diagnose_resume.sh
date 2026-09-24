#!/usr/bin/env bash
# 诊断：为什么 fleet 实例都不健康、为什么前端连不上 astrbot 的 WS。
set -u

echo "=== 1) fleet 状态原文 ==="
curl -s --max-time 10 http://127.0.0.1:8800/fleet/status | head -c 900
echo
echo
echo "=== 2) 实例自己的日志（舰队里每个子进程一个文件）==="
docker exec xxj-runtime-fleet sh -c 'ls -la /logs/ 2>/dev/null | head; ls /data/ | head -12'
echo "  --- 取一个人的日志尾部 ---"
docker exec xxj-runtime-fleet sh -c 'tail -20 /logs/default-friendmessage-qq01.log 2>/dev/null || echo "(没有 /logs)"'

echo
echo "=== 3) astrbot-test 有没有在听 6199 ==="
docker exec astrbot-test sh -c 'ss -ltn 2>/dev/null | head -12 || netstat -ltn 2>/dev/null | head -12 || echo "(没有 ss/netstat)"'
echo "  --- 从舰队容器连它试试 ---"
docker exec xxj-runtime-fleet python3 -c "
import socket
for host,port in (('astrbot',6199),):
    try:
        s=socket.create_connection((host,port),timeout=5); s.close(); print('  %s:%s 通' % (host,port))
    except Exception as e:
        print('  %s:%s 不通 -> %s' % (host,port,e))
"

echo
echo "=== 4) 前端配的连哪个地址 ==="
docker inspect xxj-onebot --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -iE "ws|url|host|port|target|token" | head -20
echo "  --- 前端的挂载/命令行 ---"
docker inspect xxj-onebot --format '{{.Path}} {{.Args}}'
docker inspect xxj-onebot --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'

echo
echo "=== 5) astrbot 的平台配置（aiocqhttp 监听什么）==="
docker exec -u 0 astrbot-test python3 -c "
import json
c=json.load(open('/AstrBot/data/cmd_config.json',encoding='utf-8-sig'))
print(json.dumps(c.get('platform'), ensure_ascii=False, indent=2)[:900])
"
