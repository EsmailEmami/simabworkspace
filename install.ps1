# =============================================================================
#  Simab Workspace — one-line install for Windows (runs on Windows itself, no WSL):
#
#    irm https://raw.githubusercontent.com/EsmailEmami/simabworkspace/main/install.ps1 | iex
#
#  1. Makes sure Python 3 and Git for Windows are installed (winget, asks once if needed).
#  2. Installs Simab Workspace and asks for your server and user (Mutagen and jq
#     are downloaded into %USERPROFILE%\.local\bin).
#  3. Adds "Simab Workspace" to the Start menu and opens the window.
#  A team can pre-fill its server:  $env:SIMAB_HOST='10.0.0.5'; $env:SIMAB_PORT='2222'; irm … | iex
# =============================================================================
function Install-SimabWorkspace {
  $ErrorActionPreference = 'Continue'      # native programs write to stderr; check exit codes instead
  $Repo = 'EsmailEmami/simabworkspace'
  try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

  function Say($m) { Write-Host "  $m" }

  function Update-PathFromRegistry {
    $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $u = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($m, $u) | Where-Object { $_ }) -join ';'
  }

  function Find-Python {
    # python.exe of a real Python 3 (the "python3" of the Microsoft Store is only a placeholder)
    $cands = @()
    foreach ($c in 'py', 'python', 'python3') {
      $cmd = Get-Command $c -ErrorAction SilentlyContinue
      if ($cmd) { $cands += $cmd.Source }
    }
    foreach ($d in (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)) {
      $cands += $d.FullName
    }
    foreach ($c in $cands) {
      $args3 = @()
      if ((Split-Path $c -Leaf) -ieq 'py.exe') { $args3 = @('-3') }
      $exe = (& $c @args3 -c "import sys; print(sys.executable if sys.version_info >= (3, 8) else '')" 2>$null | Select-Object -First 1)
      if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) { return $exe.Trim() }
    }
    return $null
  }

  function Find-GitBash {
    $c = @()
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) { $c += Join-Path (Split-Path (Split-Path $git.Source)) 'bin\bash.exe' }
    foreach ($b in $env:ProgramFiles, $env:ProgramW6432, ${env:ProgramFiles(x86)}, "$env:LOCALAPPDATA\Programs") {
      if ($b) { $c += Join-Path $b 'Git\bin\bash.exe' }
    }
    foreach ($p in $c) { if (Test-Path $p) { return $p } }   # never System32\bash.exe (that is WSL)
    return $null
  }

  function Install-WithWinget($id, $what) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
      Say "✗ $what is needed and winget is not available. Install it yourself, then run this command again."
      return $false
    }
    Say "Installing $what (winget)…"
    & winget install --id $id -e --source winget --accept-package-agreements --accept-source-agreements
    Update-PathFromRegistry
    return $true
  }

  Say 'Simab Workspace — Windows install'

  # --- 1. Python and Git for Windows
  $py = Find-Python
  if (-not $py) {
    if (-not (Install-WithWinget 'Python.Python.3.12' 'Python 3')) { Say 'https://www.python.org/downloads/windows/'; return }
    $py = Find-Python
    if (-not $py) { Say '✗ Python was installed but cannot be found yet. Open a new PowerShell and run the command again.'; return }
  }
  Say "✓ Python: $py"
  $bash = Find-GitBash
  if (-not $bash) {
    if (-not (Install-WithWinget 'Git.Git' 'Git for Windows')) { Say 'https://git-scm.com/download/win'; return }
    $bash = Find-GitBash
    if (-not $bash) { Say '✗ Git was installed but cannot be found yet. Open a new PowerShell and run the command again.'; return }
  }
  Say "✓ Git Bash: $bash"
  # python must be found by name inside Git Bash too
  $pyDir = Split-Path $py
  if (-not (($env:Path -split ';') -contains $pyDir)) { $env:Path = "$pyDir;$env:Path" }

  # --- 2. download the release and install it (into %USERPROFILE%\.local\…)
  $url = "https://github.com/$Repo/releases/latest/download/simabworkspace-install.sh"
  if ($env:SIMAB_VERSION) { $url = "https://github.com/$Repo/releases/download/$($env:SIMAB_VERSION)/simabworkspace-install.sh" }
  if ($env:SIMAB_INSTALLER_URL) { $url = $env:SIMAB_INSTALLER_URL }
  $tmp = Join-Path ([IO.Path]::GetTempPath()) ("simabworkspace-" + [guid]::NewGuid().ToString('n'))
  New-Item -ItemType Directory -Path $tmp | Out-Null
  $file = Join-Path $tmp 'simabworkspace-install.sh'
  Say 'Downloading Simab Workspace…'
  try {
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $file -ErrorAction Stop
  } catch {
    Say "✗ Download failed: $url"; Say "  $($_.Exception.Message)"; return
  }
  & $bash ($file -replace '\\', '/') --no-open
  $code = $LASTEXITCODE
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  if ($code -ne 0) { Say '✗ Installation did not finish. Fix the message above and run the command again.'; return }

  $home2 = $env:USERPROFILE
  $binDir = Join-Path $home2 '.local\bin'
  $cmd = Join-Path $binDir 'simabworkspace'
  $server = Join-Path $home2 '.local\share\simabworkspace\gui\server.py'
  if (-not (Test-Path $server)) { $server = Join-Path $home2 '.local\share\simabworkspace\app\gui\server.py' }

  # `simabworkspace` in PowerShell / cmd (simabworkspace.cmd) and in Git Bash
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  if (-not (($userPath -split ';') -contains $binDir)) {
    [Environment]::SetEnvironmentVariable('Path', ((@($userPath, $binDir) | Where-Object { $_ }) -join ';'), 'User')
  }
  if (-not (($env:Path -split ';') -contains $binDir)) { $env:Path = "$binDir;$env:Path" }

  # --- 3. first time: server, user, SSH key and the sync tools (questions in this window)
  $config = Join-Path $home2 '.config\simabworkspace\config'
  if ($env:XDG_CONFIG_HOME) { $config = Join-Path $env:XDG_CONFIG_HOME 'simabworkspace\config' }
  if (-not (Test-Path $config) -and -not $env:SIMAB_NO_SETUP) {
    Say ''
    Say 'Set up this machine and your server:'
    & $bash ($cmd -replace '\\', '/') setup
    if ($LASTEXITCODE -ne 0) { Say 'Setup did not finish — you can finish it in the window (Settings) or run: simabworkspace setup' }
  }

  # --- 4. Start menu, then open the window
  & $py $server shortcut
  if ($LASTEXITCODE -ne 0) { Say '! Could not add the Start menu entry (the window can do it: Settings).' }
  Say ''
  Say '✓ Done. Open "Simab Workspace" from the Start menu,'
  Say '  or run in a new PowerShell:  simabworkspace gui'
  if (-not $env:SIMAB_NO_OPEN) {
    $pyw = Join-Path (Split-Path $py) 'pythonw.exe'
    if (-not (Test-Path $pyw)) { $pyw = $py }
    Start-Process -FilePath $pyw -ArgumentList @("`"$server`"", 'open') -WorkingDirectory $home2
  }
}
Install-SimabWorkspace
