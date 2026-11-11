# oasc.ps1 —— OAS 实例控制补充入口（oasc = oas command）
# 由 PowerShell 配置文件里的 oasc 函数转发调用，也可在项目根直接 .\oasc.ps1 <命令>
#
# 定位：只提供 `oas` 没有的能力 —— 脚本实例控制、实例状态、快捷调度。
# 后端管理（启动/停止/状态/WebUI/运行任意脚本/MuMu 多开）一律用 `oas`，见 oas help。
# 本文件不代劳启动或停止后端；后端没起时只给出引导，避免与 oas start 的 MuMu 前置步骤重复。
#
# 用法：
#   oasc start <配置名...>            启动指定脚本实例（按现有调度表）
#   oasc stop <配置名...>             停止指定脚本实例
#   oasc stopall                      停止全部运行中的脚本实例（后端保留）
#   oasc disable <配置名...>          禁用实例全部任务并存快照（清空调度表）
#   oasc enable <配置名...>           按快照恢复实例的启用任务
#   oasc solo-explore [配置名...]     独占执行探索（禁用其余任务，默认 御魂1 御魂2）
#   oasc solo-raid [配置名...]        独占执行个人突破（同上）
#   oasc solo-climb [配置名...]       独占执行当期式神爬塔（同上）
#   oasc runtask <任务名> [配置名...] 独占执行任意任务（如 runtask Orochi 御魂1）
#   oasc status                       查看各配置脚本实例的运行状态
#   oasc evo <子命令>                 EvoZone 组队任务（默认 3 个实例：御魂1/2/3）
#       status | set <键=值>... [--apply] | start | stop | solo [--dry-run] | help
#   oasc solo-evo [配置名...]         = oasc evo solo 的别名
#   oasc explor-raid [--watch 60] [配置名...]  一键安排探索+个人突破
#   oasc analyze                      统计探索/突破日志并生成报告
#   oasc help                         显示本帮助
#
# 实例控制命令的实现都在 nakk\oas_control.py；扩展新命令：在下方 $NamedScripts 照抄
# 一行（快捷脚本），或在 switch 里加分支（需要固定子命令前缀时）。

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Cmd = "help",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

# 本脚本在 deploy\launcher\ 下，项目根是它的上两级；不能用 $PSScriptRoot 直接当根，
# 否则 .venv、config、nakk 都会找不到。
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

# 解释器：优先本项目 .venv，其次上游安装包用的 toolkit 便携布局（与 oas.ps1 一致）
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    $toolkitPy = Join-Path $Root "toolkit\python.exe"
    if (Test-Path $toolkitPy) { $Py = $toolkitPy }
}

# 后端端口：优先读 config\deploy.yaml 的 WebuiPort，读不到则用 22288
$Port = 22288
$deployYaml = Join-Path $Root "config\deploy.yaml"
if (Test-Path $deployYaml) {
    $m = Select-String -Path $deployYaml -Pattern "WebuiPort:\s*(\d+)" | Select-Object -First 1
    if ($m) { $Port = [int]$m.Matches[0].Groups[1].Value }
}
$Base = "http://127.0.0.1:$Port"
$Ctrl = Join-Path $Root "nakk\oas_control.py"

# ---- 快捷命令注册表：命令名 => 脚本（相对项目根）----
# NeedsBackend = $true 表示执行前要求后端在运行（只检查并提示，不代劳启动）
# PassPort     = $true 表示给脚本追加 --port
$NamedScripts = [ordered]@{
    "explor-raid" = @{ Script = "nakk\quick_start_exploration.py"; NeedsBackend = $true; PassPort = $true;
                       Desc = "一键安排探索+个人突破（quick_start_exploration.py）" }
    "analyze"     = @{ Script = "nakk\analyze_exp_raid_log.py"; NeedsBackend = $false; PassPort = $false;
                       Desc = "统计探索/突破日志并生成报告（nakk\stats\）" }
}

# 与 module/server/script_process.py 的 ScriptState 对应
$StateNames = @{
    0 = "未运行"
    1 = "运行中"
    2 = "异常（需人工接管）"
    3 = "更新中"
}

function Test-BackendAlive {
    # 只读前置检查：后端状态的展示归 oas status，这里只用来给出引导，不重复实现
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne(500) -and $client.Connected) { return $true }
        return $false
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-ConfigNames {
    Get-ChildItem -Path (Join-Path $Root "config") -Filter *.json |
        Where-Object { $_.Name -ne "template.json" } |
        ForEach-Object { $_.BaseName } | Sort-Object
}

function Format-CjkPad([string]$s, [int]$width) {
    $len = 0
    foreach ($ch in $s.ToCharArray()) {
        if ([int]$ch -gt 0x2E7F) { $len += 2 } else { $len += 1 }
    }
    if ($len -ge $width) { return $s }
    return $s + (" " * ($width - $len))
}

function Get-ScriptState {
    # 经后端 WebSocket /ws/{配置名} 查询运行状态：连接后首帧即 {"state": N}
    param([string]$Name)
    $ws = New-Object System.Net.WebSockets.ClientWebSocket
    try {
        $ct = [System.Threading.CancellationToken]::None
        $uri = "ws://127.0.0.1:$Port/ws/" + [System.Uri]::EscapeDataString($Name)
        if (-not $ws.ConnectAsync([Uri]$uri, $ct).Wait(3000)) { return $null }
        $buffer = New-Object byte[] 65536
        $sb = New-Object System.Text.StringBuilder
        $messages = 0
        while ($true) {
            $seg = [ArraySegment[byte]]::new($buffer)
            $recvTask = $ws.ReceiveAsync($seg, $ct)
            if (-not $recvTask.Wait(5000)) { return $null }
            $result = $recvTask.Result
            if ($result.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) { return $null }
            if ($result.Count -gt 0) {
                [void]$sb.Append([System.Text.Encoding]::UTF8.GetString($buffer, 0, $result.Count))
            }
            if (-not $result.EndOfMessage) { continue }
            $text = $sb.ToString()
            [void]$sb.Clear()
            try { $json = $text | ConvertFrom-Json } catch { continue }
            if ($json.PSObject.Properties["state"]) { return [int]$json.state }
            $messages++
            if ($messages -ge 5) { return $null }
        }
    } catch {
        return $null
    } finally {
        try { $ws.Dispose() } catch { }
    }
}

function Invoke-Control {
    # 实例控制命令统一转发给 nakk\oas_control.py（$Rest 原样透传）
    param([string]$Sub)
    if (-not (Test-Path $Ctrl)) {
        Write-Host "找不到 $Ctrl" -ForegroundColor Red
        return
    }
    & $Py $Ctrl --port $Port $Sub @Rest
}

function Show-Status {
    if (-not (Test-BackendAlive)) {
        Write-Host "OAS 后端未运行（$Base 未监听），无法查询实例状态。" -ForegroundColor Yellow
        Write-Host "请先用 oas start 启动后端，或用 oas status 查看后端状态。"
        return
    }
    Write-Host "脚本实例状态（后端：$Base）："
    foreach ($name in Get-ConfigNames) {
        $state = Get-ScriptState $name
        if ($null -eq $state) {
            $line = "  " + (Format-CjkPad $name 14) + "状态未知"
            Write-Host $line -ForegroundColor Yellow
            continue
        }
        $label = if ($StateNames.ContainsKey($state)) { $StateNames[$state] } else { "state=$state" }
        $line = "  " + (Format-CjkPad $name 14) + $label
        if ($state -eq 1) { Write-Host $line -ForegroundColor Green }
        elseif ($state -eq 2) { Write-Host $line -ForegroundColor Red }
        else { Write-Host $line }
    }
}

function Invoke-Evo {
    # EvoZone 组队任务专属命令族；默认覆盖 3 个实例（1 队长 + 2 队员）
    # solo 复用 oas_control.py 的 solo-evo（它会禁用其余任务并重启实例，副作用大）
    $evoScript = Join-Path $Root "nakk\evo_zone_control.py"
    if (-not (Test-Path $evoScript)) {
        Write-Host "找不到 $evoScript" -ForegroundColor Red
        $global:LASTEXITCODE = 2
        return
    }
    $sub = "status"
    $tail = @()
    if ($Rest -and @($Rest).Count -gt 0) {
        $sub = @($Rest)[0]
        if (@($Rest).Count -gt 1) {
            $tail = @($Rest)[1..(@($Rest).Count - 1)]
        }
    }
    if ($sub -eq "solo") {
        & $Py $Ctrl --port $Port "solo-evo" @tail
    } else {
        & $Py $evoScript --port $Port $sub @tail
    }
    # 不返回值：原生命令的 stdout 应直接显示给调用者；退出码由 $LASTEXITCODE 传递
}

function Show-Help {
    Write-Host "OAS 实例控制（oasc）—— 仅补充 oas 没有的能力"
    Write-Host "项目根：$Root   后端：$Base"
    Write-Host ""
    Write-Host "后端管理请用 oas：oas start / stop / status / web / run / mumu（详见 oas help）"
    Write-Host ""
    Write-Host "实例控制（实现见 nakk\oas_control.py）："
    Write-Host "  oasc start <配置名...>             启动指定脚本实例（按现有调度表）"
    Write-Host "  oasc stop <配置名...>              停止指定脚本实例"
    Write-Host "  oasc stopall                       停止全部运行中的实例（后端保留）"
    Write-Host "  oasc disable <配置名...>           禁用实例全部任务并存快照"
    Write-Host "  oasc enable <配置名...>            按快照恢复实例的启用任务"
    Write-Host "  oasc solo-explore [配置名...]      独占执行探索（默认 御魂1 御魂2）"
    Write-Host "  oasc solo-raid [配置名...]         独占执行个人突破"
    Write-Host "  oasc solo-climb [配置名...]        独占执行当期式神爬塔"
    Write-Host "  oasc runtask <任务名> [配置名...]  独占执行任意任务（如 runtask Orochi 御魂1）"
    Write-Host "  （disable / enable / solo-* / runtask 支持 --dry-run 预览，不改配置）"
    Write-Host "  oasc status                        查看各配置实例运行状态"
    Write-Host ""
    Write-Host "EvoZone 组队任务（默认 3 个实例：御魂1 队长 + 御魂2/御魂3 队员）："
    Write-Host "  oasc evo status                    只读总览（状态/角色/层数/日志进度）"
    Write-Host "  oasc evo set <键=值>... [--apply]  批量改配置，默认 dry-run"
    Write-Host "  oasc evo start [--dry-run]         队员先起、队长后起"
    Write-Host "  oasc evo stop  [--dry-run]         队长先停、队员后停"
    Write-Host "  oasc evo solo  [--dry-run]         独占执行 EvoZone（会禁用其余任务并重启）"
    Write-Host "  oasc solo-evo [配置名...]          = oasc evo solo 的别名"
    Write-Host "  （evo 命令可用 --only <配置名> 指定子集；详见 oasc evo help）"
    Write-Host ""
    Write-Host "快捷命令（注册表见本文件 `$NamedScripts，可自行添加）："
    foreach ($k in $NamedScripts.Keys) {
        $e = $NamedScripts[$k]
        $flag = if ($e.NeedsBackend) { "  [需后端在运行]" } else { "" }
        Write-Host ("  oasc {0,-11} {1}{2}" -f $k, $e.Desc, $flag)
    }
    Write-Host ""
    Write-Host "  oasc help                         显示本帮助"
}

switch ($Cmd) {
    "help"         { Show-Help }
    "start"        {
        if (-not $Rest -or @($Rest).Count -eq 0) {
            Write-Host "oasc start 需要指定配置名，例如：oasc start 御魂1" -ForegroundColor Yellow
            Write-Host "启动后端（含 MuMu 前置）请用：oas start"
            return
        }
        Invoke-Control "start"; exit $LASTEXITCODE
    }
    "stop"         {
        if (-not $Rest -or @($Rest).Count -eq 0) {
            Write-Host "oasc stop 需要指定配置名，例如：oasc stop 御魂1" -ForegroundColor Yellow
            Write-Host "停止全部实例请用 oasc stopall；停止后端请用：oas stop"
            return
        }
        Invoke-Control "stop"; exit $LASTEXITCODE
    }
    "stopall"      { Invoke-Control "stopall"; exit $LASTEXITCODE }
    "disable"      { Invoke-Control "disable"; exit $LASTEXITCODE }
    "enable"       { Invoke-Control "enable"; exit $LASTEXITCODE }
    "solo-explore" { Invoke-Control "solo-explore"; exit $LASTEXITCODE }
    "solo-raid"    { Invoke-Control "solo-raid"; exit $LASTEXITCODE }
    "solo-climb"   { Invoke-Control "solo-climb"; exit $LASTEXITCODE }
    "solo-evo"     { Invoke-Control "solo-evo"; exit $LASTEXITCODE }
    "runtask"      { Invoke-Control "run-task"; exit $LASTEXITCODE }
    "status"       { Show-Status }
    "evo"          { Invoke-Evo; exit $LASTEXITCODE }
    default {
        if ($NamedScripts.Contains($Cmd)) {
            $entry = $NamedScripts[$Cmd]
            if ($entry.NeedsBackend -and -not (Test-BackendAlive)) {
                Write-Host "OAS 后端未运行（$Base 未监听），请先执行 oas start" -ForegroundColor Yellow
                return
            }
            $scriptPath = Join-Path $Root $entry.Script
            if (-not (Test-Path $scriptPath)) {
                Write-Host "找不到脚本：$scriptPath" -ForegroundColor Red
                return
            }
            $scriptArgs = @()
            if ($entry.PassPort) { $scriptArgs += @("--port", "$Port") }
            if ($Rest) { $scriptArgs += $Rest }
            & $Py $scriptPath @scriptArgs
            exit $LASTEXITCODE
        } else {
            Write-Host "未知命令：$Cmd" -ForegroundColor Yellow
            Write-Host ""
            Show-Help
        }
    }
}
