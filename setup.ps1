# Set up Zyxel BW Monitor in a local virtual environment (Windows).
#
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venv = if ($env:VENV) { $env:VENV } else { ".venv" }

$candidates = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $candidates += @{ Exe = "py"; Args = @("-3") }
}
if (Get-Command python -ErrorAction SilentlyContinue) {
    $candidates += @{ Exe = "python"; Args = @() }
}

$py = $null
foreach ($c in $candidates) {
    $version = & $c.Exe @($c.Args) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $version) { continue }
    if ([version]$version -ge [version]"3.9") { $py = $c; break }
    Write-Warning "Found Python $version via $($c.Exe); 3.9 or newer is required."
}

if (-not $py) {
    Write-Error @"
Python 3.9+ was not found.
  Install it from https://www.python.org/downloads/windows/ and tick
  "Add python.exe to PATH", or use:  winget install Python.Python.3.12
"@
}

Write-Host "Creating $venv with $($py.Exe) $(@($py.Args)) ($(& $py.Exe @($py.Args) -V))"
& $py.Exe @($py.Args) -m venv $venv
$python = Join-Path $venv "Scripts\python.exe"

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip failed to upgrade" }
& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }

Write-Host @"

Installed. To use it:

  .\$venv\Scripts\Activate.ps1
  `$env:ZX_PW = 'your ONT admin password'
  python .\read_usage.py --record usage_history.db
  python .\dashboard.py

If activation is blocked:  Set-ExecutionPolicy -Scope CurrentUser Process

Preview the dashboard with synthetic data (no device needed):

  python .\make_demo_data.py ; python .\dashboard.py --db demo_history.db --out demo_dashboard.html
"@
