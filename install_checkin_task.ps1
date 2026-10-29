param(
    [string]$At = '',
    [switch]$Remove,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$OutputEncoding = [Text.Encoding]::UTF8

$taskName = 'AI每日签到'
# 脚本和工作目录跟着本文件走：换机器、换克隆目录都不用改脚本。
$workdir = $PSScriptRoot
$script = Join-Path $workdir 'daily_checkin.py'

function Resolve-Python {
    # 依次尝试：显式环境变量 → 本仓库虚拟环境 → PATH → python.exe 同目录的 pythonw。
    $cands = @()
    if ($env:AI_PROXY_PYTHONW) { $cands += $env:AI_PROXY_PYTHONW }
    $cands += (Join-Path $workdir '.venv\Scripts\pythonw.exe')
    $cands += (Join-Path $workdir 'venv\Scripts\pythonw.exe')
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $cands += $cmd.Source }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $cands += (Join-Path (Split-Path $cmd.Source -Parent) 'pythonw.exe')
        $cands += $cmd.Source
    }
    foreach ($c in $cands) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }
    return ''
}
$pythonw = Resolve-Python
$who = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Get-CheckinTaskStatus {
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $t) {
        return [pscustomobject]@{ exists = $false; task = $taskName }
    }
    $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
    $time = ''
    if ($t.Triggers.Count -gt 0 -and $t.Triggers[0].StartBoundary) {
        try { $time = ([datetime]$t.Triggers[0].StartBoundary).ToString('HH:mm') } catch { }
    }
    $lastRun = $null
    $nextRun = $null
    $lastResult = $null
    if ($info) {
        if ($info.LastRunTime -and $info.LastRunTime.Year -gt 2000) {
            $lastRun = $info.LastRunTime.ToString('yyyy-MM-dd HH:mm')
        }
        if ($info.NextRunTime -and $info.NextRunTime.Year -gt 2000) {
            $nextRun = $info.NextRunTime.ToString('yyyy-MM-dd HH:mm')
        }
        $lastResult = [int]$info.LastTaskResult
    }
    $act = $t.Actions | Select-Object -First 1
    [pscustomobject]@{
        exists = $true
        task = $taskName
        state = [string]$t.State
        time = $time
        last_run = $lastRun
        last_result = $lastResult
        next_run = $nextRun
        action = if ($act) { [string]$act.Execute } else { '' }
        arguments = if ($act) { [string]$act.Arguments } else { '' }
    }
}

if ($Status) {
    Get-CheckinTaskStatus | ConvertTo-Json -Compress
    exit 0
}

if ($Remove) {
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $t) {
        Write-Host "计划任务不存在：$taskName"
        Get-CheckinTaskStatus | ConvertTo-Json -Compress
        exit 0
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "已移除计划任务：$taskName"
    Get-CheckinTaskStatus | ConvertTo-Json -Compress
    exit 0
}

if (-not $At) {
    $cur = Get-CheckinTaskStatus
    if ($cur.exists -and $cur.time) {
        $At = $cur.time
    } else {
        $At = '09:00'
    }
}
if ($At -notmatch '^([01]\d|2[0-3]):[0-5]\d$') {
    Write-Host "时间格式应为 HH:mm（00:00–23:59）：$At"
    exit 2
}
if (-not $pythonw) {
    Write-Host "未找到 Python：请安装 Python 3 并加入 PATH，或用 AI_PROXY_PYTHONW 指定 pythonw.exe"
    exit 3
}
if (-not (Test-Path -LiteralPath $script)) {
    Write-Host "找不到签到脚本：$script"
    exit 3
}
$atTime = [datetime]::Today.Add([timespan]::Parse($At))
try {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"' + $script + '" --scheduled') -WorkingDirectory $workdir
    $trigger = New-ScheduledTaskTrigger -Daily -At $atTime
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $who -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'WorkBuddy + 豆包每日签到' -Force | Out-Null
    Write-Host "已注册计划任务：$taskName（每天 $At，错过则尽快补跑）"
} catch {
    Write-Host "PowerShell 注册失败：$($_.Exception.Message)"
    Write-Host "改用 schtasks..."
    $tr = '"' + $pythonw + '" "' + $script + '" --scheduled'
    schtasks.exe /Create /SC DAILY /ST $At /TN $taskName /TR $tr /F
}
Get-CheckinTaskStatus | ConvertTo-Json -Compress
