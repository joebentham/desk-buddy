# Start, stop or check the Desk Buddy bridge on Windows (installed by tools\windows\install.ps1).
#   powershell -ExecutionPolicy Bypass -File tools\windows\bridge.ps1 -Status
#   ... -Stop     stops the bridge and its keep-alive (flash.ps1 and wifi_setup.py do this while they use the USB port)
#   ... -Start    starts them again (only if the bridge is installed)
param([switch]$Start, [switch]$Stop, [switch]$Status)

$RT = Join-Path $HOME ".desk-buddy"
$Shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "Desk Buddy bridge.lnk"
$Pythonw = Join-Path $RT "venv\Scripts\pythonw.exe"

function Get-BridgeProcesses {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -match '[\\/]\.desk-buddy[\\/](bridge|keepalive)\.py' }
}

function Test-Bridge {
    try { Invoke-RestMethod -Uri "http://127.0.0.1:47823/status" -TimeoutSec 2 } catch { $null }
}

if ($Stop) {
    # the keep-alive first, so it doesn't start the bridge again
    Get-BridgeProcesses | Sort-Object { $_.CommandLine -notmatch 'keepalive' } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    for ($i = 0; $i -lt 20 -and (Get-BridgeProcesses); $i++) { Start-Sleep -Milliseconds 250 }
    Write-Output "bridge stopped"
}

if ($Start) {
    if (-not (Test-Path $Shortcut) -or -not (Test-Path $Pythonw)) {
        Write-Output "bridge not installed (tools\windows\install.ps1)"
    } elseif (Get-BridgeProcesses | Where-Object { $_.CommandLine -match 'keepalive' }) {
        Write-Output "bridge already running"
    } else {
        Start-Process -FilePath $Pythonw -ArgumentList "`"$RT\keepalive.py`"" -WorkingDirectory $RT -WindowStyle Hidden
        for ($i = 0; $i -lt 40 -and -not (Test-Bridge); $i++) { Start-Sleep -Milliseconds 250 }
        Write-Output "bridge started"
    }
}

if ($Status -or -not ($Start -or $Stop)) {
    $s = Test-Bridge
    if ($s) { Write-Output ("bridge running: mood {0}, board {1}" -f $s.mood, $(if ($s.device) { "connected ($($s.link))" } else { "not connected" })) }
    else { Write-Output "bridge not running" }
}
