# Desktop Control Traffic Light —— 一键安装（Windows 10/11）
# 用法：在仓库根目录执行 powershell -ExecutionPolicy Bypass -File .\install.ps1
$ErrorActionPreference = 'Stop'

$repoRoot = $PSScriptRoot
Write-Output "仓库目录：$repoRoot"
Write-Output ''

# 1. 检查 Python
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Output '[1/4] 未找到 .venv，正在创建（需要 Python 3.12）...'
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        & py -3.12 -m venv (Join-Path $repoRoot '.venv')
    } else {
        & python -m venv (Join-Path $repoRoot '.venv')
    }
} else {
    Write-Output '[1/4] 虚拟环境已存在，跳过创建。'
}

# 2. 安装依赖
Write-Output '[2/4] 安装依赖...'
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r (Join-Path $repoRoot 'requirements.txt') --quiet
Write-Output '      依赖安装完成。'

# 3. 注册 MCP + skill
Write-Output '[3/4] 注册 MCP 并安装 skill...'
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repoRoot 'server\install_config.ps1')

# 4. 自检
Write-Output '[4/4] 运行自检...'
& $venvPython -m pytest (Join-Path $repoRoot 'tests') -q

Write-Output ''
Write-Output '安装完成。手动补两步：'
Write-Output '  python pet\install_hooks.py       # 让灯亮起来'
Write-Output '  pythonw pet\launch_pet.pyw        # 启动桌宠'
Write-Output ''
Write-Output '想开机自动出现：python pet\manage_watchdog.py install'
