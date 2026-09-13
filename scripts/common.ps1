$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$LocalDir = Join-Path $ProjectRoot '.local'

function Get-ProjectPython {
    $venvPython = Join-Path $ProjectRoot 'backend/.venv/Scripts/python.exe'
    if (Test-Path -LiteralPath $venvPython) { return $venvPython }
    throw 'Run scripts/setup.ps1 first to create backend/.venv.'
}

function Import-LocalEnvironment {
    $envPath = Join-Path $LocalDir 'runtime.env'
    if (-not (Test-Path -LiteralPath $envPath)) { throw 'Run scripts/setup.ps1 first.' }
    foreach ($line in [IO.File]::ReadAllLines($envPath)) {
        if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
        }
    }
}

function Test-LocalPort([int] $Port) {
    $client = [Net.Sockets.TcpClient]::new()
    try { $client.Connect('127.0.0.1', $Port); return $true }
    catch { return $false }
    finally { $client.Dispose() }
}

function Wait-HttpReady([string] $Url, [int] $TimeoutSeconds = 40) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Url -TimeoutSec 2 -UseBasicParsing
            if ($response.StatusCode -eq 200) { return }
        } catch { }
        Start-Sleep -Milliseconds 400
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Service did not become ready: $Url. Inspect .local/*.log."
}

function New-ProjectSecret([int] $Bytes = 32) {
    $value = [byte[]]::new($Bytes)
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($value) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($value).Replace('+', '-').Replace('/', '_')
}
