param(
    [switch]$SkipDockerBuild,
    [switch]$SkipCodeRabbit,
    [switch]$SkipLocalChecks,
    [switch]$SkipLocalGuards,
    [switch]$CodexReviewConfirmed,
    [string]$CodeRabbitFixturePath = "",
    [switch]$PrintCodeRabbitRunner,
    [ValidateSet("", "native", "wsl", "docker")]
    [string]$CodeRabbitRunner = ""
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

function Convert-ToWslPath {
    param([string]$Path)
    $convertedPath = wsl -e wslpath -a $Path
    if (-not $convertedPath) {
        throw "could not convert path for WSL: $Path"
    }
    return $convertedPath.Trim()
}

function ConvertTo-BashQuoted {
    param([string]$Value)
    return "'" + $Value.Replace("'", "'\''") + "'"
}

function Get-WslCodeRabbitCommand {
    param([string]$Command)
    $wslRepoPath = Convert-ToWslPath $RepoPath
    return "cd $(ConvertTo-BashQuoted $wslRepoPath) && export PATH=`"`$HOME/.local/bin:`$PATH`" && $Command"
}

function Get-CodeRabbitDockerImage {
    return "screenarr-coderabbit-cli:local"
}

function Protect-LocalSecret {
    param([string]$Value)
    $redacted = $Value
    foreach ($secretName in $ReviewGateSecretEnvNames) {
        $secret = [Environment]::GetEnvironmentVariable($secretName)
        if (-not [string]::IsNullOrWhiteSpace($secret)) {
            $redacted = $redacted.Replace($secret, "[REDACTED_SECRET]")
        }
    }
    return $redacted
}

function Build-CodeRabbitDockerImage {
    $dockerfilePath = Join-Path $RepoPath "scripts/coderabbit.Dockerfile"
    docker build -f $dockerfilePath -t (Get-CodeRabbitDockerImage) (Split-Path $dockerfilePath -Parent)
    if ($LASTEXITCODE -ne 0) {
        throw "CodeRabbit Docker image build failed with exit code $LASTEXITCODE"
    }
}

function Complete-ProcessReadTask {
    param([object]$Task)
    if ($null -eq $Task) {
        return
    }
    try {
        $null = $Task.GetAwaiter().GetResult()
    } catch {
        # Best-effort cleanup for daemon probing; the caller decides availability.
        Write-Verbose "Ignoring daemon probe stream cleanup failure."
    }
}

function Test-DockerDaemonAvailable {
    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerCommand) {
        return $false
    }
    $timeoutMilliseconds = 10000
    $configuredTimeout = 0
    if (
        [int]::TryParse($env:SCREENARR_TEST_DOCKER_INFO_TIMEOUT_MS, [ref]$configuredTimeout) -and
        $configuredTimeout -gt 0
    ) {
        $timeoutMilliseconds = $configuredTimeout
    }

    $process = [System.Diagnostics.Process]::new()
    try {
        $process.StartInfo.FileName = $dockerCommand.Source
        $process.StartInfo.Arguments = "info"
        $process.StartInfo.UseShellExecute = $false
        $process.StartInfo.RedirectStandardOutput = $true
        $process.StartInfo.RedirectStandardError = $true
        if (-not $process.Start()) {
            return $false
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($timeoutMilliseconds)) {
            $process.Kill()
            $null = $process.WaitForExit(1000)
            Complete-ProcessReadTask $stdoutTask
            Complete-ProcessReadTask $stderrTask
            return $false
        }
        Complete-ProcessReadTask $stdoutTask
        Complete-ProcessReadTask $stderrTask
        return $process.ExitCode -eq 0
    } catch {
        return $false
    } finally {
        $process.Dispose()
    }
}

function Invoke-CodeRabbitDocker {
    param([string]$Command)
    if ([string]::IsNullOrWhiteSpace($env:CODERABBIT_API_KEY)) {
        throw "CODERABBIT_API_KEY must be set locally to run CodeRabbit through Docker."
    }
    $workspaceMount = "${RepoPath}:/workspace:ro"
    $dockerArgs = @(
        "run",
        "--rm",
        "-e",
        "CODERABBIT_API_KEY",
        "--env",
        "GIT_OPTIONAL_LOCKS=0",
        "--env",
        "HOME=/tmp/coderabbit-home",
        "-v",
        $workspaceMount,
        "-w",
        "/workspace",
        (Get-CodeRabbitDockerImage),
        "bash",
        "-lc",
        "mkdir -p `"`$HOME`" && export PATH=/opt/coderabbit/bin:`$PATH && coderabbit auth login --api-key `"`$CODERABBIT_API_KEY`" >/dev/null && git config --global --add safe.directory /workspace && $Command"
    )
    docker @dockerArgs
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

function Test-CodeRabbitRunnerAvailable {
    param([string]$Runner)
    if ($Runner -eq "native") {
        return [bool](Get-Command coderabbit -ErrorAction SilentlyContinue)
    }
    if ($Runner -eq "docker") {
        if ([string]::IsNullOrWhiteSpace($env:CODERABBIT_API_KEY)) {
            return $false
        }
        return Test-DockerDaemonAvailable
    }
    if ($Runner -eq "wsl") {
        if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
            return $false
        }
        try {
            wsl -e bash -lc (Get-WslCodeRabbitCommand "command -v coderabbit >/dev/null")
        } catch {
            $global:LASTEXITCODE = 1
        }
        return $LASTEXITCODE -eq 0
    }
    return $false
}

function Get-CodeRabbitRunner {
    param(
        [string]$RequestedRunner = "",
        [bool]$WarnOnUnavailable = $true
    )
    if ($RequestedRunner) {
        if (Test-CodeRabbitRunnerAvailable $RequestedRunner) {
            return $RequestedRunner
        }
        if ($WarnOnUnavailable) {
            Write-Warning "Requested CodeRabbit runner '$RequestedRunner' is unavailable."
        }
        return "missing"
    }
    foreach ($runner in @("native", "wsl", "docker")) {
        if (Test-CodeRabbitRunnerAvailable $runner) {
            return $runner
        }
    }
    return "missing"
}

if ($PrintCodeRabbitRunner) {
    Write-Output (Get-CodeRabbitRunner -RequestedRunner $CodeRabbitRunner -WarnOnUnavailable $false)
    exit 0
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

# SkipDockerBuild only skips the Screenarr app image; the Docker CodeRabbit runner
# still builds when CodeRabbit selects or is given -CodeRabbitRunner docker.
if (-not $SkipDockerBuild) {
    Invoke-Step "Docker build" {
        docker build -t screenarr:local .
    }
}

if (-not $SkipCodeRabbit) {
    New-Item -ItemType Directory -Force -Path ".review-gate" | Out-Null
    if ($CodeRabbitFixturePath) {
        $lines = Get-Content -LiteralPath $CodeRabbitFixturePath
        Test-CodeRabbitOutput $lines
    } else {
        $runner = Get-CodeRabbitRunner -RequestedRunner $CodeRabbitRunner
        if ($runner -eq "missing") {
            throw "CodeRabbit CLI was not found natively, in WSL, or through Docker."
        }
        Invoke-Step "CodeRabbit auth" {
            if ($runner -eq "native") {
                coderabbit auth status --agent
            } elseif ($runner -eq "docker") {
                Build-CodeRabbitDockerImage
                $authStderrPath = Join-Path ".review-gate" "coderabbit-auth.stderr"
                Invoke-CodeRabbitDocker "coderabbit auth status --agent" 2> $authStderrPath
                $authExitCode = $LASTEXITCODE
                if (Test-Path -LiteralPath $authStderrPath) {
                    $authStderrLines = Get-Content -LiteralPath $authStderrPath -Encoding UTF8
                    if ($authStderrLines) {
                        $redactedAuthStderrLines = @(
                            $authStderrLines | ForEach-Object { Protect-LocalSecret $_ }
                        )
                        $redactedAuthStderrLines | Set-Content -LiteralPath $authStderrPath -Encoding UTF8
                        $redactedAuthStderrLines | ForEach-Object { Write-Host $_ }
                    }
                }
                if ($authExitCode -ne 0) {
                    throw "CodeRabbit auth failed with exit code $authExitCode"
                }
            } else {
                wsl -e bash -lc (Get-WslCodeRabbitCommand "coderabbit auth status --agent")
            }
        }
        Invoke-Step "CodeRabbit review" {
            $outputPath = Join-Path ".review-gate" "coderabbit.ndjson"
            $stderrPath = Join-Path ".review-gate" "coderabbit.stderr"
            if ($runner -eq "native") {
                $lines = coderabbit review --agent -t uncommitted -c AGENTS.md 2> $stderrPath
            } elseif ($runner -eq "docker") {
                $reviewCommand = "coderabbit review --agent -t uncommitted -c AGENTS.md"
                $lines = Invoke-CodeRabbitDocker $reviewCommand 2> $stderrPath
            } else {
                $wslOutputPath = Convert-ToWslPath (Join-Path $RepoPath $outputPath)
                $wslStderrPath = Convert-ToWslPath (Join-Path $RepoPath $stderrPath)
                $reviewCommand = @(
                    "coderabbit review --agent -t uncommitted -c AGENTS.md",
                    "> $(ConvertTo-BashQuoted $wslOutputPath)",
                    "2> $(ConvertTo-BashQuoted $wslStderrPath)"
                ) -join " "
                wsl -e bash -lc (Get-WslCodeRabbitCommand $reviewCommand)
                if (Test-Path -LiteralPath $outputPath) {
                    $lines = Get-Content -LiteralPath $outputPath -Encoding UTF8
                } else {
                    $lines = @()
                }
            }
            $exitCode = $LASTEXITCODE
            $lines = @($lines | ForEach-Object { Protect-LocalSecret $_ })
            $lines | Set-Content -LiteralPath $outputPath -Encoding UTF8
            if (Test-Path -LiteralPath $stderrPath) {
                $stderrLines = Get-Content -LiteralPath $stderrPath -Encoding UTF8
                if ($stderrLines) {
                    $redactedStderrLines = @($stderrLines | ForEach-Object { Protect-LocalSecret $_ })
                    $redactedStderrLines | Set-Content -LiteralPath $stderrPath -Encoding UTF8
                    $redactedStderrLines | ForEach-Object { Write-Host $_ }
                }
            }
            if ($exitCode -ne 0) {
                throw "CodeRabbit review failed with exit code $exitCode"
            }
            Test-CodeRabbitOutput $lines
        }
    }
}

Write-Host "Review gate passed."
