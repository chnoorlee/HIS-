param([switch] $Desktop, [switch] $E2E)
. (Join-Path $PSScriptRoot 'common.ps1')
$python = Get-ProjectPython
Push-Location (Join-Path $ProjectRoot 'backend')
try {
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed.' }
} finally { Pop-Location }
Push-Location $ProjectRoot
try {
    & $python -m pytest tests/integrations -q
    if ($LASTEXITCODE -ne 0) { throw 'ASR adapter tests failed.' }
} finally { Pop-Location }
Push-Location (Join-Path $ProjectRoot 'frontend')
try {
    & npm.cmd test
    if ($LASTEXITCODE -ne 0) { throw 'Browser capture unit tests failed.' }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
} finally { Pop-Location }
if ($Desktop) {
    foreach ($project in @('HisVoice.Core.Tests', 'HisVoice.Transport.Tests')) {
        & dotnet test (Join-Path $ProjectRoot "desktop/$project/$project.csproj") --configuration Release
        if ($LASTEXITCODE -ne 0) { throw "Desktop $project tests failed." }
    }
}
if ($E2E) {
    Push-Location (Join-Path $ProjectRoot 'frontend')
    try {
        & npm.cmd run test:browser
        if ($LASTEXITCODE -ne 0) { throw 'Workspace browser regression tests failed.' }
        & npm.cmd run test:usability
        if ($LASTEXITCODE -ne 0) { throw 'Clinical editor workflow tests failed.' }
    } finally { Pop-Location }
    & node (Join-Path $ProjectRoot 'tests/e2e/workspace.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Browser system tests failed.' }
}
