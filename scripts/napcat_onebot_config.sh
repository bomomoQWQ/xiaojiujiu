#!/usr/bin/env bash
# NapCat 的 OneBot 连接配置（反向 WS 目标与 token）。
set -u
cd /home/bomomo/astrbot_test/napcat-test-data/config
for f in onebot11_3640344731.json onebot11.json napcat_3640344731.json; do
  [ -f "$f" ] || continue
  echo "=== $f ==="
  python3 - "$f" <<'PY'
import json, sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
net = data.get("network") if isinstance(data.get("network"), dict) else data
print(json.dumps(net, ensure_ascii=False, indent=2)[:800])
PY
done
