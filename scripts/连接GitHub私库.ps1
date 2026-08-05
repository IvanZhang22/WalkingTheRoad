param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryUrl,
    [string]$Tag = "v2.1.0"
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$gitCandidates = @(
    "D:\Program Files\Git\cmd\git.exe",
    "C:\Program Files\Git\cmd\git.exe"
)
$gitExe = $gitCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $gitExe) {
    throw "未找到 Git。请先安装 Git for Windows。"
}

Set-Location -LiteralPath $projectDir
& $gitExe remote get-url origin 2>$null
if ($LASTEXITCODE -eq 0) {
    throw "当前仓库已经存在 origin；请先人工核对，脚本不会覆盖。"
}
& $gitExe remote add origin $RepositoryUrl
& $gitExe push -u origin main
& $gitExe push origin $Tag
Write-Host "已推送 main 和 $Tag 标签。请到 GitHub 检查 Actions、Release 和分支保护。" -ForegroundColor Green
