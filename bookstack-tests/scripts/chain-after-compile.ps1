<#
.SYNOPSIS
    Waits for the running compilation to finish, then reports its exit state and (optionally)
    runs the official BookStack Playwright suite plus the failure analysis.

.DESCRIPTION
    The compiler run is expensive, so it must never be interrupted or duplicated. This script
    only observes it: it blocks until the wrapper process that started 'uv run main.py' exits,
    reads the exit code the wrapper recorded, summarises '.arc/compilation.json' (status,
    deployability, per-stage duration and tokens), and then chains the already-validated steps:

        run-bookstack-tests.ps1 -AppDir <dir> -AppUrl <url>   (installs, builds, starts, tests,
                                                               stops the application)
        npm run test:e2e:analyze                              (A/B/C attribution report)

    Nothing here edits the compiler, the tests or the requirements. If the compilation failed,
    the scene (log paths, status, failing checks) is recorded and the script stops without
    retrying.

.PARAMETER WrapperPid
    PID of the PowerShell wrapper running 'generate-bookstack-app.ps1 -ConfirmRun'. 0 discovers
    it from the process table.

.PARAMETER AppDir
    Generated application directory. Defaults to <repo>/outputs/bookstack.

.PARAMETER AppUrl
    Entry URL used for the suite. Defaults to http://127.0.0.1:3000.

.PARAMETER CompileStdout
    File that captured the wrapper's stdout (holds the 'Compiler exit code: N' line).
    Defaults to $env:TEMP\bookstack-compile.out. When the file is absent - the usual case
    when the chain is started after the compilation already ended - the exit code is read
    from '.arc/compilation.json' (exit.code) so a finished run is still reported exactly.

.PARAMETER CompileOnly
    Skip the Playwright suite even when the application is deployable.
#>
param(
    [int]$WrapperPid = 0,
    [string]$AppDir = '',
    [string]$AppUrl = 'http://127.0.0.1:3000',
    [string]$CompileStdout = '',
    [switch]$CompileOnly
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$repo = Split-Path -Parent $root
if (-not $AppDir) { $AppDir = Join-Path $repo 'outputs\bookstack' }
if (-not $CompileStdout) { $CompileStdout = Join-Path $env:TEMP 'bookstack-compile.out' }
$reports = Join-Path $root 'reports'
if (-not (Test-Path -LiteralPath $reports)) { New-Item -ItemType Directory -Path $reports | Out-Null }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$chainStarted = Get-Date
$summaryPath = Join-Path $reports "chain-$stamp.json"

function Say([string]$Message) { Write-Host ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Message) }

function Find-CompilerWrapper {
    # Return the PowerShell process that launched generate-bookstack-app.ps1, if it is alive.
    $found = Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'generate-bookstack-app\.ps1' }
    return @($found | Select-Object -First 1)
}

function Find-CompilerPython {
    # Return the python process actually running main.py for this output directory.
    $found = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'main\.py' -and $_.CommandLine -match 'outputs/bookstack' }
    return @($found | Select-Object -First 1)
}

function Wait-ForExit([int]$ProcessId, [string]$Label) {
    # Block (without polling the transcript) until the process disappears.
    if (-not $ProcessId) { return }
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) { Say "$Label (pid $ProcessId) already exited."; return }
    Say "Waiting for $Label (pid $ProcessId) to finish; no interruption, no transcript polling."
    $process.WaitForExit()
    Say "$Label (pid $ProcessId) exited."
}

function Read-CompileExitCode {
    # Read the exit code the wrapper printed, if it is available.
    $code = $null
    if (Test-Path -LiteralPath $CompileStdout) {
        $match = [regex]::Match((Get-Content -LiteralPath $CompileStdout -Raw), 'Compiler exit code:\s*(-?\d+)')
        if ($match.Success) { $code = [int]$match.Groups[1].Value }
    }
    return $code
}

function Read-CompilationReport {
    # Summarise status, deployability and per-stage time/tokens from .arc/compilation.json.
    $path = Join-Path $AppDir '.arc\compilation.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $json = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    $stages = @()
    # '.arc/compilation.json' keys the passes by module ("12_implement_frontend_tdd"); the
    # record itself carries no name, so the property name is the stage label. Older or
    # array-shaped reports keep their own field.
    $entries = if ($json.stages -is [System.Array]) {
        @($json.stages | ForEach-Object { [pscustomobject]@{
            Label = $(if ($_.stage) { $_.stage } else { $_.name }); Value = $_ } })
    } else {
        @($json.stages.PSObject.Properties | ForEach-Object { [pscustomobject]@{
            Label = $_.Name; Value = $_.Value } })
    }
    foreach ($pair in $entries) {
        $entry = $pair.Value
        if (-not $entry) { continue }
        $usage = $entry.model_usage
        $stages += [ordered]@{
            stage   = $pair.Label
            status  = $entry.status
            seconds = $entry.duration_seconds
            calls   = $(if ($usage) { $usage.Calls } else { $null })
            tokens  = $(if ($usage) { $usage.Total } else { $null })
        }
    }
    return [ordered]@{
        path         = $path
        status       = $json.status
        deployable   = $json.deployable
        delivery     = $json.delivery
        failedChecks = @($json.failed_checks)
        blockedChecks = @($json.blocked_checks)
        startedAt    = $json.started_at
        finishedAt   = $json.finished_at
        seconds      = $json.duration_seconds
        exitRecord   = $json.exit
        stageCount   = $stages.Count
        failedStages = @($stages | Where-Object { $_.status -and $_.status -notin @('GREEN', 'NOT_APPLICABLE') } |
            ForEach-Object { "$($_.stage)=$($_.status)" })
        stages       = $stages
        failures     = @($json.failures | ForEach-Object {
            [ordered]@{ kind = $_.kind; id = $_.id; stage = $_.stage; status = $_.status } })
    }
}

$wrapperPid = $WrapperPid
$pythonPid = 0
if (-not $wrapperPid) {
    $wrapper = Find-CompilerWrapper
    if ($wrapper) { $wrapperPid = [int]$wrapper.ProcessId }
}
$python = Find-CompilerPython
if ($python) { $pythonPid = [int]$python.ProcessId }

Say "Compilation in progress: wrapper pid='$wrapperPid' compiler pid='$pythonPid' output='$AppDir'"
Wait-ForExit -ProcessId $pythonPid -Label 'compiler python'
Wait-ForExit -ProcessId $wrapperPid -Label 'compiler wrapper'

$compileExit = Read-CompileExitCode
$exitSource = 'wrapper transcript'
$report = Read-CompilationReport
if ($null -eq $compileExit -and $report -and $report.exitRecord -and
    $null -ne $report.exitRecord.code) {
    # The wrapper is gone and its transcript was not captured: the compilation itself still
    # recorded the verdict it returned, so the chain reports that instead of 'unknown'.
    $compileExit = [int]$report.exitRecord.code
    $exitSource = 'compilation.json exit.code'
}
Say "Compiler exit code: $(if ($null -eq $compileExit) { 'unknown' } else { "$compileExit (from $exitSource)" })"
if ($report) {
    Say "Report status: $($report.status) | deployable: $($report.deployable) | duration: $($report.seconds) s | stages: $($report.stageCount)"
    if ($report.delivery) {
        Say "Delivery: $($report.delivery.status) | failed checks: $($report.failedChecks -join ', ')"
    }
    if ($report.blockedChecks.Count -gt 0) {
        Say "Checks blocked by an upstream artifact: $($report.blockedChecks -join ', ')"
    }
    foreach ($stage in $report.stages) {
        Say ("  {0,-34} {1,-22} {2,8} s {3,10} tokens" -f $stage.stage, $stage.status, $stage.seconds,
            $(if ($null -eq $stage.tokens) { '-' } else { $stage.tokens }))
    }
} else {
    Say "No .arc/compilation.json under $AppDir - the compilation produced no report."
}

$deployable = $false
if ($report -and $report.deployable -eq $true) { $deployable = $true }
elseif (-not $report -and (Test-Path -LiteralPath (Join-Path $AppDir 'package.json'))) { $deployable = $true }

$chain = [ordered]@{
    stamp           = $stamp
    wrapperPid      = $wrapperPid
    compilerPid     = $pythonPid
    compileExitCode = $compileExit
    reportStatus    = $(if ($report) { $report.status } else { $null })
    deliveryStatus  = $(if ($report -and $report.delivery) { $report.delivery.status } else { $null })
    deployable      = $deployable
    compileSummary  = $report
    tests           = $null
    analyzeExitCode = $null
}

if (-not $deployable) {
    Say 'The compilation did not leave a deployable application. Saving the scene and stopping (no retry).'
} elseif ($CompileOnly) {
    Say 'CompileOnly was requested; the Playwright suite is skipped.'
} else {
    Say "Running the official BookStack suite against $AppUrl (runner installs, builds, starts, tests, stops)."
    Push-Location $root
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'run-bookstack-tests.ps1') `
            -AppDir $AppDir -AppUrl $AppUrl
        $runnerExit = $LASTEXITCODE
    } finally { Pop-Location }
    Say "Runner exit code: $runnerExit"

    $latestResults = Get-ChildItem -Path (Join-Path $reports 'results-*.json') -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime | Select-Object -Last 1
    # A run that died before Playwright (a failed 'db:init', a build error) leaves no new
    # results file. Analysing the previous run's JSON would attribute old failures to this
    # run, so the analysis only runs on a file this chain actually produced.
    $freshResults = if ($latestResults -and $latestResults.LastWriteTime -ge $chainStarted) { $latestResults } else { $null }
    $chain.tests = [ordered]@{
        exitCode    = $runnerExit
        resultsJson = $(if ($freshResults) { $freshResults.FullName } else { '' })
        report      = (Join-Path $root 'playwright-report\index.html')
        artifacts   = (Join-Path $root 'test-results')
    }

    if (-not $freshResults) {
        Say 'The suite produced no results-*.json (it stopped before Playwright); skipping the analysis so an older run is not re-reported.'
    } else {
        Say "Running the failure analysis on $($freshResults.Name) ..."
        Push-Location $root
        try {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
                (Join-Path $PSScriptRoot 'analyze-bookstack-failures.ps1') -Results $freshResults.FullName
            $chain.analyzeExitCode = $LASTEXITCODE
        } finally { Pop-Location }
        Say "Analyze exit code: $($chain.analyzeExitCode)"
    }
}

[System.IO.File]::WriteAllText($summaryPath, (($chain | ConvertTo-Json -Depth 8) + "`n"),
    (New-Object System.Text.UTF8Encoding($false)))
Say "Chain summary: $summaryPath"
