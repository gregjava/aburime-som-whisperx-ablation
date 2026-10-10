<#
.SYNOPSIS
Standalone fault-injection runner for the naive 5-min condition.

Launches the framework, waits a fixed wall-clock delay, kills Java,
restarts with --resume, measures recovery, writes a sidecar.

Does not depend on progress.dat or any framework-internal signal.
#>

[CmdletBinding()]
param(
    [string]$Condition = "naive_full_n1_t5min_fi",
    [int]$DelayMinutes = 20,
    [int]$RunIndex = 1,
    [string]$ProjectRoot = "C:\Users\GREG-PC\Documents\NetBeansProjects\AburimeSoundManager",
    [string]$OutDir = "experiments\fi_sweep"
)

$ErrorActionPreference = 'Stop'

# --- paths ---
$manifest   = Join-Path $ProjectRoot "experiments\manifest_fi.csv"
$outPath    = Join-Path $ProjectRoot $OutDir
$driverPath = Join-Path $ProjectRoot "experiments\driver.py"
$runDir     = Join-Path $outPath ("{0}\run_{1:D2}" -f $Condition, $RunIndex)
$artefacts  = Join-Path $runDir "artefacts"
$sidecar    = Join-Path $artefacts "fault_injection.json"
$logDir     = Join-Path $outPath "_logs"

foreach ($p in @($manifest, $driverPath)) {
    if (-not (Test-Path $p)) { throw "Missing: $p" }
}
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

# --- guard ---
$stale = Get-Process java, python -ErrorAction SilentlyContinue
if ($stale) {
    Write-Host "ABORT: java/python already running:" -ForegroundColor Red
    $stale | Select-Object Id, Name, StartTime | Format-Table -AutoSize
    throw "Kill them first"
}

# --- banner ---
Write-Host "=== Naive FI run ===" -ForegroundColor Cyan
Write-Host "Condition : $Condition"
Write-Host "Delay     : $DelayMinutes min ($($DelayMinutes * 60) s)"
Write-Host "Run index : $RunIndex"
Write-Host ""

# --- helper to invoke driver ---
function Start-Driver {
    param([switch]$Resume, [string]$Tag)
    $driverArgs = @(
        $driverPath,
        "--manifest",       $manifest,
        "--out",            $outPath,
        "--only-condition", $Condition,
        "--only-run",       $RunIndex.ToString()
    )
    if ($Resume) { $driverArgs += "--resume" }

    $stdout = Join-Path $logDir "naivefi_${Tag}_stdout.log"
    $stderr = Join-Path $logDir "naivefi_${Tag}_stderr.log"

    Write-Host "[$(Get-Date -Format HH:mm:ss)] Launching driver ($Tag, resume=$Resume)..."
    $p = Start-Process -FilePath "python" -ArgumentList $driverArgs `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError  $stderr `
        -PassThru
    return [PSCustomObject]@{ Proc=$p; Stdout=$stdout; Stderr=$stderr }
}

# --- launch initial run ---
$t0 = Get-Date
$initial = Start-Driver -Tag "initial"

# --- wait delay ---
$delaySec = $DelayMinutes * 60
$injectAt = $t0.AddSeconds($delaySec)

Write-Host "[$(Get-Date -Format HH:mm:ss)] Waiting $delaySec s before kill..." -ForegroundColor Yellow
while ((Get-Date) -lt $injectAt) {
    $remain = [int]($injectAt - (Get-Date)).TotalSeconds
    $elapsed = [int]($delaySec - $remain)
    Write-Host "  [$(Get-Date -Format HH:mm:ss)] elapsed=${elapsed}s remaining=${remain}s"
    Start-Sleep -Seconds 15
}

# --- kill ---
$injectTime = Get-Date
Write-Host "[$(Get-Date -Format HH:mm:ss)] -> KILLING java..." -ForegroundColor Red
$javaProcs = Get-Process java -ErrorAction SilentlyContinue
$killedPids = @()
foreach ($jp in $javaProcs) {
    $killedPids += $jp.Id
    Stop-Process -Id $jp.Id -Force
    Write-Host "  killed Java PID $($jp.Id)"
}
if (-not $killedPids) {
    Write-Host "  WARNING: no Java process found at kill time" -ForegroundColor Yellow
}

# also kill any python subprocess (framework's whisperx children)
Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "  killed Python PID $($_.Id)"
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}

# --- wait for the initial driver to notice its child died ---
Start-Sleep -Seconds 5
if (-not $initial.Proc.HasExited) {
    Write-Host "[$(Get-Date -Format HH:mm:ss)] Initial driver still alive, waiting..." -ForegroundColor Yellow
    $initial.Proc.WaitForExit(60 * 1000) | Out-Null
}

# --- resume ---
$t1 = Get-Date
Write-Host "[$(Get-Date -Format HH:mm:ss)] Restarting with --resume..." -ForegroundColor Yellow
$restart = Start-Driver -Resume -Tag "restart"
$restart.Proc.WaitForExit() | Out-Null
$t2 = Get-Date

# --- results ---
$recoverySec = [math]::Round(($t2 - $t1).TotalSeconds, 2)
$totalSec    = [math]::Round(($t2 - $t0).TotalSeconds, 2)
$killedStr   = if ($killedPids) { $killedPids -join "," } else { "none" }

# --- read run_exit if it exists ---
$exitPath = Join-Path $artefacts "run_exit.json"
$runExit = if (Test-Path $exitPath) {
    Get-Content $exitPath -Raw | ConvertFrom-Json
} else { $null }

# --- discover fresh output files in smoke_test/output ---
$smokeOutput = Join-Path $ProjectRoot "smoke_test\output"
$discovered = @()
if (Test-Path $smokeOutput) {
    $discovered = Get-ChildItem $smokeOutput -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -gt $t1 } |
        Select-Object Name, Length
}

# --- sidecar ---
$sidecarData = [ordered]@{
    harness                   = "naive_fi_standalone"
    fault_class               = "F1_kill"
    condition                 = $Condition
    run_index                 = $RunIndex
    delay_minutes             = $DelayMinutes
    kill_epoch_ms             = [DateTimeOffset]::FromUnixTimeMilliseconds(
                                    [int64]($injectTime - [datetime]'1970-01-01').TotalMilliseconds
                                ).ToUnixTimeMilliseconds()
    resume_epoch_ms           = [DateTimeOffset]::FromUnixTimeMilliseconds(
                                    [int64]($t1 - [datetime]'1970-01-01').TotalMilliseconds
                                ).ToUnixTimeMilliseconds()
    end_epoch_ms              = [DateTimeOffset]::FromUnixTimeMilliseconds(
                                    [int64]($t2 - [datetime]'1970-01-01').TotalMilliseconds
                                ).ToUnixTimeMilliseconds()
    killed_java_pids          = $killedStr
    recovery_wall_clock_s     = $recoverySec
    total_wall_clock_s        = $totalSec
    run_elapsed_s             = if ($runExit) { $runExit.elapsed_s } else { $null }
    run_success               = if ($runExit) { $runExit.success } else { $null }
    discovered_output_files   = ($discovered | ForEach-Object { $_.Name }) -join "; "
}

$sidecarData | ConvertTo-Json -Depth 4 | Set-Content -Path $sidecar -Encoding UTF8

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Green
Write-Host "Sidecar: $sidecar"
Write-Host ""
Get-Content $sidecar