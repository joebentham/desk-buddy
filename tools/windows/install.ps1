# Install (or update) the Desk Buddy bridge on Windows: the Windows counterpart of tools/install_bridge.sh.
#   powershell -ExecutionPolicy Bypass -File tools\windows\install.ps1
#   powershell -ExecutionPolicy Bypass -File tools\windows\install.ps1 -Uninstall
# Runtime lives in ~/.desk-buddy (venv\, bridge.py, keepalive.py, sounds\, bridge.log), as on the Mac.
# It starts at logon from a shortcut in your Startup folder (no admin rights needed; it shows under
# Task Manager > Startup apps), running keepalive.py with pythonw.exe so there's no window, which
# restarts the bridge if it ever stops. It also installs Lump Control (control.py), a tray app to test
# Lump and change his settings, started at logon the same way.
# Then hook up Claude Code with: python tools\install_hooks.py
# Uninstall stops both and removes their shortcuts; the runtime folder is left in place.
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RT = Join-Path $HOME ".desk-buddy"
$Startup = [Environment]::GetFolderPath("Startup")
$Shortcut = Join-Path $Startup "Desk Buddy bridge.lnk"
$ControlShortcut = Join-Path $Startup "Lump Control.lnk"
$Venv = Join-Path $RT "venv"
$Python = Join-Path $Venv "Scripts\python.exe"

function Stop-Control {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -match '[\\/]\.desk-buddy[\\/]control\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

& (Join-Path $PSScriptRoot "bridge.ps1") -Stop | Out-Null
Stop-Control
if ($Uninstall) {
    Remove-Item $Shortcut, $ControlShortcut -ErrorAction SilentlyContinue
    Write-Output "Bridge and Lump Control stopped and removed from startup (runtime left in $RT)."
    exit 0
}

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE) { throw "failed ($LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

New-Item -ItemType Directory -Force $RT | Out-Null
if (-not (Test-Path $Python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { Invoke-Checked "py" @("-3", "-m", "venv", $Venv) }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { Invoke-Checked "python" @("-m", "venv", $Venv) }
    else { throw "Python 3 isn't installed: get it from python.org (or the Microsoft Store), then run this again." }
}
Invoke-Checked $Python @("-m", "pip", "-q", "install", "--disable-pip-version-check", "--upgrade", "pip")
Invoke-Checked $Python @("-m", "pip", "-q", "install", "--disable-pip-version-check", "pyserial", "psutil", "icalendar", "recurring-ical-events", "pystray", "pillow", "pywebview")
Copy-Item (Join-Path $Root "tools\buddy_bridge.py") (Join-Path $RT "bridge.py") -Force
# Lump Control's window (an HTML page shown in WebView2) and the board's own drawings, which it shows him with
New-Item -ItemType Directory -Force (Join-Path $RT "ui") | Out-Null
Copy-Item (Join-Path $Root "tools\windows\ui\*") (Join-Path $RT "ui") -Force
Copy-Item (Join-Path $Root "firmware\data\lump.bin") (Join-Path $RT "ui\lump.bin") -Force
Remove-Item (Join-Path $RT "lump.ico") -ErrorAction SilentlyContinue      # remade from lump_icon.png on next start
Copy-Item (Join-Path $Root "tools\windows\keepalive.py") (Join-Path $RT "keepalive.py") -Force
Copy-Item (Join-Path $Root "tools\windows\control.py") (Join-Path $RT "control.py") -Force
Copy-Item (Join-Path $Root "tools\windows\bridge.ps1") (Join-Path $RT "bridge.ps1") -Force
Copy-Item (Join-Path $Root "tools\lumpbar\icon.png") (Join-Path $RT "lump_icon.png") -Force
# Lump's voice (and sounds.json, the first time), then the sound effects for his tap gags
Invoke-Checked $Python @("-I", (Join-Path $Root "tools\make_sounds.py"))
Invoke-Checked $Python @("-I", (Join-Path $Root "tools\make_gag_sounds.py"))

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($Shortcut)
$lnk.TargetPath = Join-Path $Venv "Scripts\pythonw.exe"
$lnk.Arguments = "`"$(Join-Path $RT 'keepalive.py')`""
$lnk.WorkingDirectory = $RT
$lnk.Description = "Desk Buddy bridge: Claude -> Lump"
$lnk.Save()
$lnk = $shell.CreateShortcut($ControlShortcut)
$lnk.TargetPath = Join-Path $Venv "Scripts\pythonw.exe"
$lnk.Arguments = "`"$(Join-Path $RT 'control.py')`""
$lnk.WorkingDirectory = $RT
$lnk.Description = "Lump Control: test Lump and change his settings"
$lnk.Save()

& (Join-Path $PSScriptRoot "bridge.ps1") -Start | Out-Null
Start-Process -FilePath (Join-Path $Venv "Scripts\pythonw.exe") -ArgumentList "`"$(Join-Path $RT 'control.py')`"" -WorkingDirectory $RT
& (Join-Path $PSScriptRoot "bridge.ps1") -Status
Write-Output "Lump Control is in the tray (the ^ by the clock): click Lump to open it."
Write-Output "Installed. It starts by itself when you log in. Next, if you haven't yet: python tools\install_hooks.py"
