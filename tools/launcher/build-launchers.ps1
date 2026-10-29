#requires -version 5.1
# Build native .exe launchers for the existing .bat entry points.
# The exe files are thin launchers: they call the installed Python / PowerShell
# scripts in place, so editing a .py or .ps1 does not require rebuilding.
param(
    [switch]$SkipAgentHub
)
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$project = Split-Path -Parent (Split-Path -Parent $here)
$templatePath = Join-Path $here 'Launcher.cs.template'

$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) {
    $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $csc)) {
    throw 'csc.exe not found (.NET Framework 4.x is required).'
}
if (-not (Test-Path -LiteralPath $templatePath)) {
    throw "Template not found: $templatePath"
}

$pigIcon = Join-Path $project 'assets\pig.ico'
# AgentHub 是可选的外部仓库：AGENTHUB_DIR 优先，否则取本仓库的兄弟目录。
$agenthubRoot = if ($env:AGENTHUB_DIR) {
    Join-Path $env:AGENTHUB_DIR 'scripts'
} else {
    Join-Path (Split-Path -Parent $project) 'AgentHub\scripts'
}
$wantAgentHub = (-not $SkipAgentHub) -and
    (Test-Path -LiteralPath (Join-Path $agenthubRoot 'agent-hub.ps1'))

# Small icon for the AgentHub launcher, extracted once from PowerShell.
$agenthubIcon = Join-Path $here 'agent-hub.ico'
if ($wantAgentHub -and -not (Test-Path -LiteralPath $agenthubIcon)) {
    try {
        Add-Type -AssemblyName System.Drawing
        $psExe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($psExe)
        if ($icon) {
            $stream = [IO.File]::Create($agenthubIcon)
            try { $icon.Save($stream) } finally { $stream.Close() }
        }
    } catch {
        Write-Warning "Cannot create agent-hub.ico: $($_.Exception.Message)"
    }
}

$targets = @(
    @{ Mode = 'panel'; Title = 'AI 反代面板'; Desc = 'AI 反代网关可视面板'; Root = $project; Script = 'gateway_ui.py'; Out = (Join-Path $project 'AI反代面板.exe'); Icon = $pigIcon; Gui = $true },
    @{ Mode = 'switch'; Title = 'AI 反代开关'; Desc = '打开 AI 反代面板或命令行菜单'; Root = $project; Script = 'gateway_ui.py'; Out = (Join-Path $project 'AI反代开关.exe'); Icon = $pigIcon; Gui = $true },
    @{ Mode = 'checkin'; Title = 'WorkBuddy + 豆包 每日签到'; Desc = '执行 WorkBuddy 与豆包每日签到'; Root = $project; Script = 'daily_checkin.py'; Out = (Join-Path $project '每日签到.exe'); Icon = $pigIcon; Gui = $false },
    @{ Mode = 'status'; Title = 'WorkBuddy + 豆包 签到状态'; Desc = '查看 WorkBuddy 与豆包签到状态'; Root = $project; Script = 'daily_checkin.py'; Out = (Join-Path $project '查看签到状态.exe'); Icon = $pigIcon; Gui = $false },
    @{ Mode = 'installtask'; Title = '安装每日签到任务'; Desc = '注册 AI每日签到 计划任务'; Root = $project; Script = 'install_checkin_task.ps1'; Out = (Join-Path $project '安装每日签到任务.exe'); Icon = $pigIcon; Gui = $false },
    @{ Mode = 'agenthub'; Title = 'AgentHub'; Desc = 'AgentHub 共享技能与规则脚本'; Root = $agenthubRoot; Script = 'agent-hub.ps1'; Out = (Join-Path $agenthubRoot 'agent-hub.exe'); Icon = $agenthubIcon; Gui = $false }
)

if (-not $wantAgentHub) {
    $targets = @($targets | Where-Object { $_.Mode -ne 'agenthub' })
    Write-Host '[skip] AgentHub 启动器：未找到 agent-hub.ps1（可选，用 AGENTHUB_DIR 指定）'
}

$template = [IO.File]::ReadAllText($templatePath, [Text.Encoding]::UTF8)
$utf8Bom = New-Object System.Text.UTF8Encoding($true)
$work = Join-Path $env:TEMP ('aiproxy-launcher-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null

try {
    foreach ($t in $targets) {
        $source = $template
        $source = $source.Replace('__MODE__', $t.Mode)
        $source = $source.Replace('__TITLE__', $t.Title)
        $source = $source.Replace('__DESC__', $t.Desc)
        $source = $source.Replace('__ROOT__', $t.Root)
        $source = $source.Replace('__SCRIPT__', $t.Script)
        $product = if ($t.Mode -eq 'agenthub') { 'AgentHub' } else { 'AI Proxy Hub' }
        $source = $source.Replace('__PRODUCT__', $product)

        $cs = Join-Path $work ($t.Mode + '.cs')
        [IO.File]::WriteAllText($cs, $source, $utf8Bom)

        $cscArgs = @(
            '/nologo',
            ('/target:' + $(if ($t.Gui) { 'winexe' } else { 'exe' })),
            '/platform:anycpu',
            '/optimize+',
            ('/out:' + $t.Out)
        )
        if ($t.Icon -and (Test-Path -LiteralPath $t.Icon)) {
            $cscArgs += ('/win32icon:' + $t.Icon)
        }
        $cscArgs += $cs

        & $csc @cscArgs
        if ($LASTEXITCODE -ne 0) {
            throw "csc failed for $($t.Out)"
        }
        $info = Get-Item -LiteralPath $t.Out
        Write-Host ('[ok] {0,-24} {1,8:N1} KB' -f $info.Name, ($info.Length / 1KB))
    }
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host 'Done. The .bat files are untouched and remain usable as fallback.'
