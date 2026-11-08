<#
.SYNOPSIS
    Turns a BookStack Playwright JSON result into a triage report that separates
        A. generated-application defects,
        B. compiler-wide generation-rule defects,
        C. test-environment / requirements-version defects.

.DESCRIPTION
    Reads the JSON produced by ./run-bookstack-tests.ps1 (or any 'playwright test --reporter=json'
    run), collects every failed test with its error text and artifacts, and classifies it by
    the failure signature. The classification is evidence-based, never assertion-based: nothing
    in tests/ is touched and no expected value is rewritten. Ambiguous failures are reported as
    'needs manual review' instead of being guessed.

    The report also records the environment facts needed to rule out cause C: the URL under
    test, the pinned ARC-Bench commit, the drift check of the official files and the hash of
    requirements.yaml.

.PARAMETER Results
    Path to a results-*.json file. Defaults to the newest file under ./reports.

.PARAMETER OutFile
    Markdown output path. Defaults to ./reports/failure-analysis-<stamp>.md.

.EXAMPLE
    powershell -File ./scripts/analyze-bookstack-failures.ps1
#>
param(
    [string]$Results = '',
    [string]$OutFile = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$reports = Join-Path $root 'reports'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

if (-not $Results) {
    $newest = Get-ChildItem -LiteralPath $reports -Filter 'results-*.json' -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $newest) { throw "No results-*.json in $reports. Run ./scripts/run-bookstack-tests.ps1 first." }
    $Results = $newest.FullName
}
$Results = (Resolve-Path -LiteralPath $Results).Path
if (-not $OutFile) { $OutFile = Join-Path $reports "failure-analysis-$stamp.md" }

function Get-FailureCategory([string]$Text) {
    if ($Text -match 'ERR_CONNECTION_REFUSED|ECONNREFUSED|ERR_EMPTY_RESPONSE|ERR_CONNECTION_RESET|ERR_NAME_NOT_RESOLVED|ERR_ADDRESS_UNREACHABLE|ERR_ABORTED') {
        return 'C. 测试环境/应用未启动'
    }
    if ($Text -match 'E2E_API_CONTRACT_ERROR|E2E_API_ROUTE_ERROR|Unknown API route|api_contract|API contract violation') {
        return 'B. 编译器通用规则（API 契约/路由）'
    }
    if ($Text -match 'Benchmark Bootstrap Shelf|BookStack User|bookstack_user@example\.com') {
        return 'A. 生成应用（Requirements 要求的初始化数据缺失）'
    }
    if ($Text -match 'waiting for getBy(Role|Label|Text|Placeholder|TestId)|toBeVisible|toBeAttached|toBeEnabled') {
        return 'A. 生成应用（UI 结构/可访问名称缺失）'
    }
    if ($Text -match 'toHaveValue|toHaveText|toHaveURL|toHaveCount|toHaveTitle|toBeHidden|toBeChecked') {
        return 'A. 生成应用（行为/状态断言不符）'
    }
    if ($Text -match 'Test timeout of|Timeout \d+ms exceeded') {
        return '未分类（超时，需人工确认）'
    }
    return '未分类（需人工确认）'
}

function Get-SpecsInSuite($Suite, $FileHint = '') {
    $file = if ($Suite.PSObject.Properties['file'] -and $Suite.file) { $Suite.file } else { $FileHint }
    if ($Suite.PSObject.Properties['specs']) {
        foreach ($spec in $Suite.specs) { [pscustomobject]@{ File = $file; Spec = $spec } }
    }
    if ($Suite.PSObject.Properties['suites']) {
        foreach ($child in $Suite.suites) { Get-SpecsInSuite $child $file }
    }
}

$data = Get-Content -LiteralPath $Results -Raw | ConvertFrom-Json
$specs = foreach ($suite in $data.suites) { Get-SpecsInSuite $suite }

$passCount = 0
$skipCount = 0
$failures = @()
foreach ($entry in $specs) {
    $spec = $entry.Spec
    $statuses = @()
    foreach ($test in $spec.tests) { foreach ($result in $test.results) { $statuses += $result.status } }
    $isFailure = ($spec.ok -eq $false) -or (@($statuses | Where-Object { $_ -in 'failed', 'timedOut', 'interrupted' }).Count -gt 0)
    if (-not $isFailure) {
        if (@($statuses | Where-Object { $_ -eq 'skipped' }).Count -gt 0) { $skipCount++ } else { $passCount++ }
        continue
    }

    $texts = @()
    $attachments = @()
    $duration = 0
    foreach ($test in $spec.tests) {
        foreach ($result in $test.results) {
            if ($result.status -notin 'failed', 'timedOut', 'interrupted') { continue }
            if ($result.PSObject.Properties['error'] -and $result.error -and $result.error.message) { $texts += $result.error.message }
            if ($result.PSObject.Properties['errors']) {
                foreach ($inner in $result.errors) { if ($inner.message) { $texts += $inner.message } }
            }
            if ($result.PSObject.Properties['attachments']) { $attachments += $result.attachments }
            $duration += [int]$result.duration
        }
    }
    $combined = ($texts -join "`n")
    $failures += [pscustomobject]@{
        File        = $entry.File
        Title       = $spec.title
        Category    = Get-FailureCategory $combined
        Message     = $combined
        Attachments = $attachments
        DurationMs  = $duration
    }
}

# ---------------------------------------------------------------- environment facts
$configPath = Join-Path $root 'playwright.config.ts'
$baseUrl = ''
if (Test-Path -LiteralPath $configPath) {
    $match = [regex]::Match((Get-Content -LiteralPath $configPath -Raw), 'baseURL:\s*"([^"]+)"')
    if ($match.Success) { $baseUrl = $match.Groups[1].Value }
}
$answer = 'unreachable'
try {
    if ($baseUrl) {
        $probe = Invoke-WebRequest -Uri $baseUrl -UseBasicParsing -TimeoutSec 5
        $answer = "HTTP $($probe.StatusCode), $($probe.RawContentLength) bytes"
    }
} catch {
    $answer = "unreachable ($($_.Exception.Message.Split([Environment]::NewLine)[0]))"
}

$manifest = Get-Content -LiteralPath (Join-Path $root 'official-manifest.json') -Raw | ConvertFrom-Json
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'verify-official-tests.ps1') *> $null
$verifyExit = $LASTEXITCODE
$requirementsHash = (Get-FileHash -LiteralPath (Join-Path $root 'requirements\requirements.yaml') -Algorithm SHA256).Hash.ToLowerInvariant()
$playwrightVersion = (& npx.cmd playwright --version 2>$null | Select-Object -Last 1)
$byCategory = @($failures | Group-Object Category | Sort-Object Count -Descending)

# ---------------------------------------------------------------- markdown report
$lines = New-Object System.Collections.Generic.List[string]
$lines.Add('# ARC-Bench BookStack 失败分析')
$lines.Add('')
$lines.Add("生成时间 (UTC): $((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))")
$lines.Add("结果文件: ``$Results``")
$lines.Add('')
$lines.Add('## 环境事实（用于排除 C 类原因）')
$lines.Add('')
$lines.Add('| 项 | 值 |')
$lines.Add('| --- | --- |')
$lines.Add("| 被测 URL | ``$baseUrl`` |")
$lines.Add("| URL 探测 | $answer |")
$lines.Add("| 官方快照 commit | ``$($manifest.source.commit)`` ($($manifest.source.commitDate)) |")
$provenance = if ($verifyExit -eq 0) { 'OK：tests/、requirements/ 与 setup 脚本和官方逐字节一致' } else { 'DRIFT：官方文件被修改，见 verify-official-tests.ps1' }
$lines.Add("| 官方文件校验 | $provenance |")
$lines.Add("| requirements.yaml SHA-256 | ``$requirementsHash`` |")
$lines.Add("| Playwright | $playwrightVersion |")
$lines.Add('')
$lines.Add('## 统计')
$lines.Add('')
$lines.Add("通过 $passCount，失败 $($failures.Count)，跳过 $skipCount。")
$lines.Add('')
$lines.Add('## 分类汇总')
$lines.Add('')
if ($byCategory.Count -eq 0) {
    $lines.Add('无失败用例。')
} else {
    $lines.Add('| 类别 | 数量 | 用例 |')
    $lines.Add('| --- | ---: | --- |')
    foreach ($group in $byCategory) {
        $titles = ($group.Group | ForEach-Object { $_.Title }) -join '；'
        $lines.Add("| $($group.Name) | $($group.Count) | $titles |")
    }
}
$lines.Add('')
$lines.Add('## 逐条证据')
$lines.Add('')
$index = 0
foreach ($failure in $failures) {
    $index++
    $lines.Add("### $index. $($failure.Title)")
    $lines.Add('')
    $lines.Add("- 文件：``$($failure.File)``")
    $lines.Add("- 分类：$($failure.Category)")
    $lines.Add("- 耗时：$([math]::Round($failure.DurationMs / 1000, 1)) s")
    $kept = @($failure.Attachments | Where-Object { $_.PSObject.Properties['path'] -and $_.path })
    if ($kept.Count -gt 0) {
        $lines.Add('- 失败产物：')
        foreach ($attachment in $kept) { $lines.Add("  - $($attachment.name)：``$($attachment.path)``") }
    } else {
        $lines.Add('- 失败产物：无（该失败早于页面操作，或产物策略被改动）')
    }
    $lines.Add('')
    $lines.Add('```text')
    ($failure.Message -split "`n" | Select-Object -First 40) | ForEach-Object { $lines.Add($_) }
    $lines.Add('```')
    $lines.Add('')
}
$lines.Add('## 判定规则')
$lines.Add('')
$lines.Add('| 失败特征 | 结论 |')
$lines.Add('| --- | --- |')
$lines.Add('| ERR_CONNECTION_REFUSED / ERR_EMPTY_RESPONSE | C：被测应用未启动或 URL 不对（环境） |')
$lines.Add('| E2E_API_CONTRACT_ERROR / Unknown API route | B：编译器通用生成规则（前端触发器与已接受契约不一致） |')
$lines.Add('| 失败定位器正好是 Benchmark Bootstrap Shelf / BookStack User / bookstack_user@example.com | A：Requirements 声明的初始化数据未生成（生成应用） |')
$lines.Add('| 等待 getByRole/getByLabel/getByText 超时 | A：生成应用缺少该区域、控件或可访问名称 |')
$lines.Add('| toHaveValue/toHaveText/toHaveURL/toHaveCount 不符 | A：生成应用行为或状态错误 |')
$lines.Add('| 只有超时、无其他线索 | 未分类：人工确认 |')
$lines.Add('')
$lines.Add('分类只是证据摘要：判定为“未分类”时必须打开 trace 与 video 复核；任何情况下都不要修改官方断言来提升通过率。')

[System.IO.File]::WriteAllText($OutFile, (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Analyzed: $Results"
Write-Host "Passed $passCount, failed $($failures.Count), skipped $skipCount"
if ($byCategory.Count -gt 0) { $byCategory | Format-Table Name, Count -AutoSize | Out-String | Write-Host }
Write-Host "Report: $OutFile"


