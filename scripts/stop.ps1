. (Join-Path $PSScriptRoot 'common.ps1')
$runPath = Join-Path $LocalDir 'run.json'
if (-not (Test-Path -LiteralPath $runPath)) { Write-Host 'No recorded project processes.'; exit 0 }
$run = Get-Content -LiteralPath $runPath -Raw -Encoding UTF8 | ConvertFrom-Json
$remaining = @()
foreach ($entry in $run.processes) {
    $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
    if (-not $process) { continue }
    # PowerShell versions deserialize ISO timestamps as either strings or DateTime.
    $recordedStart = if ($entry.started_at -is [DateTime]) {
        $entry.started_at.ToUniversalTime()
    } else {
        [DateTimeOffset]::Parse([string]$entry.started_at, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime
    }
    $sameStart = $process.StartTime.ToUniversalTime().Ticks -eq $recordedStart.Ticks
    $sameExe = [string]::Equals($process.Path, $entry.executable, [StringComparison]::OrdinalIgnoreCase)
    if ($sameStart -and $sameExe) {
        Stop-Process -Id $process.Id
        Write-Host "Stopped project $($entry.name)."
    } else {
        $remaining += $entry
        Write-Warning "PID $($entry.pid) no longer identifies the recorded process; skipped and retained in run.json."
    }
}
if ($remaining.Count -eq 0) {
    Remove-Item -LiteralPath $runPath
} else {
    $run.processes = @($remaining)
    [IO.File]::WriteAllText($runPath, ($run | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
}
