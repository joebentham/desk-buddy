# Build + flash the desk-buddy firmware over a USB data cable on Windows: the counterpart of tools/flash.sh.
#   powershell -ExecutionPolicy Bypass -File tools\windows\flash.ps1           # program + Lump's drawings (~1 min)
#   powershell -ExecutionPolicy Bypass -File tools\windows\flash.ps1 -NoArt    # program only
#   ... -Port COM5                                                             # if more than one board is plugged in
# Day to day, update him over Wi-Fi instead (tools/ota.py). This is for the first install, a new partition
# layout, or recovery. Plain esptool works here: Windows' WCH driver for the board's CH343 chip doesn't drop
# bytes the way macOS's does, so there's no need for the Mac's paced upload.
# Needs Python 3; PlatformIO and esptool are installed into tools\.venv the first time.
param([switch]$NoArt, [string]$Port)
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Venv = Join-Path $Root "tools\.venv"
$Python = Join-Path $Venv "Scripts\python.exe"

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE) { throw "failed ($LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

if (-not (Test-Path $Python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { Invoke-Checked "py" @("-3", "-m", "venv", $Venv) }
    else { Invoke-Checked "python" @("-m", "venv", $Venv) }
}
if (-not (Test-Path (Join-Path $Venv "Scripts\pio.exe"))) {
    Invoke-Checked $Python @("-m", "pip", "-q", "install", "--disable-pip-version-check", "platformio", "esptool", "pyserial")
}

if (-not $Port) {
    # the board's USB-serial chip: WCH CH343 (VID 1A86, PID 55D3)
    $Port = (& $Python -c "from serial.tools import list_ports; print(' '.join(sorted(p.device for p in list_ports.comports() if (p.vid, p.pid) == (0x1A86, 0x55D3))))").Trim()
    if (-not $Port) { throw "Lump isn't plugged in (no CH343 USB-serial port found). Use a data cable, not a charge-only one." }
    if ($Port -match " ") { throw "More than one board found ($Port): pick one with -Port." }
}

Push-Location (Join-Path $Root "firmware")
try { Invoke-Checked (Join-Path $Venv "Scripts\pio.exe") @("run") } finally { Pop-Location }

$Build = Join-Path $Root "firmware\.pio\build\buddy"
$BootApp0 = Join-Path $HOME ".platformio\packages\framework-arduinoespressif32\tools\partitions\boot_app0.bin"
$Images = @("0x0", "$Build\bootloader.bin", "0x8000", "$Build\partitions.bin", "0xe000", $BootApp0, "0x10000", "$Build\firmware.bin")
if (-not $NoArt) { $Images += @("0x410000", (Join-Path $Root "firmware\data\lump.bin")) }    # the "lump" partition

# the bridge holds the serial port: pause it while flashing, start it again afterwards
$Ctl = Join-Path $PSScriptRoot "bridge.ps1"
& $Ctl -Stop | Out-Null
try {
    Invoke-Checked $Python (@("-m", "esptool", "--chip", "esp32s3", "--port", $Port, "--baud", "921600",
        "write-flash", "--flash-mode", "dio", "--flash-size", "16MB") + $Images)
    Write-Output "Flashed Lump on $Port."
} finally {
    & $Ctl -Start | Out-Null
}
