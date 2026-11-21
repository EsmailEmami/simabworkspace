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

  # Runs one python.exe and returns its full path if it is a real Python 3.8+ (else $null).
  # The exit code is read right after the call (a pipeline like `| Select-Object -First 1`
  # can leave $LASTEXITCODE of an earlier program, e.g. winget's, and reject a good Python).
  function Test-Python($exe, $pre) {
    if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { return $null }
    $out = @(& $exe @pre -c 'import sys; print(sys.executable) if sys.version_info >= (3, 8) else sys.exit(3)' 2>$null)
    $code = $LASTEXITCODE
    if ($code -ne 0 -or $out.Count -eq 0) { return $null }
    $p = ([string]$out[0]).Trim()
    if ($p -and (Test-Path -LiteralPath $p)) { return $p }
    return $null
  }

  function Find-Python {
    # 1. where Python installers register themselves (works even when python is not on PATH —
    #    the python.org / winget installer does not add it to PATH by default)
    $cands = @()
    foreach ($root in 'HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore') {
      foreach ($k in (Get-ChildItem $root -ErrorAction SilentlyContinue | Sort-Object PSChildName -Descending)) {
        $ip = Get-ItemProperty (Join-Path $k.PSPath 'InstallPath') -ErrorAction SilentlyContinue
        if ($ip) {
          if ($ip.ExecutablePath) { $cands += $ip.ExecutablePath }
          if ($ip.'(default)') { $cands += (Join-Path $ip.'(default)' 'python.exe') }
        }
      }
    }
    # 2. the usual folders
    foreach ($d in (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)) {
      $cands += $d.FullName
    }
    foreach ($c in $cands) {
      $r = Test-Python $c @()
      if ($r) { return $r }
    }
    # 3. on PATH: the py launcher, then python (the Microsoft Store placeholder fails the test)
    foreach ($n in 'py', 'python', 'python3') {
      $cmd = Get-Command $n -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
      if (-not $cmd) { continue }
      $pre = @()
      if ($n -eq 'py') { $pre = @('-3') }
      $r = Test-Python $cmd.Source $pre
      if ($r) { return $r }
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
    $code = $LASTEXITCODE
    if ($code -ne 0) { Say "  (winget exit code $code — if it says 'already installed', that is fine)" }
    Update-PathFromRegistry
    return $true
  }

  Say 'Simab Workspace — Windows install'

  # --- 1. Python and Git for Windows
  $py = Find-Python
  if (-not $py) {
    if (-not (Install-WithWinget 'Python.Python.3.12' 'Python 3')) { Say 'https://www.python.org/downloads/windows/'; return }
    $py = Find-Python
    if (-not $py) {
      Say '✗ Python was installed but cannot be found yet. Open a NEW PowerShell window and run the command again.'
      Say '  If it still fails, send this line:'
      Say ("  py: " + [bool](Get-Command py -ErrorAction SilentlyContinue) + " · python: " + ((Get-Command python -ErrorAction SilentlyContinue).Source) +
           " · folders: " + ((Get-ChildItem "$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python3*" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }) -join ', '))
      return
    }
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
  if ($env:SIMAB_PAIR) {
    # a one-time link from the web version (Computers → Connect a computer): no questions, no password
    Say ''
    Say 'Connecting this computer to your server…'
    & $py $server pair $env:SIMAB_PAIR
    if ($LASTEXITCODE -ne 0) { Say 'The link did not work — make a new one in the web version (Computers → Connect a computer).' }
  }
  elseif (-not (Test-Path $config) -and -not $env:SIMAB_NO_SETUP) {
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
