# Install (or update) the Desk Buddy bridge on Windows: the Windows counterpart of tools/install_bridge.sh.
#   powershell -ExecutionPolicy Bypass -File tools\windows\install.ps1
#   powershell -ExecutionPolicy Bypass -File tools\windows\install.ps1 -Uninstall
# Runtime lives in ~/.desk-buddy (venv\, bridge.py, keepalive.py, sounds\, bridge.log), as on the Mac.
# It starts at logon from a shortcut in your Startup folder (no admin rights needed; it shows under
# Task Manager > Startup apps), running keepalive.py with pythonw.exe so there's no window, which
# restarts the bridge if it ever stops. It also installs Lump Control (control.py), a tray app to test
# Lump and change his settings, started at logon the same way.
# Then hook up Claude Code with: python tools\install_hooks.py
# Uninstall stops both (and the relay, below) and removes their shortcuts; the runtime folder is left in place.
#
# Remote link (Lump plugged into a different computer from the one Claude runs on; see README):
#   On the bridge PC:   install.ps1 -AllowRelay        makes a relay token (config.json "relay_token") if there
#                       isn't one, lets the tailnet reach the bridge's relay port with
#                       `tailscale serve --bg --tcp 4211 tcp://127.0.0.1:4211`, and prints what to run on the other
#                       computer (without the token).
#                       install.ps1 -AllowRelay -Off   removes that serve entry and the token (the bridge then
#                       drops the relay and stops listening).
#   On Lump's computer: install.ps1 -Relay -BridgeHost <bridge PC's Tailscale name> [-TokenFile <path>]
#                       asks for the token (hidden) unless -TokenFile gives a file holding it, sets up a venv with
#                       pyserial only, copies lump_relay.py, writes relay.json and starts the relay at logon
#                       ("Lump relay" in the Startup folder). It refuses if the bridge is installed on that computer
#                       too (both would want Lump's USB port) unless -Force, which stops the bridge there and
#                       removes it from startup.
param(
    [switch]$Uninstall,
    [switch]$AllowRelay,
    [switch]$Off,
    [switch]$Relay,
    [string]$BridgeHost,
    [string]$TokenFile,
    [switch]$Force
)
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RT = Join-Path $HOME ".desk-buddy"
$Startup = [Environment]::GetFolderPath("Startup")
$Shortcut = Join-Path $Startup "Desk Buddy bridge.lnk"
$ControlShortcut = Join-Path $Startup "Lump Control.lnk"
$RelayShortcut = Join-Path $Startup "Lump relay.lnk"
$RelayJson = Join-Path $RT "relay.json"
$Venv = Join-Path $RT "venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Control = Join-Path $Root "tools\windows\control.py"
$RelayPort = 4211

function Stop-Control {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -match '[\\/]\.desk-buddy[\\/]control\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Stop-Relay {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -match '[\\/]\.desk-buddy[\\/]lump_relay\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Test-BridgeInstalled {
    if (Test-Path $Shortcut) { return $true }
    $procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -match '[\\/]\.desk-buddy[\\/](bridge|keepalive)\.py' }
    return [bool]$procs
}

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE) { throw "failed ($LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

function New-Runtime {
    New-Item -ItemType Directory -Force $RT | Out-Null
    if (-not (Test-Path $Python)) {
        if (Get-Command py -ErrorAction SilentlyContinue) { Invoke-Checked "py" @("-3", "-m", "venv", $Venv) }
        elseif (Get-Command python -ErrorAction SilentlyContinue) { Invoke-Checked "python" @("-m", "venv", $Venv) }
        else { throw "Python 3 isn't installed: get it from python.org (or the Microsoft Store), then run this again." }
    }
    Invoke-Checked $Python @("-m", "pip", "-q", "install", "--disable-pip-version-check", "--upgrade", "pip")
}

function Get-Tailscale {
    $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $exe = Join-Path $env:ProgramFiles "Tailscale\tailscale.exe"
    if (Test-Path $exe) { return $exe }
    throw "Tailscale isn't installed on this PC (tailscale.com/download)."
}

function Get-TailscaleName([string]$Ts) {
    $json = & $Ts status --json | Out-String
    if ($LASTEXITCODE) { throw "tailscale status failed: is Tailscale running and logged in?" }
    $name = "$((ConvertFrom-Json $json).Self.DNSName)".TrimEnd(".")
    if (-not $name) { throw "Tailscale didn't report this PC's name (is MagicDNS on in the admin console?)" }
    return $name
}

# The token for -Relay: from -TokenFile, or typed (or pasted) at a hidden prompt. Never a command-line
# argument, so it can't land in a shell history or show up in the process list.
function Read-RelayToken {
    if ($TokenFile) {
        $t = "$(Get-Content -LiteralPath $TokenFile -Raw)".Trim()
    } else {
        $secure = Read-Host -AsSecureString "Relay token (on the bridge PC: Lump Control > Settings > Remote Lump > Copy token)"
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $t = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim() }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }
    if ($t -notmatch '^[0-9a-fA-F]{32}$') { throw "That isn't a relay token (it's 32 hex characters)." }
    return $t
}

if ($Off -and -not $AllowRelay) { throw "-Off goes with -AllowRelay (install.ps1 -AllowRelay -Off)." }
if ($AllowRelay -and $Relay) { throw "-AllowRelay is for the bridge PC, -Relay for the computer Lump is plugged into: pick one." }

# ---- remote link, bridge side: let the tailnet reach the bridge's relay port (127.0.0.1 only) ----
if ($AllowRelay) {
    if (-not (Test-Path $Python)) { throw "Install the bridge on this PC first (install.ps1 without -AllowRelay)." }
    $ts = Get-Tailscale
    if ($Off) {
        # only this one entry, never a reset of everything: anything else you serve is left alone
        & $ts serve --tcp $RelayPort off
        if ($LASTEXITCODE) { Write-Output "(no Tailscale serve entry for port $RelayPort to remove)" }
        Invoke-Checked $Python @("-I", $Control, "--remove-relay-token")
        Write-Output "Remote link off: the bridge drops the relay and stops listening within a few seconds."
        exit 0
    }
    Invoke-Checked $Python @("-I", $Control, "--ensure-relay-token")
    Invoke-Checked $ts @("serve", "--bg", "--tcp", "$RelayPort", "tcp://127.0.0.1:$RelayPort")
    $name = Get-TailscaleName $ts
    Write-Output ""
    Write-Output "Remote link allowed: Tailscale passes port $RelayPort on $name to the bridge (your tailnet only, no Funnel)."
    Write-Output "On the computer Lump is plugged into, in a copy of this repo, run:"
    Write-Output ""
    Invoke-Checked $Python @("-I", $Control, "--relay-command", $name)
    Write-Output ""
    Write-Output "It asks for the relay token: copy it from Lump Control here (Settings > Remote Lump > Copy token);"
    Write-Output "Remote Desktop shares the clipboard. Recommended: a Tailscale ACL so only that computer can reach"
    Write-Output "port $RelayPort here (see the README). Undo with: install.ps1 -AllowRelay -Off"
    exit 0
}

# ---- remote link, Lump's side: the relay ----
if ($Relay) {
    if (-not $BridgeHost) { throw "-Relay needs -BridgeHost <the bridge PC's Tailscale name> (install.ps1 -AllowRelay prints the whole command)." }
    if ($BridgeHost -notmatch '^[A-Za-z0-9.-]+$') { throw "-BridgeHost should be a Tailscale name such as mypc.tailnet-name.ts.net" }
    $bridgeHere = Test-BridgeInstalled
    if ($bridgeHere -and -not $Force) {
        throw "The bridge is installed on this PC too, and both would want Lump's USB port. Remove it first (install.ps1 -Uninstall), or add -Force to stop it and take it out of startup."
    }
    $token = Read-RelayToken
    if ($bridgeHere) {
        & (Join-Path $PSScriptRoot "bridge.ps1") -Stop | Out-Null
        Remove-Item $Shortcut -ErrorAction SilentlyContinue
        Write-Output "Stopped the bridge on this PC and took it out of startup (-Force)."
    }
    New-Runtime
    Invoke-Checked $Python @("-m", "pip", "-q", "install", "--disable-pip-version-check", "pyserial")
    Stop-Relay
    Copy-Item (Join-Path $Root "tools\lump_relay.py") (Join-Path $RT "lump_relay.py") -Force
    $cfg = [ordered]@{ bridge_host = $BridgeHost; port = $RelayPort; token = $token } | ConvertTo-Json
    [IO.File]::WriteAllText($RelayJson, $cfg, (New-Object Text.UTF8Encoding $false))
    $token = $null
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($RelayShortcut)
    $lnk.TargetPath = Join-Path $Venv "Scripts\pythonw.exe"
    $lnk.Arguments = "`"$(Join-Path $RT 'lump_relay.py')`""
    $lnk.WorkingDirectory = $RT
    $lnk.Description = "Lump relay: Lump on this computer, Claude on $BridgeHost"
    $lnk.Save()
    Start-Process -FilePath (Join-Path $Venv "Scripts\pythonw.exe") -ArgumentList "`"$(Join-Path $RT 'lump_relay.py')`"" -WorkingDirectory $RT
    Write-Output "Relay installed and running: it connects to $BridgeHost over Tailscale and starts by itself when you log in."
    Write-Output "Its log is $(Join-Path $RT 'relay.log'). Remove it with: install.ps1 -Uninstall"
    exit 0
}

& (Join-Path $PSScriptRoot "bridge.ps1") -Stop | Out-Null
Stop-Control
if ($Uninstall) {
    Stop-Relay
    Remove-Item $Shortcut, $ControlShortcut, $RelayShortcut -ErrorAction SilentlyContinue
    Remove-Item $RelayJson -ErrorAction SilentlyContinue           # the relay's copy of the token
    Write-Output "Bridge, Lump Control and relay stopped and removed from startup (runtime left in $RT)."
    $config = Join-Path $RT "config.json"
    if ((Test-Path $config) -and (Select-String -LiteralPath $config -Pattern '"relay_token"' -Quiet)) {
        Write-Output "The remote link is still allowed on this PC: install.ps1 -AllowRelay -Off removes its Tailscale serve entry and token."
    }
    exit 0
}

New-Runtime
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
