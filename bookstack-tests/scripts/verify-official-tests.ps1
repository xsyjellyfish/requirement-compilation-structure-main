<#
.SYNOPSIS
    Verifies that the BookStack requirements, tests and setup scripts are still the
    unmodified ARC-Bench originals (rule 9: official assertions must not be edited).

.DESCRIPTION
    Compares every file recorded in ../official-manifest.json with the file on disk.
    Exits 1 when a file was modified, deleted or added inside tests/ or requirements/,
    so a passing (or failing) run can never be attributed to a doctored assertion.
#>
param(
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $root 'official-manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath)) {
    Write-Error "Missing manifest: $manifestPath. Run ./scripts/write-official-manifest.ps1 first."
    exit 1
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json

$drift = @()
foreach ($entry in $manifest.files) {
    $path = Join-Path $root ($entry.path -replace '/', '\')
    if (-not (Test-Path -LiteralPath $path)) {
        $drift += [pscustomobject]@{ Path = $entry.path; Problem = 'MISSING'; Detail = 'file not found' }
        continue
    }
    $sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($sha256 -ne $entry.sha256) {
        $drift += [pscustomobject]@{ Path = $entry.path; Problem = 'MODIFIED'; Detail = "expected $($entry.sha256), found $sha256" }
    }
}

$known = @{}
foreach ($entry in $manifest.files) { $known[$entry.path] = $true }
foreach ($folder in 'tests', 'requirements') {
    $base = Join-Path $root $folder
    if (-not (Test-Path -LiteralPath $base)) { continue }
    foreach ($file in Get-ChildItem -Recurse -File -LiteralPath $base) {
        $relative = $file.FullName.Substring($root.Length + 1).Replace('\', '/')
        if (-not $known.ContainsKey($relative)) {
            $drift += [pscustomobject]@{ Path = $relative; Problem = 'EXTRA'; Detail = 'not part of the official snapshot' }
        }
    }
}

Write-Host "ARC-Bench BookStack snapshot: commit $($manifest.source.commit) ($($manifest.source.commitDate))"
Write-Host "Checked $($manifest.files.Count) official files under tests/, requirements/ and the setup scripts."
if ($drift.Count -eq 0) {
    Write-Host 'OK: every official file matches the manifest byte for byte.'
    exit 0
}
Write-Host "DRIFT: $($drift.Count) file(s) differ from the official snapshot:"
$drift | Format-Table -AutoSize | Out-String | Write-Host
exit 1
