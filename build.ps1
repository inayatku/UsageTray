# Build UsageTray.exe from the .spec file, which holds the hidden imports
# (building from the .py file would regenerate and overwrite it).

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir

python -m PyInstaller --noconfirm UsageTray.spec
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Done! The app is .\dist\UsageTray.exe" -ForegroundColor Green
