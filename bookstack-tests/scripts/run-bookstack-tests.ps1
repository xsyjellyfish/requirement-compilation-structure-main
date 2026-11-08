<#
.SYNOPSIS
    Runs the official ARC-Bench BookStack Playwright suite against a generated application.

.DESCRIPTION
    One entry point for every run shape required by the task:
      * every BookStack test, one .spec.ts file, or a single scenario title;
      * a machine-readable JSON result under ./reports plus the official HTML report;
      * screenshots, traces and videos are always requested for failed tests.

    The script never edits tests/ or requirements/. When -AppUrl differs from the URL baked
    into playwright.config.ts it re-invokes the official setup-playwright.cjs, so baseURL is
    changed by the upstream tool instead of by hand.

.PARAMETER AppUrl
    Entry URL of the application under test, for example http://127.0.0.1:3000.
    Omit it to reuse the URL already configured in playwright.config.ts.

.PARAMETER Spec
    A single target: 'REQ-4.3', 'REQ-4.3.spec.ts' or 'tests/REQ-4.3.spec.ts'.

.PARAMETER Grep
    Scenario title filter forwarded to Playwright's -g (regular expression).

.PARAMETER List
    List the discovered tests and exit without running them.

.PARAMETER AppDir
    Root of the generated application. When supplied, the script installs, builds,
    initialises and starts it, waits for the URL, and stops it after the run.

.PARAMETER SkipDbInit
    Do not run 'npm run db:init' when -AppDir is used (the server may initialise itself).

.PARAMETER StartupTimeout
    Seconds to wait for the application URL to answer.

.PARAMETER OpenReport
    Open the HTML report after the run in a separate window (npm run test:e2e:report).

.EXAMPLE
    pwsh -File ./scripts/run-bookstack-tests.ps1 -List

.EXAMPLE
    pwsh -File ./scripts/run-bookstack-tests.ps1 -AppUrl http://127.0.0.1:3000 -Spec REQ-4.3

.EXAMPLE
    pwsh -File ./scripts/run-bookstack-tests.ps1 -AppDir ../outputs/bookstack -AppUrl http://127.0.0.1:3000
#>
param(
    [string]$AppUrl = '',
    [string]$Spec = '',
    [string]$Grep = '',
    [switch]$List,
    [string]$AppDir = '',
    [switch]$SkipDbInit,
    [int]$StartupTimeout = 180,
    [switch]$OpenReport
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$reports = Join-Path $root 'reports'
if (-not (Test-Path -LiteralPath $reports)) { New-Item -ItemType Directory -Path $reports | Out-Null }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

function Invoke-Checked([string]$File, [string[]]$Arguments, [string]$WorkDir) {
    Push-Location $WorkDir
    try {
        & $File @Arguments
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) { throw "$File $($Arguments -join ' ') failed with exit code $code" }
}

function Get-ConfiguredBaseUrl {
    $configPath = Join-Path $root 'playwright.config.ts'
    if (-not (Test-Path -LiteralPath $configPath)) { return '' }
    $match = [regex]::Match((Get-Content -LiteralPath $configPath -Raw), 'baseURL:\s*"([^"]+)"')
    return $(if ($match.Success) { $match.Groups[1].Value } else { '' })
}

function Get-ArtifactPolicy {
    $configPath = Join-Path $root 'playwright.config.ts'
    $config = if (Test-Path -LiteralPath $configPath) { Get-Content -LiteralPath $configPath -Raw } else { '' }
    $read = {
        param([string]$Key)
        $match = [regex]::Match($config, "${Key}:\s*'([^']+)'")
        if ($match.Success) { $match.Groups[1].Value } else { 'unset' }
    }
    return [ordered]@{
        trace      = (& $read 'trace')
        screenshot = (& $read 'screenshot')
        video      = (& $read 'video')
    }
}

function Test-Url([string]$Url, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { return $true }
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    return $false
}

# ---------------------------------------------------------------- prerequisites
if (-not (Test-Path -LiteralPath (Join-Path $root 'node_modules'))) {
    Write-Host 'Installing test dependencies (node_modules is missing)...'
    Invoke-Checked 'npm.cmd' @('install', '--no-audit', '--no-fund') $root
}
if (-not (Test-Path -LiteralPath (Join-Path $root 'playwright.config.ts'))) {
    Write-Host 'Generating playwright.config.ts with the official setup-playwright.cjs...'
    $seedUrl = if ($AppUrl) { $AppUrl } else { 'http://127.0.0.1:3000' }
    Invoke-Checked 'node.exe' @((Join-Path $root 'setup-playwright.cjs'), $root, $seedUrl) $root
}

$baseUrl = Get-ConfiguredBaseUrl
if ($AppUrl -and $AppUrl.TrimEnd('/') -ne $baseUrl.TrimEnd('/')) {
    Write-Host "Pointing the suite at $AppUrl through the official setup-playwright.cjs..."
    Invoke-Checked 'node.exe' @((Join-Path $root 'setup-playwright.cjs'), $root, $AppUrl) $root
    $baseUrl = Get-ConfiguredBaseUrl
}
if (-not $baseUrl) { throw 'playwright.config.ts does not define use.baseURL' }
Write-Host "Application under test: $baseUrl"

# ---------------------------------------------------------------- optional app boot
$appProcess = $null
$appDatabase = ''
try {
    if ($AppDir) {
        $appRoot = (Resolve-Path -LiteralPath $AppDir).Path
        $appPackagePath = Join-Path $appRoot 'package.json'
        if (-not (Test-Path -LiteralPath $appPackagePath)) { throw "No package.json in $appRoot" }
        $appPackage = Get-Content -LiteralPath $appPackagePath -Raw | ConvertFrom-Json
        $port = ([System.Uri]$baseUrl).Port
        if ($port -le 0) { $port = 3000 }

        if (-not (Test-Path -LiteralPath (Join-Path $appRoot 'node_modules'))) {
            Write-Host "Installing application dependencies in $appRoot..."
            Invoke-Checked 'npm.cmd' @('install', '--no-audit', '--no-fund') $appRoot
        }
        Write-Host 'Building the application...'
        Invoke-Checked 'npm.cmd' @('run', 'build') $appRoot
        if (-not $SkipDbInit -and $appPackage.scripts.'db:init') {
            Write-Host 'Initialising the database...'
            Invoke-Checked 'npm.cmd' @('run', 'db:init') $appRoot
        }

        $env:PORT = "$port"
        $appLog = Join-Path $reports "app-$stamp.log"
        Write-Host "Starting the application on port $port (log: $appLog)..."
        $appProcess = Start-Process -FilePath 'npm.cmd' -ArgumentList @('run', 'start') -WorkingDirectory $appRoot `
            -PassThru -WindowStyle Hidden -RedirectStandardOutput $appLog -RedirectStandardError "$appLog.err"
        if (-not (Test-Url $baseUrl $StartupTimeout)) {
            throw "The application did not answer on $baseUrl within $StartupTimeout s (see $appLog)."
        }
        Write-Host 'Application is answering.'
        $guessedDatabase = Join-Path $appRoot 'backend\data\app.db'
        if (Test-Path -LiteralPath $guessedDatabase) { $appDatabase = $guessedDatabase }
    } elseif (-not $List -and -not (Test-Url $baseUrl 5)) {
        Write-Warning "Nothing answers on $baseUrl. Start the generated application first, or pass -AppDir <generated app root>."
    }

    # ------------------------------------------------------------ playwright run
    $playwrightArgs = @('playwright', 'test')
    if ($Spec) {
        if ($Spec -match '^REQ-[\d.]+$') { $playwrightArgs += "tests/$Spec.spec.ts" }
        elseif ($Spec -match '\.spec\.ts$' -and $Spec -notmatch '[\\/]') { $playwrightArgs += "tests/$Spec" }
        else { $playwrightArgs += $Spec }
    }
    if ($Grep) { $playwrightArgs += @('-g', $Grep) }
    if ($List) { $playwrightArgs += '--list' }

    $resultsJson = Join-Path $reports "results-$stamp.json"
    $savedEnv = @{}
    if (-not $List) {
        # Playwright has no --screenshot/--video CLI switch; that policy lives in
        # playwright.config.ts. --trace is passed here too so traces survive a regenerated config.
        #
        # Passing --reporter on the command line drops the options the config gave the reporter
        # (the html reporter would then default to open:'on-failure' and block the run behind a
        # local web server). The output folder and 'never open' behaviour are restored through
        # the documented reporter environment variables instead of editing the official config.
        $playwrightArgs += @('--trace=retain-on-failure')
        $playwrightArgs += @('--reporter=list,html,json')
        $envOverrides = [ordered]@{
            PLAYWRIGHT_JSON_OUTPUT_NAME = $resultsJson
            PLAYWRIGHT_HTML_OUTPUT_DIR  = 'playwright-report'
            PLAYWRIGHT_HTML_OPEN        = 'never'
        }
        foreach ($key in $envOverrides.Keys) {
            $savedEnv[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
            [Environment]::SetEnvironmentVariable($key, $envOverrides[$key], 'Process')
        }
    }
    $policy = Get-ArtifactPolicy
    Write-Host "Artifact policy: trace=$($policy.trace) screenshot=$($policy.screenshot) video=$($policy.video)"
    if ($policy.screenshot -ne 'only-on-failure' -or $policy.video -ne 'retain-on-failure') {
        Write-Warning 'playwright.config.ts no longer retains screenshots/videos on failure. Re-run setup-playwright.ps1 to restore the official policy.'
    }

    Write-Host "Running: npx $($playwrightArgs -join ' ')"
    $startedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    Push-Location $root
    try {
        & npx.cmd @playwrightArgs
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
        foreach ($key in $savedEnv.Keys) {
            [Environment]::SetEnvironmentVariable($key, $savedEnv[$key], 'Process')
        }
    }

    $runRecord = [ordered]@{
        startedAt   = $startedAt
        finishedAt  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        baseUrl     = $baseUrl
        spec        = $Spec
        grep        = $Grep
        appDir      = $AppDir
        appDatabase = $appDatabase
        artifacts   = $policy
        exitCode    = $exitCode
        resultsJson = $(if (Test-Path -LiteralPath $resultsJson) { $resultsJson } else { '' })
    }
    $runRecordPath = Join-Path $reports "run-$stamp.json"
    [System.IO.File]::WriteAllText($runRecordPath, (($runRecord | ConvertTo-Json -Depth 4) + "`n"),
        (New-Object System.Text.UTF8Encoding($false)))

    Write-Host ''
    Write-Host "Playwright exit code: $exitCode"
    Write-Host "HTML report:    $(Join-Path $root 'playwright-report\index.html')"
    Write-Host "Artifacts:      $(Join-Path $root 'test-results')  (screenshot/trace/video for each failed test)"
    if (-not $List) { Write-Host "JSON results:   $resultsJson" }
    Write-Host "Run record:     $runRecordPath"
    if (-not $List) {
        Write-Host "Analyse:        powershell -File ./scripts/analyze-bookstack-failures.ps1 -Results '$resultsJson'"
        Write-Host 'Show report:    npm run test:e2e:report'
    }
    if ($OpenReport -and -not $List) {
        Start-Process -FilePath 'npm.cmd' -ArgumentList @('run', 'test:e2e:report') -WorkingDirectory $root
    }

    exit $exitCode
} finally {
    if ($appProcess -and -not $appProcess.HasExited) {
        Write-Host 'Stopping the application...'
        & taskkill.exe /PID $appProcess.Id /T /F | Out-Null
    }
}

