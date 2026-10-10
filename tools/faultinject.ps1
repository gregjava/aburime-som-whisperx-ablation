<#
.SYNOPSIS
Fault-injection harness for ASoM v4.0.4 fault-tolerance evaluation (v3).

.DESCRIPTION
Launches a condition from experiments\manifest_fi.csv, polls the framework's
work directory for progress markers, injects a fault at the target segment,
and (for process-kill faults) restarts with --resume to exercise the recovery
path. Writes a fault_injection.json sidecar into the run's artefacts dir.

Fault classes:
  F1_kill              - hard-kill the Java process at the target segment
  F2a_corrupt_progress - append garbage to progress.dat mid-run
  F2b_corrupt_result   - flip bytes in the most recently written result_<i>.json
  F3_subproc           - hard-kill the Python subprocess handling the target segment

The harness NEVER writes to core_sweep\; all output goes to a separate
fi_sweep\ tree so the paper's 207-run dataset stays intact.

.EXAMPLE
.\faultinject.ps1 -Fault F1_kill -Condition adaptive_full_n1_t15min_fi -InjectionSegment 14
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('F1_kill','F2a_corrupt_progress','F2b_corrupt_result','F3_subproc')]
    [string]$Fault,

    [Parameter(Mandatory=$true)]
    [string]$Condition,

    [Parameter(Mandatory=$true)]
    [int]$InjectionSegment,

    [int]$RunIndex = 1,

    [string]$ProjectRoot = "C:\Users\GREG-PC\Documents\NetBeansProjects\AburimeSoundManager",
    [string]$OutDir      = "experiments\fi_sweep",

    [int]$TimeoutMinutes = 180,
    [int]$WorkDirWaitMinutes = 5,

    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# --- guards ---
if ([string]::IsNullOrWhiteSpace($Condition)) { throw "Condition is empty" }
if ($InjectionSegment -lt 0)                  { throw "InjectionSegment must be >= 0" }

# --- paths ---
$manifest    = Join-Path $ProjectRoot "experiments\manifest_fi.csv"
$outPath     = Join-Path $ProjectRoot $OutDir
$driverPath  = Join-Path $ProjectRoot "experiments\driver.py"
$runDir      = Join-Path $outPath ("{0}\run_{1:D2}" -f $Condition, $RunIndex)
$artefacts   = Join-Path $runDir "artefacts"
$sidecarPath = Join-Path $artefacts "fault_injection.json"

# Log files live OUTSIDE the artefacts dir so the driver's post-processing
# (shutil.rmtree) does not fail on an open file handle. This was the cause
# of the PermissionError in the pilot run.
$logDir = Join-Path $outPath "_logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

foreach ($p in @($manifest, $driverPath)) {
    if (-not (Test-Path $p)) { throw "Required file missing: $p" }
}
if (-not (Test-Path $runDir))    { New-Item -ItemType Directory -Path $runDir -Force | Out-Null }
if (-not (Test-Path $artefacts)) { New-Item -ItemType Directory -Path $artefacts -Force | Out-Null }

$stamp = Get-Date -Format yyyyMMdd_HHmmss
$stdoutLog = Join-Path $logDir "driver_${Condition}_run${RunIndex}_${stamp}_stdout.log"
$stderrLog = Join-Path $logDir "driver_${Condition}_run${RunIndex}_${stamp}_stderr.log"

Write-Host "=== Fault injection run ===" -ForegroundColor Cyan
Write-Host "Fault      : $Fault"
Write-Host "Condition  : $Condition"
Write-Host "Run index  : $RunIndex"
Write-Host "Inject at  : segment $InjectionSegment"
Write-Host "Out dir    : $outPath"
Write-Host "Log dir    : $logDir"
Write-Host ""

# --- smoke_test/output is where the framework writes its transcripts ---
$smokeOutput = Join-Path $ProjectRoot "smoke_test\output"

# --- utilities ---
function Find-WorkDir {
    param([datetime]$After)
    Get-ChildItem $env:TEMP -Directory -Filter "segment_work_*" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -gt $After } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}

function Get-ProgressCount {
    param([string]$WorkDirPath)
    if (-not (Test-Path $WorkDirPath)) { return 0 }
    $pd = Get-ChildItem $WorkDirPath -Recurse -Filter "progress.dat" -ErrorAction SilentlyContinue |
          Select-Object -First 1
    if ($pd) {
        try {
            return (Get-Content $pd.FullName -ErrorAction Stop |
                    Where-Object { $_ -match '\S' }).Count
        } catch { return 0 }
    }
    return (Get-ChildItem $WorkDirPath -Recurse -Filter "result_*.json" -ErrorAction SilentlyContinue).Count
}

function Get-JavaPid {
    param([string]$ConditionHint)
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'java.exe'" -ErrorAction SilentlyContinue
    $match = $procs | Where-Object { $_.CommandLine -like "*$ConditionHint*" } | Select-Object -First 1
    if (-not $match) { $match = $procs | Select-Object -First 1 }
    if ($match) { return $match.ProcessId } else { return $null }
}

function Get-PythonPid {
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue
    $match = $procs | Where-Object {
        $_.CommandLine -like '*whisperx*' -or $_.CommandLine -like '*segment*'
    } | Select-Object -First 1
    if ($match) { return $match.ProcessId } else { return $null }
}

function Invoke-Driver {
    param(
        [switch]$Resume,
        [string]$Tag = ""
    )
    $driverArgs = @(
        $driverPath,
        "--manifest",       $manifest,
        "--out",            $outPath,
        "--only-condition", $Condition,
        "--only-run",       $RunIndex.ToString()
    )
    if ($Resume) { $driverArgs += "--resume" }

    $outLog = Join-Path $logDir "driver_${Condition}_run${RunIndex}_${Tag}_stdout.log"
    $errLog = Join-Path $logDir "driver_${Condition}_run${RunIndex}_${Tag}_stderr.log"

    Write-Host "[$(Get-Date -Format HH:mm:ss)] Launching driver (resume=$Resume)..." -ForegroundColor Yellow
    $proc = Start-Process -FilePath "python" -ArgumentList $driverArgs `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $outLog `
        -RedirectStandardError  $errLog `
        -PassThru
    return [PSCustomObject]@{ Proc = $proc; Stdout = $outLog; Stderr = $errLog }
}

# --- guard: refuse if another java/python is running ---
$existing = Get-Process java, python -ErrorAction SilentlyContinue |
    Where-Object { $_.StartTime -lt (Get-Date).AddSeconds(-30) }
if ($existing) {
    Write-Host ""
    Write-Host "ABORT: Other java/python processes are running:" -ForegroundColor Red
    $existing | Select-Object Id, Name, StartTime | Format-Table -AutoSize
    Write-Host "Stop them first, or this run will collide." -ForegroundColor Red
    throw "Aborted: concurrent framework instance detected"
}

# --- launch initial driver ---
$startEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()

if ($DryRun) {
    Write-Host "DRY RUN: would launch driver for $Condition" -ForegroundColor Magenta
    $initial = $null
    Start-Sleep -Seconds 2
} else {
    $initial = Invoke-Driver -Tag "initial"
}

# --- wait for work dir ---
$launchTime = Get-Date
$manifestRow = Import-Csv $manifest | Where-Object { $_.condition_id -eq $Condition } | Select-Object -First 1
$expectedClip = if ($manifestRow) {
    $basename = [System.IO.Path]::GetFileNameWithoutExtension($manifestRow.input_files)
    $basename -replace '_processed_[0-9a-fA-F]{6,16}$', ''
} else { $null }
Write-Host "[$(Get-Date -Format HH:mm:ss)] Expecting work dir prefix: segment_work_${expectedClip}_" -ForegroundColor Yellow
$workDir = $null
$deadline = (Get-Date).AddMinutes($WorkDirWaitMinutes)
Write-Host "[$(Get-Date -Format HH:mm:ss)] Waiting for work dir (up to $WorkDirWaitMinutes min)..." -ForegroundColor Yellow

do {
    Start-Sleep -Milliseconds 500
    $workDir = Find-WorkDir -After $launchTime -ExpectedClip $expectedClip
    if ($workDir) {
        Write-Host "[$(Get-Date -Format HH:mm:ss)] Work dir: $($workDir.FullName)" -ForegroundColor Green
        break
    }
} while ((Get-Date) -lt $deadline)

if ($DryRun) {
    Write-Host "DRY RUN: skipping run" -ForegroundColor Magenta
    return
}

if (-not $workDir) { throw "Work dir never appeared within $WorkDirWaitMinutes min" }

# --- poll for injection ---
$injected    = $false
$injectEpoch = $null
$injectData  = @{}
$lastCount   = -1
$segmentsAtInjection = -1

Write-Host "[$(Get-Date -Format HH:mm:ss)] Polling for segment $InjectionSegment..." -ForegroundColor Yellow

$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
while (-not $injected) {
    Start-Sleep -Seconds 2
    $count = Get-ProgressCount -WorkDirPath $workDir.FullName
    if ($count -ne $lastCount) {
        Write-Host "  [$(Get-Date -Format HH:mm:ss)] segments complete: $count / target $($InjectionSegment + 1)"
        $lastCount = $count
    }

    if ($count -ge ($InjectionSegment + 1)) {
        Write-Host "[$(Get-Date -Format HH:mm:ss)] -> INJECTING $Fault" -ForegroundColor Red
        $injectEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        $segmentsAtInjection = $count

        # --- capture the work dir NOW, before the framework cleans it on success ---
        $captureDir = Join-Path $artefacts "workdir_capture"
        if (-not (Test-Path $captureDir)) {
            try {
                Copy-Item $workDir.FullName $captureDir -Recurse -Force
                Write-Host "  captured work dir to $captureDir"
            } catch {
                Write-Host "  could not capture work dir: $_" -ForegroundColor Yellow
            }
        }

        switch ($Fault) {
            'F1_kill' {
                $kpid = Get-JavaPid -ConditionHint $Condition
                if ($kpid) {
                    $injectData['killed_java_pid'] = $kpid
                    taskkill /F /PID $kpid | Out-Null
                    Write-Host "  killed Java PID $kpid" -ForegroundColor Red
                } else {
                    Write-Host "  WARNING: no Java PID found" -ForegroundColor Red
                }
            }
            'F2a_corrupt_progress' {
                $pd = Get-ChildItem $workDir.FullName -Recurse -Filter "progress.dat" -ErrorAction SilentlyContinue |
                      Select-Object -First 1
                if ($pd) {
                    Add-Content -Path $pd.FullName -Value "GARBAGE_CORRUPT_LINE_XYZ"
                    $injectData['corrupted_file'] = $pd.FullName
                    $injectData['corruption']     = "appended garbage line"
                    Write-Host "  corrupted $($pd.FullName)" -ForegroundColor Red
                } else {
                    Write-Host "  WARNING: progress.dat not found" -ForegroundColor Yellow
                }
            }
            'F2b_corrupt_result' {
                $rf = Get-ChildItem $workDir.FullName -Recurse -Filter "result_*.json" -ErrorAction SilentlyContinue |
                      Sort-Object LastWriteTime -Descending | Select-Object -First 1
                if ($rf) {
                    $bytes = [System.IO.File]::ReadAllBytes($rf.FullName)
                    $bytes[0] = $bytes[0] -bxor 0xFF
                    [System.IO.File]::WriteAllBytes($rf.FullName, $bytes)
                    $injectData['corrupted_file'] = $rf.FullName
                    Write-Host "  corrupted $($rf.FullName)" -ForegroundColor Red
                } else {
                    Write-Host "  WARNING: no result_*.json found" -ForegroundColor Red
                }
            }
            'F3_subproc' {
                $ppid = Get-PythonPid
                if ($ppid) {
                    $injectData['killed_python_pid'] = $ppid
                    Stop-Process -Id $ppid -Force -ErrorAction SilentlyContinue
                    Write-Host "  killed Python PID $ppid" -ForegroundColor Red
                } else {
                    Write-Host "  WARNING: no python.exe found" -ForegroundColor Red
                }
            }
        }
        $injected = $true
    }
}

# --- wait for completion / recovery ---
$restartEpoch = $null

if ($Fault -eq 'F1_kill') {
    $initial.Proc.WaitForExit(60 * 1000) | Out-Null
    Write-Host "[$(Get-Date -Format HH:mm:ss)] Framework terminated. Restarting with --resume..." -ForegroundColor Yellow
    $restartEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $restart = Invoke-Driver -Resume -Tag "restart"
    $restart.Proc.WaitForExit() | Out-Null
    Write-Host "[$(Get-Date -Format HH:mm:ss)] Restart driver exited." -ForegroundColor Green
    $restartStdout = $restart.Stdout
    $restartStderr = $restart.Stderr
    $restartExitCode = $restart.Proc.ExitCode
} else {
    Write-Host "[$(Get-Date -Format HH:mm:ss)] Waiting for in-process recovery..." -ForegroundColor Yellow
    $initial.Proc.WaitForExit() | Out-Null
    Write-Host "[$(Get-Date -Format HH:mm:ss)] Driver exited." -ForegroundColor Green
    $restartStdout = $null
    $restartStderr = $null
    $restartExitCode = $initial.Proc.ExitCode
}

$endEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()

# --- post-run artefact discovery ---
# Scan smoke_test\output for files modified since the restart (or since launch
# for non-F1 faults). These are the framework's real output files.
$discovered = @()
if (Test-Path $smokeOutput) {
    $threshold = if ($restartEpoch) {
        [DateTimeOffset]::FromUnixTimeMilliseconds($restartEpoch).LocalDateTime
    } else {
        $launchTime
    }
    $discovered = Get-ChildItem $smokeOutput -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -gt $threshold } |
        Select-Object Name, Length, LastWriteTime
}

# Heuristic for success: a fresh .txt and .srt appeared, OR run_exit.json says success
$hasTxt = @($discovered | Where-Object { $_.Name -like "*.txt" }).Count -gt 0
$hasSrt = @($discovered | Where-Object { $_.Name -like "*.srt" }).Count -gt 0

$exitJsonPath = Join-Path $artefacts "run_exit.json"
$runExit = if (Test-Path $exitJsonPath) {
    Get-Content $exitJsonPath -Raw | ConvertFrom-Json
} else { $null }

$runSuccess = $false
if ($hasTxt -and $hasSrt)               { $runSuccess = $true }
elseif ($runExit -and $runExit.success) { $runSuccess = $true }

# --- try to read the driver's own run_exit.json (may not exist for F1) ---
$exitJsonPath = Join-Path $artefacts "run_exit.json"
$runExit = if (Test-Path $exitJsonPath) {
    Get-Content $exitJsonPath -Raw | ConvertFrom-Json
} else { $null }

# --- sidecar ---
$sidecar = [ordered]@{
    fault_class                = $Fault
    condition                  = $Condition
    run_index                  = $RunIndex
    injection_segment          = $InjectionSegment
    injection_epoch_ms         = $injectEpoch
    restart_epoch_ms           = $restartEpoch
    end_epoch_ms               = $endEpoch
    work_dir                   = $workDir.FullName
    segments_at_injection      = $segmentsAtInjection
    restart_required           = ($Fault -eq 'F1_kill')
    recovery_wall_clock_s      = if ($restartEpoch) {
                                     [math]::Round(($endEpoch - $restartEpoch) / 1000.0, 2)
                                 } else { $null }
    run_success                = $runSuccess
    run_elapsed_s              = if ($runExit) { $runExit.elapsed_s } else { $null }
    driver_exit_code           = $restartExitCode
    discovered_output_files    = ($discovered | ForEach-Object { $_.Name }) -join "; "
    dry_run                    = [bool]$DryRun
    harness_version            = "v3"
}
foreach ($k in $injectData.Keys) { $sidecar[$k] = $injectData[$k] }

$sidecar | ConvertTo-Json -Depth 4 | Set-Content -Path $sidecarPath -Encoding UTF8

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Green
Write-Host "Sidecar: $sidecarPath"
Write-Host "Logs   : $logDir"
if ($restartStdout) { Write-Host "Restart stdout: $restartStdout" }
if ($restartStderr) { Write-Host "Restart stderr: $restartStderr" }
Write-Host ""
Get-Content $sidecarPath