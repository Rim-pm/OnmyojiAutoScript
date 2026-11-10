# 兼容旧入口：真正的实现已移到 deploy\launcher\oas.ps1（那里入库、可迭代）。
# 这里只做转发，方便已经配置好的 `oas` 别名和 `.\oas.ps1` 老用法继续工作。
& (Join-Path $PSScriptRoot "deploy\launcher\oas.ps1") @args
exit $LASTEXITCODE
