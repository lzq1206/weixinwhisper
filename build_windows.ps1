$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project "runtime\.venv312\Scripts\python.exe"
Push-Location $project
try {
    & $python -m PyInstaller --clean --noconfirm "wxmoments_gui.spec"
    Write-Host "已生成：$project\dist\wxMoments.exe"
}
finally {
    Pop-Location
}
