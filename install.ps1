# =============================================================================
#  Simab Workspace — one-line install for Windows (runs inside WSL):
#
#    irm https://raw.githubusercontent.com/EsmailEmami/simabworkspace/main/install.ps1 | iex
#
#  1. Makes sure WSL with a Linux distro is installed (asks for admin once).
#  2. Installs Simab Workspace inside WSL and asks for your server and user.
#  3. Adds "Simab Workspace" to the Start menu.
# =============================================================================
$ErrorActionPreference = 'Stop'
$Repo = 'EsmailEmami/simabworkspace'
$Bootstrap = "https://raw.githubusercontent.com/$Repo/main/install.sh"

function Say($m) { Write-Host "  $m" }

# --- 1. WSL
$hasWsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
$distroOk = $false
if ($hasWsl) {
  & wsl.exe -e true 2>$null
  $distroOk = ($LASTEXITCODE -eq 0)
}
if (-not $distroOk) {
  Say 'WSL with Ubuntu is needed. Installing it (Windows asks for administrator rights)…'
  Start-Process wsl.exe -ArgumentList '--install', '-d', 'Ubuntu' -Verb RunAs -Wait
  Say 'When Ubuntu has finished (it may ask for a restart and to create a Linux user),'
  Say 'run this same command again:'
  Say "  irm https://raw.githubusercontent.com/$Repo/main/install.ps1 | iex"
  return
}

# --- 2. install inside WSL (python3 first, then the installer; it asks the setup questions)
Say 'Installing inside WSL…'
$pre = 'command -v python3 >/dev/null 2>&1 || { echo "  python3 is needed — your Linux password may be asked:"; sudo apt-get update -qq && sudo apt-get install -y -qq python3 curl; }'
$extra = ''
if ($env:SIMAB_HOST) { $extra += " SIMAB_HOST='$($env:SIMAB_HOST)'" }
if ($env:SIMAB_PORT) { $extra += " SIMAB_PORT='$($env:SIMAB_PORT)'" }
& wsl.exe -e bash -lc "$pre; curl -fsSL $Bootstrap | $extra bash"
if ($LASTEXITCODE -ne 0) { Say 'Installation did not finish. Fix the message above and run the command again.'; return }

# --- 3. Start menu shortcut (opens the window without a terminal)
$distro = (& wsl.exe -e sh -c 'printf %s "$WSL_DISTRO_NAME"')
$lnk = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Simab Workspace.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = 'wsl.exe'
$s.Arguments = "-d $distro -- bash -lc `"`$HOME/.local/bin/simabworkspace gui`""
$s.WindowStyle = 7
$s.Description = 'Simab Workspace'
$s.Save()
Say '✓ Done. Open "Simab Workspace" from the Start menu,'
Say '  or in a WSL terminal run:  simabworkspace'
