<#
.SYNOPSIS
    Records the official ARC-Bench BookStack snapshot in ../official-manifest.json.

.DESCRIPTION
    The manifest stores, for every official file (requirements/, tests/ and the three
    upstream setup scripts), its byte size, SHA-256 and git blob SHA. SHA-256 detects local
    drift; the git blob SHA can be compared with the GitHub contents API at the pinned
    commit, so the provenance of the snapshot stays verifiable without a git clone.

    Run this only when the snapshot is refreshed on purpose:
        pwsh -File ./scripts/write-official-manifest.ps1 -UpstreamCommit <sha> -UpstreamCommitDate <iso>
#>
param(
    [string]$UpstreamCommit = 'e59696bbc68578c3b3da1ad47f9a0ac07e581184',
    [string]$UpstreamCommitDate = '2026-11-03T10:45:50Z'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Get-GitBlobSha([string]$Path) {
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $header = [System.Text.Encoding]::ASCII.GetBytes("blob $($bytes.Length)`0")
    $buffer = New-Object byte[] ($header.Length + $bytes.Length)
    [Array]::Copy($header, 0, $buffer, 0, $header.Length)
    [Array]::Copy($bytes, 0, $buffer, $header.Length, $bytes.Length)
    $hash = [System.Security.Cryptography.SHA1]::Create().ComputeHash($buffer)
    return (($hash | ForEach-Object { $_.ToString('x2') }) -join '')
}

$targets = @()
$targets += Get-ChildItem -Recurse -File (Join-Path $root 'tests')
$targets += Get-ChildItem -Recurse -File (Join-Path $root 'requirements')
foreach ($name in 'setup-playwright.ps1', 'setup-playwright.cjs', 'setup-playwright.sh') {
    $targets += Get-Item (Join-Path $root $name)
}

$files = foreach ($file in ($targets | Sort-Object FullName)) {
    [ordered]@{
        path       = $file.FullName.Substring($root.Length + 1).Replace('\', '/')
        bytes      = $file.Length
        sha256     = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        gitBlobSha = Get-GitBlobSha $file.FullName
    }
}

$manifest = [ordered]@{
    source = [ordered]@{
        repository   = 'https://github.com/code-philia/arc-bench'
        app          = 'bookstack'
        commit       = $UpstreamCommit
        commitDate   = $UpstreamCommitDate
        archive      = "https://codeload.github.com/code-philia/arc-bench/tar.gz/$UpstreamCommit"
        paths        = @('arc-bench/bookstack/requirements', 'arc-bench/bookstack/tests',
                         'setup-playwright.ps1', 'setup-playwright.cjs', 'setup-playwright.sh')
        recordedAt   = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    fileCount = @($files).Count
    files     = @($files)
}

$manifestPath = Join-Path $root 'official-manifest.json'
$json = $manifest | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText($manifestPath, $json + "`n", (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Wrote $manifestPath ($(@($files).Count) files, commit $UpstreamCommit)"
