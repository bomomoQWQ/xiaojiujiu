# =============================================================================
# 端到端流水线编排（需要 API key 与 llama.cpp 时才会真正调用它们）
# =============================================================================
# 本文件只做编排，不含任何凭据。API key 只从环境变量读。
#
# 用法：
#   $env:DEEPSEEK_API_KEY = "sk-..."
#   pwsh -File scripts/pipeline.ps1 -GenTarget 200 -MaxUsd 1.0
#   pwsh -File scripts/pipeline.ps1 -SkipGen                  # 已有数据
#   pwsh -File scripts/pipeline.ps1 -SkipGen -LlamaCppDir E:\llama.cpp
#
# 每一步都可单独重跑；gen 幂等（断点续跑），split/sft 确定性。
# =============================================================================

param(
    [int]$GenTarget = 0,                        # >0 才执行数据生成
    [double]$MaxUsd = 1.0,
    [switch]$SkipGen,
    [switch]$SkipTrain,
    [switch]$SmokeOnly,
    [string]$ModelPath = "Qwen/Qwen3.5-2B",
    [string]$Config = "configs/lora_bf16.yaml",
    [string]$LlamaCppDir = "",
    [string]$Quants = "Q8_0,Q4_K_M,Q3_K_M"
)

# 只让 cmdlet 的错误终止脚本；外部程序（python）写 stderr 的日志不算失败，
# 它们的成败通过 exit code 判断。
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONPATH = Join-Path $Root "src"
$env:PYTHONIOENCODING = "utf-8"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==== $Message ====" -ForegroundColor Cyan
}

# 统一通过 CLI 入口调用，避免 PowerShell 把 python 的 -m 当成参数名
function Invoke-Qboss([string[]]$QbossArgs) {
    # --log-level ERROR：本工程日志走 stderr，而 Windows PowerShell 5.1 会把
    # 外部程序写 stderr 的普通 INFO 日志渲染成 NativeCommandError 噪音。
    # 需要完整日志时，去掉 --log-level 手动运行同一条命令即可。
    & python -m qboss_training.cli --log-level ERROR @QbossArgs
    if ($LASTEXITCODE -ne 0) {
        throw "命令失败 (exit $LASTEXITCODE)：qboss $($QbossArgs -join ' ')"
    }
}

# --- 1 数据生成 --------------------------------------------------------------
if (-not $SkipGen -and $GenTarget -gt 0) {
    if (-not $env:DEEPSEEK_API_KEY) {
        throw "未设置 DEEPSEEK_API_KEY。本工程绝不从文件读取密钥，请先设置环境变量。"
    }
    Write-Step "1 数据生成（每任务 $GenTarget 条，预算上限 `$$MaxUsd）"

    # 先 dry-run 确认凭据与计划，避免直接烧预算
    Invoke-Qboss @("gen", "--task", "event_eval", "--target", "$GenTarget", "--dry-run")

    foreach ($task in @("event_eval", "emotion_explain")) {
        Invoke-Qboss @(
            "gen", "--task", $task, "--target", "$GenTarget",
            "--max-usd", "$MaxUsd"
        )
    }
} else {
    Write-Host "跳过数据生成（用 -GenTarget 200 启用）。" -ForegroundColor Yellow
}

$Raw = @()
foreach ($task in @("event_eval", "emotion_explain")) {
    $path = "data/raw/$task.jsonl"
    if (Test-Path $path) { $Raw += $path }
}
if ($Raw.Count -eq 0) { throw "data/raw 下没有数据，请先用 -GenTarget 生成。" }

# --- 2 校验 ------------------------------------------------------------------
Write-Step "2 校验数据（--strict，任何失败即中止）"
Invoke-Qboss (@("validate", "--input") + $Raw + @(
    "--strict", "--report", "data/reports/validate_report.json"
))

# --- 3 切分 ------------------------------------------------------------------
Write-Step "3 切分（确定性分层 + 防泄漏）"
Invoke-Qboss (@("split", "--input") + $Raw + @(
    "--output-dir", "data/splits", "--stratify-by", "task,direction"
))

# --- 4 SFT -------------------------------------------------------------------
Write-Step "4 构建聊天 SFT（completion-only 掩码 + token 统计）"
Invoke-Qboss @(
    "sft",
    "--split-dir", "data/splits",
    "--output-dir", "data/sft",
    "--model-path", $ModelPath
)

# --- 5 训练 ------------------------------------------------------------------
if ($SkipTrain) {
    Write-Host "跳过训练（-SkipTrain）。" -ForegroundColor Yellow
} else {
    Write-Step "5a 冒烟测试（4 步：数据/掩码/adapter/保存）"
    Invoke-Qboss @("train", "--config", $Config, "--smoke")

    if (-not $SmokeOnly) {
        Write-Step "5b 正式训练"
        Invoke-Qboss @("train", "--config", $Config)
    }
}

# --- 6/7 评测与合并 ----------------------------------------------------------
$Adapter = "outputs/lora-event-eval"
if (-not $SkipTrain -and (Test-Path $Adapter)) {
    Write-Step "6 评测 adapter（--gate 门禁）"
    Invoke-Qboss @(
        "eval",
        "--input", "data/splits/test.jsonl",
        "--backend", "transformers",
        "--model-path", $ModelPath,
        "--adapter-path", $Adapter,
        "--output-dir", "reports/eval-adapter",
        "--markdown", "reports/eval-adapter.md",
        "--gate"
    )

    Write-Step "7 合并 adapter 到基座"
    Invoke-Qboss @(
        "merge",
        "--adapter-path", $Adapter,
        "--output-dir", "outputs/merged"
    )
}

# --- 8 GGUF ------------------------------------------------------------------
if (Test-Path "outputs/merged") {
    if ($LlamaCppDir) {
        Write-Step "8 GGUF 导出与量化（$Quants）"
        Invoke-Qboss @(
            "gguf",
            "--model-dir", "outputs/merged",
            "--output-dir", "outputs/gguf",
            "--quants", $Quants,
            "--llama-cpp-dir", $LlamaCppDir
        )
    } else {
        Write-Step "8 GGUF 计划（未提供 -LlamaCppDir，仅 dry-run）"
        Invoke-Qboss @(
            "gguf",
            "--model-dir", "outputs/merged",
            "--output-dir", "outputs/gguf",
            "--quants", $Quants,
            "--dry-run"
        )
    }
} else {
    Write-Host "未找到 outputs/merged，跳过 GGUF 导出。" -ForegroundColor Yellow
}

# --- 9 量化后复测 ------------------------------------------------------------
$Q4 = "outputs/gguf/qboss-2b-Q4_K_M.gguf"
if (Test-Path $Q4) {
    Write-Step "9 CPU 基准 + 量化后门禁复测（Q4_K_M）"
    Invoke-Qboss @(
        "bench",
        "--input", "data/splits/test.jsonl",
        "--backend", "llama_cpp",
        "--gguf-path", $Q4,
        "--threads", "2,4",
        "--output-dir", "reports/bench",
        "--markdown", "reports/bench.md"
    )
    Invoke-Qboss @(
        "eval",
        "--input", "data/splits/test.jsonl",
        "--backend", "llama_cpp",
        "--gguf-path", $Q4,
        "--output-dir", "reports/eval-q4",
        "--markdown", "reports/eval-q4.md",
        "--gate"
    )
}

Write-Step "完成"
Write-Host "报告在 reports/，产物在 outputs/。"
Write-Host "提醒：低量化最先坏掉的通常是结构化输出，量化后务必看 schema 合法率。"
