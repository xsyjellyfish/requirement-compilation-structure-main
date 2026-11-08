<#
.SYNOPSIS
    Prepares the Ver3 compilation of the official BookStack requirements, and runs it only
    when -ConfirmRun is passed.

.DESCRIPTION
    A full compilation is 13 consecutive LLM passes and can take tens of minutes and real
    token budget. Without -ConfirmRun this script only prints the exact command, the resolved
    paths and the prerequisite checks, then exits 0. Nothing is generated and no model call is
    made. Pass -ConfirmRun yourself when you have decided to pay for the run.

.PARAMETER Requirements
    Requirement directory. Defaults to ../requirements next to this test environment, which is
    the untouched official ARC-Bench BookStack package.

.PARAMETER OutputDir
    Where the generated application is written. Defaults to <repo>/outputs/bookstack.

.PARAMETER Port
    Backend port baked into the generated application. Must match the URL used by the tests.

.PARAMETER ConfirmRun
    Actually execute the compiler.

.PARAMETER ModelLog
    Forward --model-log to the compiler (detailed per-call prompt/response logs).

.EXAMPLE
    powershell -File ./scripts/generate-bookstack-app.ps1
    # prints the command and the readiness checks only

.EXAMPLE
    powershell -File ./scripts/generate-bookstack-app.ps1 -ConfirmRun
    # performs the expensive full compilation
#>
param(
    [string]$Requirements = '',
    [string]$OutputDir = '',
    [int]$Port = 3000,
    [switch]$ConfirmRun,
    [switch]$ModelLog,
    [string]$LogFile = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$repo = Split-Path -Parent $root
if (-not $Requirements) { $Requirements = Join-Path $root 'requirements' }
if (-not $OutputDir) { $OutputDir = Join-Path $repo 'outputs\bookstack' }

$requirementPath = (Resolve-Path -LiteralPath $Requirements).Path
if (-not (Test-Path -LiteralPath (Join-Path $requirementPath 'requirements.yaml'))) {
    throw "Missing requirements.yaml in $requirementPath"
}
$images = @(Get-ChildItem -LiteralPath (Join-Path $requirementPath 'reference') -File -ErrorAction SilentlyContinue)
$requirementHash = (Get-FileHash -LiteralPath (Join-Path $requirementPath 'requirements.yaml') -Algorithm SHA256).Hash.ToLowerInvariant()

$envPath = Join-Path $repo '.env'
# The compiler reads credentials from the process environment only: 'uv run main.py' does not
# load .env, so OPENAI_API_KEY would be empty. The file is parsed into this process instead of
# relying on 'uv --env-file', which also avoids PowerShell 5.1 rewriting backslashes in the
# arguments passed to the native uv.exe.
function Import-DotEnv([string]$Path) {
    $loaded = 0
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { continue }
        $name = $Matches[1]
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if (-not $value) { continue }
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        $loaded++
    }
    return $loaded
}

function Convert-ToForwardSlash([string]$Path) { return ($Path -replace '\\', '/') }

$arguments = @('run', 'main.py', (Convert-ToForwardSlash $requirementPath), '--output-dir', (Convert-ToForwardSlash $OutputDir), '--port', "$Port")
if ($ModelLog) { $arguments += '--model-log' }
if (-not $LogFile) {
    $reportDir = Join-Path $root 'reports'
    if (-not (Test-Path -LiteralPath $reportDir)) { New-Item -ItemType Directory -Path $reportDir | Out-Null }
    $LogFile = Join-Path $reportDir ("compile-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
}

Write-Host 'ARC-Bench BookStack · Ver3 compilation'
Write-Host "  requirements : $requirementPath  (SHA-256 $requirementHash, $($images.Count) reference images)"
Write-Host "  output       : $OutputDir"
Write-Host "  backend port : $Port"
Write-Host "  log          : $LogFile"
Write-Host "  command      : uv $($arguments -join ' ')"
Write-Host ''

$problems = @()
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { $problems += 'uv is not on PATH (see the compiler README).' }
if (-not (Test-Path -LiteralPath $envPath)) { $problems += ".env is missing: copy .env.example to .env and set OPENAI_API_KEY / OPENAI_BASE_URL / MODEL." }
elseif (-not (Select-String -LiteralPath $envPath -Pattern '^\s*OPENAI_API_KEY\s*=\s*\S' -Quiet)) { $problems += '.env has no OPENAI_API_KEY value; the compiler would fail on its first model call.' }
if (-not (Test-Path -LiteralPath (Join-Path $repo 'main.py'))) { $problems += "main.py not found in $repo." }
if ($problems.Count -gt 0) {
    Write-Host 'Blocking prerequisites:'
    $problems | ForEach-Object { Write-Host "  - $_" }
} else {
    Write-Host 'Prerequisites present: uv, .env and main.py.'
}

Write-Host ''
if (-not $ConfirmRun) {
    Write-Host 'Dry run only. No model call was made and no file was generated.'
    Write-Host 'Re-run this script with -ConfirmRun to start the expensive full compilation.'
    Write-Host "Afterwards start the app and run the suite:"
    Write-Host "  powershell -File ./scripts/run-bookstack-tests.ps1 -AppDir '$OutputDir' -AppUrl http://127.0.0.1:$Port"
    exit 0
}

if ($problems.Count -gt 0) { throw 'Fix the blocking prerequisites before compiling.' }
Write-Host "Starting the full compilation (13 LLM passes) at $((Get-Date).ToString('s')) ..."
$envCount = Import-DotEnv $envPath
Write-Host "Loaded $envCount environment variables from .env (MODEL=$env:MODEL)."
Write-Host ""

# The transcript is captured by redirecting the child process streams into files. Piping uv.exe
# through Tee-Object would turn every stderr line into a terminating error under
# $ErrorActionPreference = 'Stop' and truncate the very traceback we need to diagnose a failure.
$stdoutLog = [System.IO.Path]::ChangeExtension($LogFile, '.out.log')
$stderrLog = [System.IO.Path]::ChangeExtension($LogFile, '.err.log')
$argumentLine = ($arguments | ForEach-Object { if ($_ -match '\s') { '"{0}"' -f $_ } else { $_ } }) -join ' '
$child = Start-Process -FilePath 'uv.exe' -ArgumentList $argumentLine -WorkingDirectory $repo -PassThru `
    -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
$startedAt = Get-Date
while (-not $child.HasExited) {
    Start-Sleep -Seconds 15
    $child.Refresh()
    $minutes = [math]::Round(((Get-Date) - $startedAt).TotalMinutes, 1)
    $lastLine = (Get-Content -LiteralPath $stdoutLog -Tail 1 -ErrorAction SilentlyContinue)
    if ($lastLine) { Write-Host ("[{0,6:N1} min] {1}" -f $minutes, ($lastLine.Trim() -replace '\s+', ' ' | ForEach-Object { $_.Substring(0, [Math]::Min(110, $_.Length)) })) }
}
$code = $child.ExitCode
$meaning = switch ($code) {
    0 { 'GREEN / all required checks passed' }
    1 { 'FAILED / fatal error' }
    3 { 'COMPLETED_WITH_FAILURES / deployable application kept (strict policy)' }
    130 { 'INTERRUPTED' }
    default { 'unknown exit code' }
}
Write-Host ''
Write-Host "Compiler exit code: $code ($meaning)"
Write-Host "Transcript: $stdoutLog"
Write-Host "Errors:     $stderrLog"
Write-Host "Report:     $(Join-Path $OutputDir '.arc\final_report.md')"
exit $code
