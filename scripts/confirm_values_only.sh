#!/usr/bin/env bash
# 确认改动只落在价值观上，没有任何 silence/drive/utility 级别的调参。
set -u
FLEET=/home/bomomo/astrbot_test/fleet.yml
echo "=== 不该存在的调参项 ==="
if grep -nE 'CR_SILENCE__|CR_DRIVE__|CR_UTILITY__|COOLDOWN_SECONDS|MAX_CONTACTS' "$FLEET"; then
  echo "  !! 发现配置级调参"
else
  echo "  ✓ 无：只改了价值观"
fi
echo
echo "=== fleet.yml 里的 CR_VALUES ==="
grep -n 'CR_VALUES' "$FLEET"
echo
echo "=== runtime 代码默认值有没有被我改过 ==="
cd /home/bomomo/astrbot_test/src/xiaojiujiu
git log --oneline -1
echo -n "  SilenceConfig 默认 restraint_gain: "
grep -A6 'class SilenceConfig' runtime/src/companion_runtime/config.py | grep -E 'restraint_gain|base:' | head -2
echo -n "  DriveConfig 默认 tau_restraint: "
grep -E 'tau_restraint_seconds' runtime/src/companion_runtime/config.py | head -1
