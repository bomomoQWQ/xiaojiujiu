param(
  [string]$Source = "F:\理解痞老板",
  [string]$Destination = "E:\companion_runtime_backup"
)
$ErrorActionPreference = "Stop"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$staging = Join-Path $Destination ".staging-$stamp"
$final = Join-Path $Destination "snapshot-$stamp"
New-Item -ItemType Directory -Force -Path $staging | Out-Null
$items = @("runtime", "training", "astrbot_plugin_companion_runtime", "scripts", "archive", "RECOVERY.md", "README.md", "内源主动型长期陪伴AI_Runtime_完整架构设计.md", ".gitignore")
foreach ($item in $items) {
  $src = Join-Path $Source $item
  if (Test-Path $src) { Copy-Item -LiteralPath $src -Destination $staging -Recurse -Force }
}
if (Test-Path (Join-Path $Source ".git")) {
  git -C $Source bundle create (Join-Path $staging "workspace.bundle") --all
}
$hashes = Get-ChildItem -LiteralPath $staging -File -Recurse | ForEach-Object {
  [PSCustomObject]@{ Path = $_.FullName.Substring($staging.Length + 1); SHA256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
}
$hashes | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $staging "SHA256.json") -Encoding utf8
Move-Item -LiteralPath $staging -Destination $final
Get-ChildItem -LiteralPath $Destination -Directory -Filter "snapshot-*" | Sort-Object Name -Descending | Select-Object -Skip 5 | Remove-Item -Recurse -Force
Write-Output $final
