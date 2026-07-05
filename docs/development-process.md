# Development Process

Screenarr changes should be reviewed locally before they are committed and
published to GitHub.

## Required pre-publish gate

Run:

```powershell
.\scripts\review-gate.ps1 -CodexReviewConfirmed
```

The gate runs:

- repository sanity checks
- secret scan
- Ruff
- pytest
- Docker image build
- CodeRabbit CLI review in agent mode

Codex local review is interactive in the Codex app, so run `/review` against
the uncommitted changes first. Pass `-CodexReviewConfirmed` only after Codex
P0/P1 issues are resolved or explicitly documented as accepted.

## CodeRabbit

For native or WSL CodeRabbit, verify that the local CLI is already
authenticated before running the review gate:

```powershell
coderabbit auth status --agent
```

On Windows hosts where the CodeRabbit installer does not support the native shell,
the review gate can run CodeRabbit in Docker instead:

```powershell
# Requires Docker Desktop or another working Docker daemon.
# Load CODERABBIT_API_KEY into this PowerShell session first.
.\scripts\review-gate.ps1 -CodexReviewConfirmed -CodeRabbitRunner docker
```

One safe local pattern is to write an ignored env file from a masked prompt,
import it into the current process, run the gate, then delete the file and clear
the session variable:

```powershell
New-Item -ItemType Directory -Force -Path .review-gate | Out-Null
$secureKey = Read-Host "CodeRabbit API key" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    Set-Content -LiteralPath .review-gate/coderabbit.env -Value "CODERABBIT_API_KEY=$plainKey"
    Get-Content .review-gate/coderabbit.env | ForEach-Object {
        $name, $value = $_ -split "=", 2
        if ($name -eq "CODERABBIT_API_KEY") { $env:CODERABBIT_API_KEY = $value }
    }
    .\scripts\review-gate.ps1 -CodexReviewConfirmed -CodeRabbitRunner docker
} finally {
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    Remove-Item Env:\CODERABBIT_API_KEY -ErrorAction SilentlyContinue
    Remove-Variable plainKey -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath .review-gate/coderabbit.env -Force -ErrorAction SilentlyContinue
}
```

For the Docker path, `CODERABBIT_API_KEY` in the local process environment is
the only CodeRabbit credential needed. The native `coderabbit auth status
--agent` prerequisite does not apply to the Docker runner, and the key must not
be typed into shell history or written to tracked files. A temporary
`.review-gate/coderabbit.env` file is acceptable because `.review-gate/` is
ignored, but it is only a local source file until you import it into the current
PowerShell session. The placeholder values allowed by the fallback secret scan
are kept in `scripts/allowed-secret-placeholders.env`.

The native/WSL review gate uses:

```powershell
coderabbit review --agent -t uncommitted -c AGENTS.md
```

The Docker fallback uses the same review scope with CodeRabbit's API-key mode.
Review output is still parsed as NDJSON, and `critical` or `major` issues fail
the gate.

Resolve all `critical` and `major` CodeRabbit issues before committing. If a
finding is intentionally accepted, document the reason in the commit or PR notes.

## GitHub review

After pushing a PR, request Codex review with:

```text
@codex review
```

Automatic Codex PR review can also be enabled in the GitHub integration. PRs
should not merge until Codex P0/P1 and CodeRabbit critical/major findings are
resolved or explicitly accepted.
