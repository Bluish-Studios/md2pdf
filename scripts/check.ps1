<#
.SYNOPSIS
    Runs md2pdf's gates. The git hooks and CI call this same script, so a local pass means a CI pass.

.EXAMPLE
    ./scripts/check.ps1 -Mode setup     # once: create .venv, install dev tools, turn on the git hooks
    ./scripts/check.ps1 -Mode fast      # pre-commit (seconds): lint, privacy, TODO links, unit tests
    ./scripts/check.ps1                 # full (pre-push, about 2 minutes): everything, with Edge and the 90% coverage floor
#>
[CmdletBinding()]
param(
    [ValidateSet('full', 'fast', 'lint', 'test', 'setup')]
    [string]$Mode = 'full',
    # Use the Python on PATH instead of .venv (CI installs requirements-dev.txt into the runner's Python).
    [switch]$NoVenv
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$IsCI = [bool]$env:CI
$Results = [System.Collections.Generic.List[object]]::new()

function Get-Python([bool]$System) {
    if ($System) { return (Get-Command python -CommandType Application | Select-Object -First 1).Source }
    $venv = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path $venv)) { $venv = Join-Path $Root '.venv/bin/python' }
    if (-not (Test-Path $venv)) { throw 'No .venv yet. Run: ./scripts/check.ps1 -Mode setup' }
    return $venv
}

function Invoke-Step([string]$Name, [scriptblock]$Body) {
    Write-Host "`n=== $Name" -ForegroundColor Cyan
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $ok = $true
    try {
        & $Body
        if ($LASTEXITCODE) { $ok = $false }
    } catch {
        Write-Host $_.Exception.Message -ForegroundColor Red
        $ok = $false
    }
    $Results.Add([pscustomobject]@{ Gate = $Name; Result = $(if ($ok) { 'PASS' } else { 'FAIL' }); Seconds = [math]::Round($watch.Elapsed.TotalSeconds, 1) })
    $global:LASTEXITCODE = 0
}

function Invoke-Setup {
    $base = (Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    if (-not $base) { throw 'Python 3.10+ is needed on PATH to create .venv' }
    if (-not (Test-Path (Join-Path $Root '.venv'))) { & $base -m venv (Join-Path $Root '.venv') }
    $py = Get-Python $false
    & $py -m pip install --disable-pip-version-check --quiet --upgrade -r requirements-dev.txt
    if ($LASTEXITCODE) { throw 'pip install -r requirements-dev.txt failed' }
    git config core.hooksPath .githooks
    Write-Host 'Dev tools installed in .venv, and git hooks turned on (.githooks). Next: ./scripts/check.ps1' -ForegroundColor Green
}

function Get-ScriptAnalyzer {
    if (Get-Module -ListAvailable PSScriptAnalyzer) { return $true }
    $tools = Join-Path $Root '.tools'
    if (Test-Path (Join-Path $tools 'PSScriptAnalyzer')) { $env:PSModulePath = "$tools$([IO.Path]::PathSeparator)$env:PSModulePath"; return $true }
    if (Get-Command Save-PSResource -ErrorAction SilentlyContinue) {
        New-Item -ItemType Directory -Force $tools | Out-Null
        Save-PSResource -Name PSScriptAnalyzer -Path $tools -TrustRepository -Quiet
        $env:PSModulePath = "$tools$([IO.Path]::PathSeparator)$env:PSModulePath"
        return $true
    }
    return $false
}

function Invoke-Lint([string]$Py, [bool]$Staged) {
    Invoke-Step 'ruff (lint + security rules)' { & $Py -m ruff check . }
    Invoke-Step 'actionlint (workflow YAML)' {
        $scripts = & $Py -c "import sysconfig; print(sysconfig.get_path('scripts'))"
        $exe = Get-ChildItem -Path $scripts -Filter 'actionlint*' -File | Select-Object -First 1
        if (-not $exe) { throw 'actionlint is not installed (pip install -r requirements-dev.txt)' }
        & $exe.FullName -color
    }
    Invoke-Step 'PowerShell syntax' {
        $bad = 0
        foreach ($file in Get-ChildItem -Recurse -Include *.ps1, *.psd1 -File | Where-Object FullName -NotMatch '\\\.(venv|tools|runtime)\\|/\.(venv|tools|runtime)/') {
            $tokens = $errors = $null
            [void][Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors)
            foreach ($e in $errors) { Write-Host "$($file.Name):$($e.Extent.StartLineNumber): $($e.Message)"; $bad++ }
        }
        if ($bad) { throw "$bad PowerShell syntax error(s)" }
    }
    if (-not $Staged) {
        Invoke-Step 'PSScriptAnalyzer' {
            if (-not (Get-ScriptAnalyzer)) {
                if ($IsCI) { throw 'PSScriptAnalyzer is not available' }
                Write-Host 'PSScriptAnalyzer is not available here (needs PowerShell 7.4+ to fetch it); CI runs it.' -ForegroundColor Yellow
                return
            }
            $found = @(foreach ($f in 'md2pdf.ps1', 'scripts/check.ps1') { Invoke-ScriptAnalyzer -Path $f -Settings ./PSScriptAnalyzerSettings.psd1 })
            $found | Format-Table ScriptName, Line, RuleName, Message -AutoSize -Wrap | Out-String -Width 200 | Write-Host
            if ($found.Count) { throw "$($found.Count) PSScriptAnalyzer finding(s)" }
        }
    }
    $scope = if ($Staged) { '--staged' } else { '--history' }
    Invoke-Step 'privacy (names and emails)' { & $Py scripts/gates.py privacy $scope }
    Invoke-Step 'TODO links' { & $Py scripts/gates.py todos }
}

function Invoke-TestSuite([string]$Py, [bool]$UnitOnly) {
    if ($UnitOnly) {
        Invoke-Step 'unit tests' { & $Py -m pytest -q -x -m 'not e2e' -p no:cacheprovider }
        return
    }
    Invoke-Step 'all tests + coverage floor (unit, e2e with Edge, launchers)' {
        & $Py -m pytest -q --cov --cov-report=term-missing --cov-report=xml:test-artifacts/coverage.xml `
            --cov-report=html:test-artifacts/coverage-html --junitxml=test-artifacts/junit.xml
    }
}

if ($Mode -eq 'setup') { Invoke-Setup; exit 0 }

$py = Get-Python $NoVenv.IsPresent
switch ($Mode) {
    'fast' { Invoke-Lint $py $true; Invoke-TestSuite $py $true }
    'lint' { Invoke-Lint $py $false }
    'test' { Invoke-TestSuite $py $false }
    'full' { Invoke-Lint $py $false; Invoke-TestSuite $py $false }
}

Write-Host ''
$Results | Format-Table -AutoSize | Out-String | Write-Host
$failed = @($Results | Where-Object Result -EQ 'FAIL')
if ($env:GITHUB_STEP_SUMMARY) {
    $rows = $Results | ForEach-Object { "| $($_.Gate) | $(if ($_.Result -eq 'PASS') { 'PASS' } else { '**FAIL**' }) | $($_.Seconds) s |" }
    @("### md2pdf gates ($Mode)", '', '| Gate | Result | Time |', '|---|---|---|') + $rows | Add-Content $env:GITHUB_STEP_SUMMARY
}
if ($failed.Count) {
    Write-Host "FAILED: $($failed.Gate -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "All $($Results.Count) gates passed ($Mode)." -ForegroundColor Green
exit 0
