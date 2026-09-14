# =============================================================================
# 离线自检：不联网、不需要 GPU、不需要 API key、不下载模型
# =============================================================================
# 用途：克隆仓库后第一件事就是跑它，确认整条数据管线（schema → 校验 →
#       切分 → SFT → 评测 → 基准）都是通的。
#
#   pwsh -File scripts/selftest.ps1
# =============================================================================

# 只让 cmdlet 的错误终止脚本；外部程序（python）写 stderr 的日志不算失败，
# 它们的成败通过 exit code 判断。
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# 保证能 import qboss_training（无需 pip install -e .）
$env:PYTHONPATH = Join-Path $Root "src"
$env:PYTHONIOENCODING = "utf-8"

$Work = Join-Path $Root "work_test"
if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
New-Item -ItemType Directory -Path $Work | Out-Null

function Step($Message) {
    Write-Host ""
    Write-Host "==== $Message ====" -ForegroundColor Cyan
}

Step "1/7 生成合成夹具（无需网络）"
python -m qboss_training.fixtures make `
    --output (Join-Path $Work "fixture.jsonl") `
    --event-eval 30 --emotion-explain 30
if ($LASTEXITCODE -ne 0) { throw "夹具生成失败" }

Step "2/7 校验 schema 与不变量（--strict）"
python -m qboss_training.cli --log-level ERROR validate `
    --input (Join-Path $Work "fixture.jsonl") --strict `
    --report (Join-Path $Work "validate_report.json")
if ($LASTEXITCODE -ne 0) { throw "校验失败：夹具应当 100% 通过" }

Step "3/7 验证校验器真的能抓到坏数据"
# 故意混入违规样本：校验器必须报错（否则测试是假绿）
python -m qboss_training.fixtures make `
    --output (Join-Path $Work "bad.jsonl") `
    --event-eval 8 --emotion-explain 8 --include-invalid
python -m qboss_training.cli --log-level ERROR validate --input (Join-Path $Work "bad.jsonl") --strict
if ($LASTEXITCODE -eq 0) { throw "校验器没有抓到故意违规的样本" }
Write-Host "  校验器正确拒绝了违规样本" -ForegroundColor Green

Step "4/7 切分（确定性 + 防泄漏）"
python -m qboss_training.cli --log-level ERROR split `
    --input (Join-Path $Work "fixture.jsonl") `
    --output-dir (Join-Path $Work "splits") `
    --stratify-by task,direction
if ($LASTEXITCODE -ne 0) { throw "切分失败" }

Step "5/7 构建聊天 SFT（completion-only，离线模式）"
python -m qboss_training.cli --log-level ERROR sft `
    --split-dir (Join-Path $Work "splits") `
    --output-dir (Join-Path $Work "sft") --no-tokenize
if ($LASTEXITCODE -ne 0) { throw "SFT 构建失败" }

Step "6/7 评测管线自检（gold 回放应全部 100%）"
python -m qboss_training.cli --log-level ERROR eval `
    --input (Join-Path $Work "splits\test.jsonl") `
    --backend echo --gate `
    --output-dir (Join-Path $Work "eval")
if ($LASTEXITCODE -ne 0) { throw "评测门禁未通过：gold 回放应当全部达标" }

Step "7/7 CPU 基准与 GGUF 计划自检"
python -m qboss_training.cli --log-level ERROR bench `
    --input (Join-Path $Work "splits\test.jsonl") `
    --backend echo --repeat 2 --warmup 0 `
    --output-dir (Join-Path $Work "bench")
if ($LASTEXITCODE -ne 0) { throw "基准自检失败" }

python -m qboss_training.cli --log-level ERROR gguf `
    --model-dir (Join-Path $Work "fake_model") `
    --output-dir (Join-Path $Work "gguf") --dry-run
if ($LASTEXITCODE -ne 0) { throw "GGUF dry-run 失败" }

Step "单元测试"
python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "测试未全部通过" }

Write-Host ""
Write-Host "离线自检全部通过。" -ForegroundColor Green
Write-Host "产物在 $Work（可安全删除）。"
Write-Host ""
Write-Host "下一步（需要 API key）："
Write-Host ('  $env:DEEPSEEK_API_KEY = "sk-..."')
Write-Host "  qboss gen --task event_eval --target 200 --dry-run   # 先看计划"
Write-Host "  qboss gen --task event_eval --target 200"
