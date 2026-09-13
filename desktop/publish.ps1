param([string]$Configuration = 'Release', [string]$Runtime = 'win-x64')
$ErrorActionPreference = 'Stop'
$project = Join-Path $PSScriptRoot 'HisVoice.Desktop/HisVoice.Desktop.csproj'
$output = Join-Path $PSScriptRoot ('artifacts/' + $Runtime)
dotnet publish $project --configuration $Configuration --runtime $Runtime --self-contained true --output $output -p:PublishSingleFile=false
if ($LASTEXITCODE -ne 0) { throw 'Desktop publish failed.' }
Write-Output ('Published desktop app: ' + (Join-Path $output 'HisVoice.Desktop.exe'))
