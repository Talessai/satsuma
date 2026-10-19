# Build the release: dist\Satsuma\ (app), dist\Satsuma-Setup.exe (installer) and
# dist\Satsuma-win64.zip (portable). Runs the tests and self-tests the built exe.
#   powershell -ExecutionPolicy Bypass -File build.ps1 [-SkipTests]
param([switch]$SkipTests)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv)) {
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}
$py = ".\.venv\Scripts\python.exe"

if (-not $SkipTests) {
    & $py -m pytest tests -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw "tests failed" }
}

& $py scripts\third_party_notices.py
& .\.venv\Scripts\pyinstaller.exe satsuma.spec --noconfirm --log-level WARN
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$report = Join-Path $env:TEMP "satsuma-selftest.json"
$p = Start-Process dist\Satsuma\Satsuma.exe -ArgumentList "--selftest", $report -PassThru -Wait
Get-Content $report
if ($p.ExitCode -ne 0) { throw "self-test of the built exe failed" }

$version = & $py -c "import satsuma; print(satsuma.__version__)"
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc /Q "/DAppVersion=$version" installer\satsuma.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
} else {
    Write-Warning "Inno Setup not found: skipping Satsuma-Setup.exe (winget install JRSoftware.InnoSetup)"
}

Copy-Item install.ps1 dist\Satsuma\install.ps1
$zip = "dist\Satsuma-win64.zip"
Remove-Item $zip -ErrorAction SilentlyContinue
Compress-Archive -Path dist\Satsuma -DestinationPath $zip
Get-ChildItem dist -File | Format-Table Name, @{n = "MB"; e = { [math]::Round($_.Length / 1MB) } }
Write-Host "Built Satsuma $version"
