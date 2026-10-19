# Install Satsuma to a short folder and add a Start menu shortcut.
# Run from the unzipped Satsuma folder (next to Satsuma.exe):
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# Why: Windows can't load the bundled Python/Qt DLLs when the app sits under a very
# long path (over 260 characters), e.g. deep inside AppData or nested download folders.
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
if (-not (Test-Path (Join-Path $src "Satsuma.exe"))) { $src = Join-Path $PSScriptRoot "dist\Satsuma" }
if (-not (Test-Path (Join-Path $src "Satsuma.exe"))) { throw "Satsuma.exe not found next to install.ps1" }

$dest = Join-Path $env:USERPROFILE "Satsuma"
Get-Process Satsuma -ErrorAction SilentlyContinue | Stop-Process -Force
robocopy $src $dest /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copy failed (robocopy $LASTEXITCODE)" }

$lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "Satsuma.lnk"
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = Join-Path $dest "Satsuma.exe"
$s.WorkingDirectory = $dest
$s.Description = "Zero-click offline file converter"
$s.Save()

Write-Host "Installed to $dest"
Write-Host "Start menu shortcut: $lnk"
Start-Process (Join-Path $dest "Satsuma.exe")
