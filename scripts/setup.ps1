param([string] $Python = '')
. (Join-Path $PSScriptRoot 'common.ps1')

if (-not $Python) {
    $bundled = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
    $Python = if (Test-Path -LiteralPath $bundled) { $bundled } else { (Get-Command python).Source }
}
New-Item -ItemType Directory -Force -Path $LocalDir | Out-Null
$venvRoot = Join-Path $ProjectRoot 'backend/.venv'
if (-not (Test-Path -LiteralPath (Join-Path $venvRoot 'Scripts/python.exe'))) {
    & $Python -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed.' }
}
$projectPython = Get-ProjectPython
& $projectPython -m pip install -r (Join-Path $ProjectRoot 'backend/requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
Push-Location (Join-Path $ProjectRoot 'frontend')
try {
    if (Test-Path -LiteralPath 'package-lock.json') { & npm.cmd ci }
    else { & npm.cmd install }
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
} finally { Pop-Location }

$envPath = Join-Path $LocalDir 'runtime.env'
if (-not (Test-Path -LiteralPath $envPath)) {
    $jwtSecret = New-ProjectSecret 48
    $audioKey = New-ProjectSecret 32
    $dbFile = (Join-Path $LocalDir 'his.db').Replace('\', '/')
    $dataDir = (Join-Path $LocalDir 'data').Replace('\', '/')
    $lines = @(
        'HIS_ENV=development',
        "HIS_DATABASE_URL=sqlite:///$dbFile",
        "HIS_DATA_DIR=$dataDir",
        "HIS_JWT_SECRET=$jwtSecret",
        "HIS_AUDIO_KEY=$audioKey",
        'HIS_ALLOWED_ORIGINS=http://127.0.0.1:5173,http://localhost:5173',
        'HIS_MODEL_PROVIDER=demo',
        'HIS_ASR_PROVIDER=unavailable',
        'HIS_EMR_PROVIDER=mock',
        'HIS_RUN_WORKER=true'
    )
    [IO.File]::WriteAllLines($envPath, $lines, [Text.UTF8Encoding]::new($false))
}
$composeEnv = Join-Path $LocalDir 'compose.env'
if (-not (Test-Path -LiteralPath $composeEnv)) {
    $lines = @("HIS_DB_PASSWORD=$(New-ProjectSecret 24)", "HIS_JWT_SECRET=$(New-ProjectSecret 48)", "HIS_AUDIO_KEY=$(New-ProjectSecret 32)")
    [IO.File]::WriteAllLines($composeEnv, $lines, [Text.UTF8Encoding]::new($false))
}
Write-Host 'Dependencies installed. Local secrets created without printing their values.'
Write-Host 'Start: powershell -ExecutionPolicy Bypass -File scripts/start.ps1'
