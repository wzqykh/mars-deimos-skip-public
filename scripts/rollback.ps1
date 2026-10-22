param(
    [string]$BackupDir = ""
)

$ProjectDir = Split-Path -Parent $PSScriptRoot
if (-not $BackupDir) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $BackupDir = Join-Path $ProjectDir "rollback-$stamp"
}
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

foreach ($Relative in @("config.json", ".state", "data\learned.jsonl")) {
    $Source = Join-Path $ProjectDir $Relative
    if (Test-Path $Source) {
        $Destination = Join-Path $BackupDir $Relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
        Move-Item -Path $Source -Destination $Destination -Force
        Write-Host "moved: $Relative"
    }
}

Write-Host "rollback-backed-up: $BackupDir"
