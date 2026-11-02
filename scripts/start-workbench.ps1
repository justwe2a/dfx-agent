param([switch]$Install, [switch]$Live, [switch]$Device, [int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if ($Install) {
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        & py -3.13 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.13 virtual environment creation failed.' }
    }
    & $pythonPath -m pip install -r requirements-agent-win-py313.txt -r requirements-ui-win-py313.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $pythonPath -m pip install --no-deps -e .
    if ($LASTEXITCODE -ne 0) { throw 'Project installation failed.' }
    & $pythonPath scripts/prepare-tokenizer.py
    if ($LASTEXITCODE -ne 0) { throw 'Tokenizer preparation failed; see the download output.' }
}
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run this script with -Install first.' }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git for Windows is required.' }
if ($Live -and -not $env:DEEPSEEK_API_KEY) { throw 'Set DEEPSEEK_API_KEY in the process environment before -Live.' }
$env:PYTHONUTF8 = '1'
$arguments = @('-m', 'dfx_agent.workbench', '--data-dir', (Join-Path $projectRoot 'runs\workbench'), '--port', $Port, '--open-browser')
if ($Live) { $arguments += '--allow-live' }
if ($Device) { $arguments += '--allow-device' }
Write-Host "DFX Agent: http://127.0.0.1:$Port"
& $pythonPath @arguments
exit $LASTEXITCODE
