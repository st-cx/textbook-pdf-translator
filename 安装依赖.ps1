param([string]$Python = 'python', [switch]$SkipModels)
$ErrorActionPreference = 'Stop'
$toolRoot = $PSScriptRoot
$pythonVersion = & $Python -c "import sys,struct; print(f'{sys.version_info.major}.{sys.version_info.minor}:{struct.calcsize(""P"")*8}')"
if ($pythonVersion -ne '3.13:64') { throw '此锁定依赖包已验证 Python 3.13 64位；请使用 -Python 指定对应 python.exe。' }
$envPath = Join-Path $toolRoot '.venv'
$envPython = Join-Path $envPath 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $envPython)) {
    & $Python -m venv $envPath
    if ($LASTEXITCODE -ne 0) { throw '无法建立项目专用 Python 环境。请安装 Python 3.13 64位。' }
}
& $envPython -m pip install -r (Join-Path $toolRoot 'requirements-lock.txt')
if ($LASTEXITCODE -ne 0) { throw '依赖安装未完成，请检查网络后重试。' }
& $envPython (Join-Path $toolRoot 'scripts\install_typst.py')
if ($LASTEXITCODE -ne 0) { throw 'Typst 准备未完成。可按 docs/INSTALL.md 使用官方离线压缩包。' }
if (-not $SkipModels) {
    Push-Location -LiteralPath $toolRoot
    try { & $envPython -m textbook_pdf.bootstrap } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw '模型尚未准备好；恢复网络后可单独重跑 python -m textbook_pdf.bootstrap，已有文件会保留。' }
}
$typstExe = Join-Path $toolRoot 'tools\typst.exe'
& $typstExe --version
& $envPython (Join-Path $toolRoot 'translate_pdf.py') --help
Write-Output '依赖检查完成。可将 PDF 拖到 翻译.bat，或按 README 指定页码试译。'
