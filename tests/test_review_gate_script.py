from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def shell_command() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


POWERSHELL_AVAILABLE = pytest.mark.skipif(
    shell_command() is None,
    reason="PowerShell is not available",
)
WINDOWS_REVIEW_GATE_TEST = pytest.mark.skipif(
    sys.platform != "win32" or shell_command() is None,
    reason="fake .cmd shims require Windows and PowerShell",
)


def run_review_gate_command(
    *,
    shell: str | None = None,
    extra_args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: Path = REPO_ROOT,
) -> subprocess.CompletedProcess[str]:
    resolved_shell = shell or shell_command()
    assert resolved_shell is not None
    return subprocess.run(
        [
            resolved_shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPO_ROOT / "scripts/review-gate.ps1"),
            *(extra_args or []),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=cwd,
        env=env,
    )


def run_review_gate(
    fixture_name: str,
    *,
    cwd: Path = REPO_ROOT,
) -> subprocess.CompletedProcess[str]:
    return run_review_gate_command(
        extra_args=[
            "-CodexReviewConfirmed",
            "-SkipLocalGuards",
            "-SkipLocalChecks",
            "-SkipDockerBuild",
            "-CodeRabbitFixturePath",
            str(REPO_ROOT / "tests/fixtures" / fixture_name),
        ],
        cwd=cwd,
    )


def write_fake_git(fake_bin: Path, repo_path: Path) -> None:
    fake_bin.joinpath("git.cmd").write_text(
        "\r\n".join(
            [
                "@echo off",
                'if "%1"=="rev-parse" if "%2"=="--show-toplevel" echo '
                + str(repo_path),
                'if "%1"=="rev-parse" if "%2"=="--show-toplevel" exit /b 0',
                'if "%1"=="rev-parse" if "%2"=="--is-inside-work-tree" echo true',
                'if "%1"=="rev-parse" if "%2"=="--is-inside-work-tree" exit /b 0',
                'if "%1"=="ls-files" echo allowed.env',
                'if "%1"=="ls-files" exit /b 0',
                'if "%1"=="diff" exit /b 0',
                'if "%1"=="show" exit /b 1',
                "exit /b 1",
            ]
        ),
        encoding="utf-8",
    )


def powershell_env_with_isolated_path(fake_bin: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = str(fake_bin)
    env.pop("CODERABBIT_API_KEY", None)
    return env


@pytest.fixture
def fake_bin_with_git(tmp_path: Path) -> tuple[str, Path]:
    shell = shell_command()
    assert shell is not None
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_git(fake_bin, REPO_ROOT)
    return shell, fake_bin


@POWERSHELL_AVAILABLE
@pytest.mark.parametrize(
    ("fixture_name", "expect_success", "expected_message"),
    [
        ("coderabbit-clean.ndjson", True, "Review gate passed."),
        ("coderabbit-major.ndjson", False, "critical/major"),
        ("coderabbit-error.ndjson", False, "error event"),
        ("coderabbit-unparseable.ndjson", False, "malformed JSON event"),
        ("coderabbit-mixed-malformed.ndjson", False, "malformed JSON event"),
        ("coderabbit-review-skipped.ndjson", False, "review was skipped"),
        ("coderabbit-unknown-severity.ndjson", False, "unknown severity"),
    ],
)
def test_review_gate_coderabbit_fixtures(
    fixture_name: str,
    expect_success: bool,
    expected_message: str,
) -> None:
    result = run_review_gate(fixture_name)
    output = result.stdout + result.stderr
    if expect_success:
        assert result.returncode == 0, output
        assert expected_message in output
    else:
        assert result.returncode != 0
        assert expected_message in output


@POWERSHELL_AVAILABLE
def test_review_gate_resolves_repo_root_from_script_path(tmp_path: Path) -> None:
    result = run_review_gate("coderabbit-clean.ndjson", cwd=tmp_path)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "Review gate passed." in output


def write_fake_central_runner(
    path: Path,
    *,
    exit_code: int = 0,
) -> Path:
    path.write_text(
        "\n".join(
            [
                "param(",
                "    [string]$Repository,",
                "    [switch]$Uncommitted,",
                "    [string]$Config",
                ")",
                '$countPath = "$env:SCREENARR_CENTRAL_RUNNER_LOG.calls"',
                "$count = 0",
                "if (Test-Path -LiteralPath $countPath) { "
                "$count = [int](Get-Content -LiteralPath $countPath) }",
                "Set-Content -LiteralPath $countPath -Value ($count + 1) -Encoding UTF8",
                "$record = [ordered]@{",
                "    Repository = $Repository",
                "    Uncommitted = $Uncommitted.IsPresent",
                "    Config = $Config",
                "}",
                "$record | ConvertTo-Json -Compress | "
                "Set-Content -LiteralPath $env:SCREENARR_CENTRAL_RUNNER_LOG -Encoding UTF8",
                f"exit {exit_code}",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_coderabbit_config_exists_at_repo_root() -> None:
    config_path = REPO_ROOT / ".coderabbit.yaml"
    assert config_path.is_file(), (
        "scripts/review-gate.ps1 passes this file as -Config to the central "
        "CodeRabbit runner; a fresh clone without it fails closed"
    )


@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_delegates_once_to_central_runner(tmp_path: Path) -> None:
    runner = write_fake_central_runner(tmp_path / "Invoke-CodeRabbit.ps1")
    invocation_log = tmp_path / "central-runner.json"
    env = os.environ.copy()
    env["SCREENARR_CENTRAL_RUNNER_LOG"] = str(invocation_log)

    result = run_review_gate_command(
        extra_args=[
            "-CodexReviewConfirmed",
            "-SkipLocalGuards",
            "-SkipLocalChecks",
            "-SkipDockerBuild",
            "-CentralCodeRabbitRunner",
            str(runner),
        ],
        env=env,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert Path(f"{invocation_log}.calls").read_text(encoding="utf-8-sig").strip() == "1"
    record = json.loads(invocation_log.read_text(encoding="utf-8-sig"))
    assert Path(record["Repository"]) == REPO_ROOT
    assert record["Uncommitted"] is True
    assert Path(record["Config"]) == REPO_ROOT / ".coderabbit.yaml"
    assert "quota-aware CodeRabbit review" in output
    assert "Review gate passed." in output


@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_propagates_central_runner_failure(tmp_path: Path) -> None:
    runner = write_fake_central_runner(
        tmp_path / "Invoke-CodeRabbit.ps1",
        exit_code=3,
    )
    env = os.environ.copy()
    env["SCREENARR_CENTRAL_RUNNER_LOG"] = str(tmp_path / "central-runner.json")

    result = run_review_gate_command(
        extra_args=[
            "-CodexReviewConfirmed",
            "-SkipLocalGuards",
            "-SkipLocalChecks",
            "-SkipDockerBuild",
            "-CentralCodeRabbitRunner",
            str(runner),
        ],
        env=env,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 3
    assert "quota-aware CodeRabbit review failed with exit code 3" in output


@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_skip_coderabbit_does_not_require_central_runner(
    tmp_path: Path,
) -> None:
    result = run_review_gate_command(
        extra_args=[
            "-CodexReviewConfirmed",
            "-SkipLocalGuards",
            "-SkipLocalChecks",
            "-SkipDockerBuild",
            "-SkipCodeRabbit",
            "-CentralCodeRabbitRunner",
            str(tmp_path / "missing-runner.ps1"),
        ],
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "quota-aware CodeRabbit review" not in output
    assert "Review gate passed." in output


@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_fails_closed_when_central_runner_is_missing(tmp_path: Path) -> None:
    missing_runner = tmp_path / "missing-runner.ps1"

    result = run_review_gate_command(
        extra_args=[
            "-CodexReviewConfirmed",
            "-SkipLocalGuards",
            "-SkipLocalChecks",
            "-SkipDockerBuild",
            "-CentralCodeRabbitRunner",
            str(missing_runner),
        ],
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "Central CodeRabbit runner is unavailable" in output


@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_fallback_secret_scan_allows_documented_placeholders(
    tmp_path: Path,
) -> None:
    shell = shell_command()
    assert shell is not None
    fake_bin = tmp_path / "bin"
    fake_repo = tmp_path / "repo"
    fake_bin.mkdir()
    fake_repo.mkdir()
    write_fake_git(fake_bin, fake_repo)
    fake_repo.joinpath("allowed.env").write_text(
        (REPO_ROOT / "scripts/allowed-secret-placeholders.env").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )

    result = run_review_gate_command(
        shell=shell,
        extra_args=[
            "-CodexReviewConfirmed",
            "-SkipLocalChecks",
            "-SkipDockerBuild",
            "-SkipCodeRabbit",
        ],
        env=powershell_env_with_isolated_path(fake_bin),
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "fallback pattern scan" in output
    assert "Review gate passed." in output


@pytest.mark.parametrize(
    "env_content_template",
    [
        "CODERABBIT_API_KEY={key}\n",
        '$env:CODERABBIT_API_KEY = "{key}"\n',
    ],
)
@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_fallback_secret_scan_blocks_real_secrets(
    tmp_path: Path,
    env_content_template: str,
) -> None:
    shell = shell_command()
    assert shell is not None
    fake_bin = tmp_path / "bin"
    fake_repo = tmp_path / "repo"
    fake_bin.mkdir()
    fake_repo.mkdir()
    write_fake_git(fake_bin, fake_repo)
    fake_coderabbit_key = "cr-" + "0123456789abcdef0123456789abcdef"
    fake_repo.joinpath("allowed.env").write_text(
        env_content_template.format(key=fake_coderabbit_key),
        encoding="utf-8",
    )

    result = run_review_gate_command(
        shell=shell,
        extra_args=[
            "-CodexReviewConfirmed",
            "-SkipLocalChecks",
            "-SkipDockerBuild",
            "-SkipCodeRabbit",
        ],
        env=powershell_env_with_isolated_path(fake_bin),
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "allowed.env: possible secret match" in output
    assert "secret scan failed" in output
    assert fake_coderabbit_key not in output
