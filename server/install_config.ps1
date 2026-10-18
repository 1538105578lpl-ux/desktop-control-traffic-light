# 把 windows-mcp 指向本仓库的控制服务，并安装配套 skill。
# 只增量修改 mcp.json 中的 windows-mcp 一项，其他 MCP 条目原样保留。
$ErrorActionPreference = 'Stop'

$serverDir = $PSScriptRoot
$repoRoot = Split-Path -Parent $serverDir
$configPath = Join-Path $env:USERPROFILE '.workbuddy\mcp.json'
$backupDir = Join-Path $env:USERPROFILE '.workbuddy\backups'
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "未找到虚拟环境：$pythonExe`n请先在仓库根目录执行：py -3.12 -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt"
}
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "未找到 $configPath，请先启动一次客户端生成配置。"
}

$original = Get-Content -Raw -LiteralPath $configPath
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
[IO.File]::WriteAllText((Join-Path $backupDir "mcp-$stamp.json"), $original, [Text.UTF8Encoding]::new($false))

$config = $original | ConvertFrom-Json
if (-not $config.PSObject.Properties.Name -contains 'mcpServers') {
    $config | Add-Member -Force -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
}

$entry = [pscustomobject]@{
    command = $pythonExe
    args    = @('-X', 'utf8', (Join-Path $serverDir 'launcher.py'), 'serve')
    cwd     = $serverDir
    timeout = 60000
}
$config.mcpServers | Add-Member -Force -NotePropertyName 'windows-mcp' -NotePropertyValue $entry

$text = $config | ConvertTo-Json -Depth 40
$null = $text | ConvertFrom-Json
[IO.File]::WriteAllText($configPath, $text, [Text.UTF8Encoding]::new($false))

# 安装 skill（给 AI 看的操作规程）
$skillSrc = Join-Path $repoRoot 'skills\windows-desktop-control\SKILL.md'
$skillDst = Join-Path $env:USERPROFILE '.workbuddy\skills\windows-desktop-control'
if (Test-Path -LiteralPath $skillSrc) {
    New-Item -ItemType Directory -Force -Path $skillDst | Out-Null
    Copy-Item -LiteralPath $skillSrc -Destination (Join-Path $skillDst 'SKILL.md') -Force
    Write-Output "已安装 skill -> $skillDst"
}

Write-Output "已写入 windows-mcp 配置 -> $configPath"
Write-Output "备份：$backupDir\mcp-$stamp.json"
Write-Output "其他 MCP 条目保持不变。重启客户端后生效。"
