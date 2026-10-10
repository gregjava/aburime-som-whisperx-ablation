<#
.SYNOPSIS
Generates the fault-injection manifest.

Creates novel condition IDs (suffixed "_fi") so the driver will execute them
without touching the paper's 207-run sweep. Writes to experiments\manifest_fi.csv.
#>

[CmdletBinding()]
param(
    [string]$ProjectRoot = "C:\Users\GREG-PC\Documents\NetBeansProjects\AburimeSoundManager"
)

$ErrorActionPreference = 'Stop'
$outPath = Join-Path $ProjectRoot "experiments\manifest_fi.csv"

# Corpus paths — match the paper's corpus layout
$corpusRoot   = "C:\Users\GREG-PC\testinput_corpus"
$clip15min    = Join-Path $corpusRoot "15min\corpus_15min_01.wav"
$clip10min    = Join-Path $corpusRoot "10min\corpus_10min_01.wav"
$clip5min     = Join-Path $corpusRoot "5min\corpus_5min_01.wav"

# Header must match experiments\manifest.csv exactly
$header = "condition_id,concurrency_mode,checkpoint,retry,adaptive,skip_segmentation,batch_size,clip_duration_min,input_files,n_runs"

$rows = @()

# NAVIVE arm: no checkpoint, no retry, 1 worker (naive concurrency)
$rows += "naive_full_n1_t15min_fi,naive,false,false,false,false,1,15,$clip15min,1"
$rows += "naive_full_n1_t10min_fi,naive,false,false,false,false,1,10,$clip10min,1"

# ADAPTIVE arm: checkpoint + retry, adaptive concurrency
$rows += "adaptive_full_n1_t15min_fi,adaptive,true,true,true,false,1,15,$clip15min,1"
$rows += "adaptive_full_n1_t10min_fi,adaptive,true,true,true,false,1,10,$clip10min,1"

# STATIC arm (for the static-at-ceiling control experiment, if run later):
# adaptive framework but concurrency fixed at ceiling — no controller.
# (Framework support TBD; do not enable until confirmed.)
# $rows += "static_full_n1_t15min_fi,naive,true,true,true,false,1,15,$clip15min,1"

$content = @($header) + $rows
$content -join "`n" | Set-Content -Path $outPath -Encoding UTF8

Write-Host "Wrote $outPath" -ForegroundColor Green
Write-Host ""
Get-Content $outPath | Format-Table -AutoSize