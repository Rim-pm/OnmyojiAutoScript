# oas.ps1 —— 本项目 (OnmyojiAutoScript) 命令行入口
# 由 PowerShell 配置文件里的 oas 函数转发调用，也可直接 .\deploy\launcher\oas.ps1 <命令>
#
# 用法：
#   oas start           先把 MuMu 实例启动并摆成 base 布局，再确保后端在运行
#                       （MuMu 失败只警告不阻断；加 --no-mumu 可跳过这一步）
#   oas stop            停止后端（占用 WebuiPort 的进程）
#   oas status          查看后端是否运行
#   oas web             在浏览器打开 WebUI
#   oas run <脚本.py> [参数...]   用 .venv\Scripts\python.exe 运行项目内任意脚本
#   oas mumu [动作] [布局] [编号]  MuMu 多开窗口的启动/停止/摆放
#       oas mumu                     摆成 base 布局（没启动的实例会先启动）
#       oas mumu hand 2              摆成指定布局（同上，缺实例先启动）
#       oas mumu hand 2 --no-start   只摆放已在运行的实例
#       oas mumu start hand 3        启动实例并摆放
#       oas mumu stop                关闭 base 布局涉及的实例
#       oas mumu restart hand 2      重启实例并摆放
#       oas mumu --list              只列出当前位置大小
#       oas mumu --dry-run           只报告差异不调整
#   oas help            显示本帮助

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Cmd = "help",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

# 本脚本在 deploy\launcher\ 下，项目根是它的上两级；不用 $PSScriptRoot 直接当根，
# 否则 .venv、config、server.py 全都会找不到。
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

# 解释器：优先本项目自己的 .venv，其次上游安装包用的 toolkit 便携布局。
# 两个都没有时仍按 .venv 报错，提示更直观。
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

function Test-BackendAlive {
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

function Get-BackendPid {
    try {
        return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        return @()
    }
}

function Start-MumuForBackend {
    # oas start 的前置步骤：把 MuMu 实例启动并摆成 base 布局。
    # 模拟器没装、没开、摆放失败都不阻断后端启动，只提示一句。
    if (-not (Test-Path $Py)) {
        Write-Host "找不到 $Py，跳过 MuMu 前置步骤" -ForegroundColor Yellow
        return
    }
    $scriptPath = Join-Path $Root "dev_tools\mumu_windows.py"
    if (-not (Test-Path $scriptPath)) {
        Write-Host "找不到 $scriptPath，跳过 MuMu 前置步骤" -ForegroundColor Yellow
        return
    }
    Write-Host "正在启动 MuMu 实例并摆成 base 布局..."
    Push-Location $Root
    try {
        # base 本身就会先启动缺失的实例、等窗口稳定后再摆，等价于
        # 「mumu start base」再「mumu base」，且已在运行的实例不会重复等待。
        & $Py $scriptPath base
        if ($LASTEXITCODE -ne 0) {
            Write-Host "MuMu 启动或摆放未完全成功（退出码 $LASTEXITCODE），继续启动后端" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "MuMu 前置步骤出错：$_，继续启动后端" -ForegroundColor Yellow
    } finally {
        Pop-Location
    }
}

function Start-Backend {
    if (Test-BackendAlive) {
        Write-Host "OAS 后端已在运行：$Base"
        return $true
    }
    if (-not (Test-Path $Py)) {
        Write-Host "找不到 $Py，请检查项目目录" -ForegroundColor Red
        return $false
    }
    Write-Host "正在启动 OAS 后端 (server.py，端口 $Port)..."
    Start-Process -FilePath $Py -ArgumentList "server.py" -WorkingDirectory $Root -WindowStyle Minimized
    for ($i = 1; $i -le 60; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-BackendAlive) {
            Write-Host "OAS 后端已就绪：$Base"
            return $true
        }
    }
    Write-Host "等待 30 秒后端口 $Port 仍未监听，请查看最小化的 server.py 窗口排查" -ForegroundColor Yellow
    return $false
}

function Stop-Backend {
    $bpids = Get-BackendPid
    if (-not $bpids.Count) {
        Write-Host "OAS 后端未在运行（端口 $Port 没有监听进程）"
        return
    }
    foreach ($p in $bpids) {
        $procName = (Get-Process -Id $p -ErrorAction SilentlyContinue).ProcessName
        Write-Host "停止 OAS 后端进程 PID=$p ($procName) ..."
        Stop-Process -Id $p -Force
    }
    Write-Host "已停止。"
}

function Show-Status {
    if (Test-BackendAlive) {
        $bpids = Get-BackendPid
        $suffix = if ($bpids.Count) { "  (PID: $($bpids -join ', '))" } else { "" }
        Write-Host "OAS 后端：运行中  $Base$suffix"
    } else {
        Write-Host "OAS 后端：未运行（端口 $Port 未监听）"
    }
}

function Invoke-Script {
    if (-not $Rest -or @($Rest).Count -eq 0) {
        Write-Host "用法: oas run <脚本.py> [参数...]（路径相对项目根，也支持绝对路径）" -ForegroundColor Yellow
        return
    }
    $target = $Rest[0]
    $scriptPath = if ([System.IO.Path]::IsPathRooted($target)) { $target } else { Join-Path $Root $target }
    if (-not (Test-Path $scriptPath)) {
        Write-Host "找不到脚本：$scriptPath" -ForegroundColor Red
        return
    }
    & $Py $scriptPath @($Rest | Select-Object -Skip 1)
}

function Invoke-MumuWindows {
    # MuMu 多开窗口：缺省摆放，其余动作（start/stop/restart/place/list）与
    # --dry-run/--list 等参数原样透传，具体逻辑都在 dev_tools\mumu_windows.py。
    # 必须用脚本路径而不是 -m：-m 依赖当前目录在 sys.path 里，从别的目录调用会
    # 报 ModuleNotFoundError。工作目录同样切到项目根，日志才落到项目 log/ 下。
    # 只让 python 的输出原样上屏，函数本身不返回值：$LASTEXITCODE 由调用处读取。
    $scriptPath = Join-Path $Root "dev_tools\mumu_windows.py"
    Push-Location $Root
    try {
        & $Py $scriptPath @Rest
    } finally {
        Pop-Location
    }
}

function Show-Help {
    Write-Host "OAS 命令入口（项目根：$Root，后端：$Base）"
    Write-Host ""
    Write-Host "  oas start          先启动 MuMu 并摆成 base 布局，再确保后端在运行"
    Write-Host "                     （MuMu 失败只警告不阻断；加 --no-mumu 可跳过）"
    Write-Host "  oas stop           停止后端"
    Write-Host "  oas status         查看后端状态"
    Write-Host "  oas web            在浏览器打开 WebUI"
    Write-Host "  oas run <脚本.py> [参数...]   用 .venv\Scripts\python.exe 运行项目内任意脚本"
    Write-Host "  oas mumu [动作] [布局] [编号]   MuMu 多开窗口的启动/停止/摆放"
    Write-Host "                       oas mumu                 摆成 base 布局（缺实例会先启动）"
    Write-Host "                       oas mumu base            两个窗口并排铺满主屏"
    Write-Host "                       oas mumu hand            等同 oas mumu hand 2"
    Write-Host "                       oas mumu hand 2          左右各占主屏一半宽，两者同宽同高"
    Write-Host "                       oas mumu hand 3          再加第三个窗口贴在左下角"
    Write-Host "                       oas mumu hand 2 --no-start  只摆放已在运行的实例"
    Write-Host "                       oas mumu start [布局]    启动实例（主程序没开会自动拉起）并摆放"
    Write-Host "                       oas mumu stop            关闭当前布局涉及的实例"
    Write-Host "                       oas mumu restart [布局]  重启实例并摆放"
    Write-Host "                       oas mumu --list          只列出当前位置大小"
    Write-Host "                       oas mumu --dry-run       只报告差异不调整"
    Write-Host "  oas help           显示本帮助"
}

switch ($Cmd) {
    "help"   { Show-Help }
    "start"  {
        if ($Rest -contains "--no-mumu") {
            Write-Host "按 --no-mumu 跳过 MuMu 前置步骤"
        } else {
            Start-MumuForBackend
        }
        [void](Start-Backend)
    }
    "stop"   { Stop-Backend }
    "status" { Show-Status }
    "web"    { Start-Process $Base }
    "run"    { Invoke-Script }
    # 先把 python 的退出码存下来再 exit：pwsh -File 时即进程退出码；被 & 或
    # dot-source 调用时只结束本脚本并留下 $LASTEXITCODE，不会关掉调用方会话。
    "mumu"   { Invoke-MumuWindows; exit $LASTEXITCODE }
    default  {
        Write-Host "未知命令：$Cmd" -ForegroundColor Yellow
        Write-Host ""
        Show-Help
    }
}
