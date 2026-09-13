param([int] $ApiPort = 8787, [int] $WebPort = 5173)
. (Join-Path $PSScriptRoot 'common.ps1')
Import-LocalEnvironment
foreach ($port in @($ApiPort, $WebPort)) {
    if (Test-LocalPort $port) { throw "Port $port is occupied. Choose -ApiPort and -WebPort explicitly." }
}
$python = Get-ProjectPython
$node = (Get-Command node).Source
$env:HIS_ALLOWED_ORIGINS = "http://127.0.0.1:$WebPort,http://localhost:$WebPort"
$env:VITE_API_PROXY = "http://127.0.0.1:$ApiPort"
$env:PYTHONPATH = $ProjectRoot
$started = @()
try {
    $api = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$ApiPort", '--no-access-log') -WorkingDirectory (Join-Path $ProjectRoot 'backend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LocalDir 'api.log') -RedirectStandardError (Join-Path $LocalDir 'api.error.log')
    $started += @{ name = 'api'; pid = $api.Id; started_at = $api.StartTime.ToUniversalTime().ToString('o'); executable = $python }
    Wait-HttpReady "http://127.0.0.1:$ApiPort/api/v1/health"
    $web = Start-Process -FilePath $node -ArgumentList @('node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', "$WebPort", '--strictPort') -WorkingDirectory (Join-Path $ProjectRoot 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LocalDir 'web.log') -RedirectStandardError (Join-Path $LocalDir 'web.error.log')
    $started += @{ name = 'web'; pid = $web.Id; started_at = $web.StartTime.ToUniversalTime().ToString('o'); executable = $node }
    Wait-HttpReady "http://127.0.0.1:$WebPort"
    $run = @{ api_url = "http://127.0.0.1:$ApiPort"; web_url = "http://127.0.0.1:$WebPort"; processes = $started }
    [IO.File]::WriteAllText((Join-Path $LocalDir 'run.json'), ($run | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
    Write-Host "Doctor workspace: http://127.0.0.1:$WebPort"
    Write-Host "API schema: http://127.0.0.1:$ApiPort/docs"
} catch {
    foreach ($entry in $started) { Stop-Process -Id $entry.pid -ErrorAction SilentlyContinue }
    throw
}
