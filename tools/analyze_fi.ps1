<#
.SYNOPSIS
Aggregates fault_injection.json sidecars into a table for §6.1.1.

Usage: .\analyze_fi.ps1
       .\analyze_fi.ps1 -OutDir "C:\path\to\fi_sweep"
#>

[CmdletBinding()]
param(
    [string]$ProjectRoot = "C:\Users\GREG-PC\Documents\NetBeansProjects\AburimeSoundManager",
    [string]$OutDir      = "experiments\fi_sweep"
)

$fiRoot = Join-Path $ProjectRoot $OutDir
$sidecars = Get-ChildItem $fiRoot -Recurse -Filter "fault_injection.json" -ErrorAction SilentlyContinue

if (-not $sidecars) { Write-Host "No sidecars found under $fiRoot" -ForegroundColor Yellow; return }

$rows = $sidecars | ForEach-Object {
    try { Get-Content $_.FullName -Raw | ConvertFrom-Json }
    catch { Write-Host "Skipping malformed sidecar: $($_.FullName)" -ForegroundColor Yellow }
}

$rows | Select-Object fault_class, condition, run_index,
                       injection_segment, segments_at_injection,
                       run_success, recovery_wall_clock_s,
                       restart_required, work_dir |
    Sort-Object fault_class, condition, run_index |
    Format-Table -AutoSize

# Save CSV for the paper
$csvPath = Join-Path $ProjectRoot "experiments\analysis\fault_injection.csv"
$rows | Export-Csv -Path $csvPath -NoTypeInformation
Write-Host ""
Write-Host "Wrote $csvPath" -ForegroundColor Green
Write-Host "Total FI runs: $($rows.Count)"