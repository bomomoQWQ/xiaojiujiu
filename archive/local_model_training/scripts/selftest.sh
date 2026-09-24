#!/usr/bin/env bash
# =============================================================================
# 离线自检：不联网、不需要 GPU、不需要 API key、不下载模型
# =============================================================================
#   bash scripts/selftest.sh
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT/src"
export PYTHONIOENCODING=utf-8

WORK="$ROOT/work_test"
rm -rf "$WORK"
mkdir -p "$WORK"

step() { printf '\n==== %s ====\n' "$1"; }

step "1/7 生成合成夹具（无需网络）"
python -m qboss_training.fixtures make \
  --output "$WORK/fixture.jsonl" --event-eval 30 --emotion-explain 30

step "2/7 校验 schema 与不变量（--strict）"
python -m qboss_training.cli validate \
  --input "$WORK/fixture.jsonl" --strict \
  --report "$WORK/validate_report.json"

step "3/7 验证校验器真的能抓到坏数据"
python -m qboss_training.fixtures make \
  --output "$WORK/bad.jsonl" --event-eval 8 --emotion-explain 8 --include-invalid
if python -m qboss_training.cli validate --input "$WORK/bad.jsonl" --strict; then
  echo "校验器没有抓到故意违规的样本" >&2
  exit 1
fi
echo "  校验器正确拒绝了违规样本"

step "4/7 切分（确定性 + 防泄漏）"
python -m qboss_training.cli split \
  --input "$WORK/fixture.jsonl" \
  --output-dir "$WORK/splits" \
  --stratify-by task,direction

step "5/7 构建聊天 SFT（completion-only，离线模式）"
python -m qboss_training.cli sft \
  --split-dir "$WORK/splits" --output-dir "$WORK/sft" --no-tokenize

step "6/7 评测管线自检（gold 回放应全部 100%）"
python -m qboss_training.cli eval \
  --input "$WORK/splits/test.jsonl" --backend echo --gate \
  --output-dir "$WORK/eval"

step "7/7 CPU 基准与 GGUF 计划自检"
python -m qboss_training.cli bench \
  --input "$WORK/splits/test.jsonl" --backend echo --repeat 2 --warmup 0 \
  --output-dir "$WORK/bench"

python -m qboss_training.cli gguf \
  --model-dir "$WORK/fake_model" --output-dir "$WORK/gguf" --dry-run

step "单元测试"
python -m pytest -q

printf '\n离线自检全部通过。\n产物在 %s（可安全删除）。\n' "$WORK"
cat <<'EOF'

下一步（需要 API key）：
  export DEEPSEEK_API_KEY=sk-...
  qboss gen --task event_eval --target 200 --dry-run   # 先看计划
  qboss gen --task event_eval --target 200
EOF
