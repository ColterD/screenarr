param(
    [switch]$SkipDockerBuild,
    [switch]$SkipCodeRabbit,
    [switch]$SkipLocalChecks,
    [switch]$SkipLocalGuards,
    [switch]$CodexReviewConfirmed,
    [string]$CodeRabbitFixturePath = "",
    # Runner path: explicit parameter wins, then the environment variable,
    # then the example default checkout. Other machines must override it.
    [string]$CentralCodeRabbitRunner = $(if ($env:SCREENARR_CENTRAL_CODERABBIT_RUNNER) { $env:SCREENARR_CENTRAL_CODERABBIT_RUNNER } else { "D:\Projects\coderabbit\Invoke-CodeRabbit.ps1" })
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$ScriptRoot = if ($PSScriptRoot) {
    $PSScriptRoot
} else {
    Split-Path -Parent $MyInvocation.MyCommand.Path
}
Set-Location -LiteralPath (Join-Path $ScriptRoot "..")
$RepoPath = git rev-parse --show-toplevel 2>$null
if (-not $RepoPath) {
    throw "could not resolve git repository root"
}
$RepoPath = $RepoPath.Trim()
Set-Location -LiteralPath $RepoPath
$Python = "python"
$WindowsVenvPython = ".venv\Scripts\python.exe"
$PosixVenvPython = ".venv/bin/python"
if (Test-Path -LiteralPath $WindowsVenvPython) {
    $Python = $WindowsVenvPython
} elseif (Test-Path -LiteralPath $PosixVenvPython) {
    $Python = $PosixVenvPython
}
$ReviewGateSecretEnvNames = @(
    "BRIDGE_API_KEY",
    "MEDIAMANAGER_TOKEN",
    "MEDIAMANAGER_PASSWORD",
    "ONSCREEN_WEBHOOK_SECRET",
    "CODERABBIT_API_KEY"
)
$AllowedSecretPlaceholderPath = Join-Path $ScriptRoot "allowed-secret-placeholders.env"
$ReviewGateAllowedSecretPlaceholders = @()
if (-not (Test-Path -LiteralPath $AllowedSecretPlaceholderPath -PathType Leaf)) {
    throw "allowed secret placeholder file is missing: $AllowedSecretPlaceholderPath"
}
foreach ($line in Get-Content -LiteralPath $AllowedSecretPlaceholderPath -Encoding UTF8) {
    $trimmedLine = $line.Trim()
    if ([string]::IsNullOrWhiteSpace($trimmedLine) -or $trimmedLine.StartsWith("#")) {
        continue
    }
    $separatorIndex = $trimmedLine.IndexOf("=")
    if ($separatorIndex -ge 0) {
        $placeholderValue = $trimmedLine.Substring($separatorIndex + 1).Trim().Trim('"').Trim("'")
    } else {
        $placeholderValue = $trimmedLine
    }
    if (-not [string]::IsNullOrWhiteSpace($placeholderValue)) {
        $ReviewGateAllowedSecretPlaceholders += $placeholderValue
    }
}
$ReviewGateAllowedSecretPlaceholderPattern = [string]::Join(
    "|",
    @($ReviewGateAllowedSecretPlaceholders | Sort-Object -Unique | ForEach-Object {
        [regex]::Escape($_)
    })
)

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Command
    )
    Write-Host "==> $Name"
    $global:LASTEXITCODE = 0
    & $Command
    $stepSucceeded = $?
    $stepExitCode = $LASTEXITCODE
    if (-not $stepSucceeded) {
        if ($stepExitCode -ne 0) {
            throw "$Name failed with exit code $stepExitCode"
        }
        throw "$Name failed"
    }
    if ($stepExitCode -ne 0) {
        throw "$Name failed with exit code $stepExitCode"
    }
}

function Get-JsonProperty {
    param(
        [object]$Object,
        [string]$Name
    )
    if ($null -eq $Object) {
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Get-FindingSeverity {
    param([object]$FindingEvent)
    $severity = Get-JsonProperty $FindingEvent "severity"
    if ($severity) {
        return [string]$severity
    }
    $finding = Get-JsonProperty $FindingEvent "finding"
    $severity = Get-JsonProperty $finding "severity"
    if ($severity) {
        return [string]$severity
    }
    return "__unknown__"
}

function Test-CodeRabbitOutput {
    param([string[]]$Lines)

    $criticalOrMajor = 0
    $errors = 0
    $malformed = 0
    $parsedEvents = 0
    $reviewSkipped = 0
    $unknownSeverity = 0
    foreach ($line in $Lines) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        try {
            $reviewEvent = $line | ConvertFrom-Json
        } catch {
            $malformed += 1
            continue
        }
        $parsedEvents += 1
        $type = [string](Get-JsonProperty $reviewEvent "type")
        $eventStatus = [string](Get-JsonProperty $reviewEvent "status")
        if ($eventStatus.ToLowerInvariant() -in @("review_skipped", "skipped")) {
            $reviewSkipped += 1
        }
        if ($type -eq "error") {
            $errors += 1
            continue
        }
        if ($type -ne "finding") {
            continue
        }
        $severity = (Get-FindingSeverity $reviewEvent).ToLowerInvariant()
        if ($severity -in @("critical", "major")) {
            $criticalOrMajor += 1
        } elseif ($severity -notin @("minor", "trivial", "info")) {
            $unknownSeverity += 1
        }
    }

    if ($errors -gt 0) {
        throw "CodeRabbit returned $errors error event(s)."
    }
    if ($malformed -gt 0) {
        throw "CodeRabbit returned $malformed malformed JSON event line(s)."
    }
    if ($parsedEvents -eq 0) {
        throw "CodeRabbit returned no parseable JSON events."
    }
    if ($reviewSkipped -gt 0) {
        throw "CodeRabbit review was skipped."
    }
    if ($unknownSeverity -gt 0) {
        throw "CodeRabbit returned $unknownSeverity finding event(s) with unknown severity."
    }
    if ($criticalOrMajor -gt 0) {
        throw "CodeRabbit returned $criticalOrMajor critical/major issue(s)."
    }
}

if (-not $SkipLocalGuards) {
    Invoke-Step "git repository check" {
        git rev-parse --is-inside-work-tree | Out-Null
    }
}

Write-Host "==> Required interactive Codex review"
Write-Host "Run /review in the Codex app against the uncommitted changes before committing."
Write-Host "Resolve all P0/P1 Codex findings or document an accepted finding."
if (-not $CodexReviewConfirmed) {
    throw "Codex review confirmation missing. Rerun with -CodexReviewConfirmed after /review."
}

if (-not $SkipLocalGuards) {
    Invoke-Step "secret scan" {
        $secretNamePattern = [string]::Join("|", $ReviewGateSecretEnvNames)
        $patterns = @(
            "cr-[a-f0-9]{20,}",
            "sk-proj-[A-Za-z0-9_-]{20,}",
            "sk-[A-Za-z0-9_-]{20,}",
            "ghp_[A-Za-z0-9_]{20,}",
            "(?i)^\s*(\`$env:|Env:)?($secretNamePattern)\s*[:=]\s*['""]?(?!($ReviewGateAllowedSecretPlaceholderPattern)(['""\s]|$))[^'""\s]{16,}"
        )
        $secretMatches = @()
        $scannerExitCode = 0
        if (Get-Command gitleaks -ErrorAction SilentlyContinue) {
            gitleaks detect --source . --no-git --redact
            $scannerExitCode = $LASTEXITCODE
        } elseif (Get-Command trufflehog -ErrorAction SilentlyContinue) {
            trufflehog filesystem --fail --no-update .
            $scannerExitCode = $LASTEXITCODE
        } else {
            Write-Host "gitleaks/trufflehog not found; running fallback pattern scan."
            $files = git ls-files --cached --others --exclude-standard
            foreach ($file in $files) {
                if ($file -match "^(\.git|\.venv|__pycache__|\.review-gate|review-results)/") {
                    continue
                }
                if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
                    continue
                }
                $hit = Select-String -LiteralPath $file -Pattern $patterns -List -ErrorAction SilentlyContinue
                if ($hit) {
                    $secretMatches += "$file`: possible secret match"
                }
            }
        }
        $stagedFiles = git diff --cached --name-only --diff-filter=ACMR
        foreach ($file in $stagedFiles) {
            if ($file -match "^(\.git|\.venv|__pycache__|\.review-gate|review-results)/") {
                continue
            }
            $stagedContent = git show ":$file" 2>$null
            if ($LASTEXITCODE -ne 0) {
                $global:LASTEXITCODE = 0
                continue
            }
            $hit = $stagedContent | Select-String -Pattern $patterns -List -ErrorAction SilentlyContinue
            if ($hit) {
                $secretMatches += "$file`: possible secret match in staged content"
            }
        }
        $global:LASTEXITCODE = 0
        if ($secretMatches.Count -gt 0) {
            $secretMatches | ForEach-Object { Write-Host $_ }
            throw "secret scan failed"
        }
        if ($scannerExitCode -ne 0) {
            throw "secret scanner failed with exit code $scannerExitCode"
        }
    }
}

if (-not $SkipLocalChecks) {
    Invoke-Step "Ruff" {
        & $Python -m ruff check .
    }

    Invoke-Step "pytest" {
        & $Python -m pytest
    }
}

# SkipDockerBuild applies only to the Screenarr application image.
if (-not $SkipDockerBuild) {
    Invoke-Step "Docker build" {
        docker build -t screenarr:local .
    }
}

if (-not $SkipCodeRabbit) {
    if ($CodeRabbitFixturePath) {
        $lines = Get-Content -LiteralPath $CodeRabbitFixturePath
        Test-CodeRabbitOutput $lines
    } else {
        if (-not (Test-Path -LiteralPath $CentralCodeRabbitRunner -PathType Leaf)) {
            throw "Central CodeRabbit runner is unavailable: $CentralCodeRabbitRunner"
        }
        Write-Host "==> quota-aware CodeRabbit review"
        $runnerParameters = @{
            Repository = $RepoPath
            Uncommitted  = $true
            Config       = (Join-Path $RepoPath ".coderabbit.yaml")
        }
        $global:LASTEXITCODE = 0
        & $CentralCodeRabbitRunner @runnerParameters
        $centralExitCode = $LASTEXITCODE
        if ($centralExitCode -ne 0) {
            # Normalized exit codes: 2 = review completed with critical/major
            # findings, 3 = deferred (quota/replay policy), 4 = review failure.
            $originalExitCode = $centralExitCode
            if ($centralExitCode -notin @(2, 3, 4)) {
                $centralExitCode = 4
            }
            [Console]::Error.WriteLine(
                "quota-aware CodeRabbit review failed with exit code $centralExitCode (runner exit code $originalExitCode)"
            )
            exit $centralExitCode
        }
    }
}

Write-Host "Review gate passed."
