param(
    [int]$Port = 8765,
    [switch]$OpenBrowser,
    [string]$BootMode = "menu",
    [string]$WslUser = ""
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$WslRepoRoot = ($RepoRoot -replace '\\', '/') -replace '^([A-Za-z]):', { "/mnt/$($args[0].Groups[1].Value.ToLower())" }
$WslScript = "$WslRepoRoot/tools/qemu/astra_web_qemu.py"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Sony ZV-E10 / Gen 5 ASTRA QEMU 网页触控交互服务" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "[*] 访问地址: http://127.0.0.1:$Port/" -ForegroundColor Green
Write-Host "[*] 仓库路径: $WslRepoRoot (boot-mode=$BootMode)" -ForegroundColor DarkGray
Write-Host "[*] 提示: 按 Ctrl+C 可停止服务并自动清理 QEMU 进程`n" -ForegroundColor DarkGray

if ($OpenBrowser) {
    Start-Job -ScriptBlock {
        param($TargetPort)
        Start-Sleep -Seconds 2
        Start-Process "http://127.0.0.1:$TargetPort/"
    } -ArgumentList $Port | Out-Null
}

$wslArgs = @()
if ($WslUser) {
    $wslArgs += @("-u", $WslUser)
}
$wslArgs += @("-e", "python3", $WslScript, "--port", "$Port", "--boot-mode", $BootMode)
& wsl @wslArgs
