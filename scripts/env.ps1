# scripts/env.ps1
#
# Windows 端 SDRplay 环境辅助脚本。
#
# 用法 (PowerShell):
#     . scripts\env.ps1                  # 仅打印当前环境信息
#     . scripts\env.ps1 -Activate        # 把 conda env 'sdr' 加到 PATH 前
#     . scripts\env.ps1 -CheckService    # 检查 SDRplay API Windows service 状态
#     . scripts\env.ps1 -StartService    # 启动 SDRplay API Windows service (管理员)
#
# 注意:
#   - SDRplay API 的 Windows 安装会把 sdrplay_api.dll 放到
#     C:\Program Files\SDRplay\API\x64\, 并把它的目录加入 PATH (系统级)
#   - 它还会注册一个名为 "SDRplay API Service" 的 Windows service, 自动启动
#   - 不需要像 Linux 那样手动跑 sdrplay_apiService daemon

[CmdletBinding()]
param(
    [switch]$Activate,
    [switch]$CheckService,
    [switch]$StartService
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$condaEnvName = "sdr"

function Get-CondaEnvPrefix {
    param([string]$Name)
    $condaPrefix = $env:CONDA_PREFIX
    if (-not $condaPrefix) { return $null }
    if ($condaPrefix -match '[\\/]envs[\\/][^\\/]+$') {
        $base = $condaPrefix -replace '[\\/]envs[\\/][^\\/]+$', ''
    } else {
        $base = $condaPrefix
    }
    $prefix = Join-Path $base "envs\$Name"
    if (Test-Path $prefix) { return $prefix }
    return $null
}

function Write-EnvReport {
    $prefix = Get-CondaEnvPrefix $condaEnvName
    Write-Host "==== SDR env status ====" -ForegroundColor Cyan
    Write-Host ("repo            : {0}" -f $repoRoot)
    Write-Host ("platform        : {0}" -f $env:OS)
    if ($prefix) {
        Write-Host ("conda env       : {0} ({1})" -f $condaEnvName, $prefix) -ForegroundColor Green
    } else {
        Write-Host ("conda env       : '{0}' NOT FOUND (run: conda create -n {0} python=3.12 -y)" -f $condaEnvName) -ForegroundColor Red
    }
    $pythonSource = "not on PATH"
    try {
        $pythonSource = (Get-Command python -ErrorAction Stop).Source
    } catch {}
    Write-Host ("python          : {0}" -f $pythonSource)
    if ($prefix) {
        $soapysdr = Join-Path $prefix "Library\lib\SoapySDR\modules0.8"
        if (Test-Path $soapysdr) {
            Write-Host ("SoapySDR modules: {0}" -f $soapysdr)
            Get-ChildItem $soapysdr -Filter "*.dll" | ForEach-Object { Write-Host ("  - {0}" -f $_.Name) }
        } else {
            Write-Host "SoapySDR modules: (empty - no modules found)" -ForegroundColor Yellow
        }
        $pySoapy = Join-Path $prefix "Lib\site-packages\SoapySDR.py"
        Write-Host ("SoapySDR Python : {0}" -f (if (Test-Path $pySoapy) { "installed" } else { "MISSING" }))
    }
    Write-Host ""
    Write-Host "Plugins env SOAPY_SDR_PLUGIN_PATH = $env:SOAPY_SDR_PLUGIN_PATH"
    Write-Host ""
    # SDRplay API service check
    $svc = Get-Service -Name "SDRplay API*" -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Host ("SDRplay API service : {0} ({1})" -f $svc.Name, $svc.Status)
    } else {
        Write-Host "SDRplay API service : NOT INSTALLED" -ForegroundColor Yellow
        Write-Host "  (装 SDRplay API 的官方 Windows MSI 后会自动注册)"
    }
}

function Set-CondaEnvOnPath {
    $prefix = Get-CondaEnvPrefix $condaEnvName
    if (-not $prefix) { throw "conda env '$condaEnvName' not found" }
    $bin = Join-Path $prefix "Library\bin"
    $scripts = Join-Path $prefix "Scripts"
    $env:PATH = "$bin;$scripts;$env:PATH"
    $env:CONDA_PREFIX = $prefix
    $env:CONDA_DEFAULT_ENV = $condaEnvName
}

function Start-SDRplayService {
    $svc = Get-Service -Name "SDRplay API*" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $svc) { throw "SDRplay API service not installed" }
    if ($svc.Status -eq "Running") {
        Write-Host "Already running." -ForegroundColor Green
        return
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object -TypeName Security.Principal.WindowsPrincipal -ArgumentList $identity
    $adminRole = [Security.Principal.WindowsBuiltInRole]::Administrator
    if (-not $principal.IsInRole($adminRole)) {
        throw "需要管理员权限启动服务。请在 admin PowerShell 重新跑。"
    }
    Start-Service -Name $svc.Name
    Write-Host ("Started: {0}" -f $svc.Name) -ForegroundColor Green
}

if ($Activate) { Set-CondaEnvOnPath }
Write-EnvReport
if ($StartService) { Start-SDRplayService }