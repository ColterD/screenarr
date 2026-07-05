from __future__ import annotations

import os
import shutil
import subprocess
import sys
import warnings
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


def stop_fake_bin_processes(fake_bin: Path) -> None:
    shell = shell_command()
    if shell is None:
        return
    script_path = fake_bin.parent / "stop-fake-bin.ps1"
    script_path.write_text(
        "\n".join(
            [
                "$needle = $env:SCREENARR_TEST_FAKE_BIN",
                "$procs = @(Get-CimInstance Win32_Process | "
                'Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like "*$needle*" })',
                "$procs | ForEach-Object { "
                "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
                "Start-Sleep -Milliseconds 100",
                "$remaining = @(Get-CimInstance Win32_Process | "
                'Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like "*$needle*" })',
                "if ($remaining.Count -gt 0) { "
                'Write-Error "failed to stop $($remaining.Count) fake-bin process(es)"; exit 1 }',
                "Write-Output $procs.Count",
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["SCREENARR_TEST_FAKE_BIN"] = str(fake_bin)
    try:
        result = subprocess.run(
            [shell, "-NoProfile", "-File", str(script_path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        warnings.warn(f"failed to stop fake-bin processes: {exc}", stacklevel=2)
    else:
        if result.returncode != 0:
            warnings.warn(
                f"failed to stop fake-bin processes:\n{result.stdout}{result.stderr}",
                stacklevel=2,
            )
    finally:
        script_path.unlink(missing_ok=True)


def docker_log_args(log_path: Path) -> list[str]:
    return [
        line.removeprefix("ARG:")
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("ARG:")
    ]


def docker_logged_commands(log_path: Path) -> list[list[str]]:
    commands: list[list[str]] = []
    current: list[str] | None = None
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("RAW:"):
            if current is not None:
                commands.append(normalize_batch_logged_args(current))
            current = []
        elif line.startswith("ARG:") and current is not None:
            current.append(line.removeprefix("ARG:"))
    if current is not None:
        commands.append(normalize_batch_logged_args(current))
    return commands


def normalize_batch_logged_args(args: list[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(args):
        if (
            args[index] == "--env"
            and index + 2 < len(args)
            and args[index + 1] in {"GIT_OPTIONAL_LOCKS", "HOME"}
        ):
            normalized.extend(["--env", f"{args[index + 1]}={args[index + 2]}"])
            index += 3
            continue
        normalized.append(args[index])
        index += 1
    return normalized


def assert_adjacent_args(args: list[str], expected: list[str]) -> None:
    for offset in range(len(args) - len(expected) + 1):
        if args[offset : offset + len(expected)] == expected:
            return
    message = f"expected adjacent args {expected!r} in {args!r}"
    raise AssertionError(message)


def assert_docker_runner_shape(
    command: list[str],
    workspace_mount: str,
    image: str,
) -> None:
    assert_adjacent_args(
        command,
        [
            "run",
            "--rm",
            "-e",
            "CODERABBIT_API_KEY",
            "--env",
            "GIT_OPTIONAL_LOCKS=0",
            "--env",
            "HOME=/tmp/coderabbit-home",
            "-v",
            workspace_mount,
            "-w",
            "/workspace",
            image,
            "bash",
            "-lc",
        ],
    )
    assert "--api-key" in command[-1]
    assert "$CODERABBIT_API_KEY" in command[-1]


def assert_coderabbit_secret_not_logged(
    *,
    output: str,
    docker_args: list[str],
    secret: str,
) -> None:
    assert secret not in output
    assert secret not in "\n".join(docker_args)


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


def write_fake_docker(
    fake_bin: Path,
    log_path: Path,
    *,
    fail_auth_run: bool = False,
    fail_review_run: bool = False,
    hang_info: bool = False,
) -> None:
    review_fixture_path = REPO_ROOT / "tests/fixtures/coderabbit-docker-clean.ndjson"
    auth_failure_lines = []
    if fail_auth_run:
        auth_failure_lines = [
            (
                'if "!command!"=="run" if not '
                '"!all_args:auth status --agent=!"=="!all_args!" '
                "echo %CODERABBIT_API_KEY% 1>&2"
            ),
            (
                'if "!command!"=="run" if not '
                '"!all_args:auth status --agent=!"=="!all_args!" exit /b 9'
            ),
        ]
    review_failure_lines = []
    if fail_review_run:
        review_failure_lines = [
            (
                'if "!command!"=="run" if not '
                '"!all_args:review --agent=!"=="!all_args!" '
                "echo docker review stdout %CODERABBIT_API_KEY% %BRIDGE_API_KEY%"
            ),
            (
                'if "!command!"=="run" if not '
                '"!all_args:review --agent=!"=="!all_args!" '
                "echo docker review failed %CODERABBIT_API_KEY% %BRIDGE_API_KEY% 1>&2"
            ),
            (
                'if "!command!"=="run" if not '
                '"!all_args:review --agent=!"=="!all_args!" exit /b 9'
            ),
        ]
    fake_bin.joinpath("docker.cmd").write_text(
        "\r\n".join(
            [
                "@echo off",
                "setlocal EnableDelayedExpansion",
                'set "command=%~1"',
                'set "all_args="',
                f'>>"{log_path}" echo RAW:%*',
                ":log_args",
                'if "%~1"=="" goto after_log',
                'set "arg=%~1"',
                'set "all_args=!all_args! !arg!"',
                f'>>"{log_path}" echo ARG:!arg!',
                "shift",
                "goto log_args",
                ":after_log",
                (
                    'if "!command!"=="info" goto hang_info'
                    if hang_info
                    else 'if "!command!"=="info" exit /b 0'
                ),
                'if "!command!"=="build" exit /b 0',
                *auth_failure_lines,
                *review_failure_lines,
                f'if "!command!"=="run" type "{review_fixture_path}"',
                'if "!command!"=="run" exit /b 0',
                "exit /b 1",
                ":hang_info",
                r'"%SystemRoot%\System32\ping.exe" -n 2 127.0.0.1 >nul',
                "goto hang_info",
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


def write_unavailable_wsl(fake_bin: Path) -> None:
    fake_bin.joinpath("wsl.cmd").write_text(
        "@echo off\r\nexit /b 1\r\n",
        encoding="utf-8",
    )


def install_fake_docker(
    fake_bin: Path,
    *,
    fail_auth_run: bool = False,
    fail_review_run: bool = False,
    hang_info: bool = False,
) -> Path:
    docker_log = fake_bin.parent / "docker-args.log"
    write_fake_docker(
        fake_bin,
        docker_log,
        fail_auth_run=fail_auth_run,
        fail_review_run=fail_review_run,
        hang_info=hang_info,
    )
    return docker_log


@pytest.fixture
def fake_bin_with_docker(fake_bin_with_git: tuple[str, Path]) -> tuple[str, Path, Path]:
    shell, fake_bin = fake_bin_with_git
    write_unavailable_wsl(fake_bin)
    docker_log = install_fake_docker(fake_bin)
    return shell, fake_bin, docker_log


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


@pytest.mark.parametrize(
    ("api_key", "expected"),
    [
        ("test-review-key", "docker"),
        (None, "missing"),
        ("   ", "missing"),
    ],
)
@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_detects_docker_runner_by_api_key_state(
    fake_bin_with_docker: tuple[str, Path, Path],
    api_key: str | None,
    expected: str,
) -> None:
    shell, fake_bin, _docker_log = fake_bin_with_docker
    env = powershell_env_with_isolated_path(fake_bin)
    if api_key is not None:
        env["CODERABBIT_API_KEY"] = api_key

    result = run_review_gate_command(
        shell=shell,
        extra_args=["-PrintCodeRabbitRunner"],
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == expected


@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_times_out_hung_docker_runner_probe(
    fake_bin_with_git: tuple[str, Path],
) -> None:
    shell, fake_bin = fake_bin_with_git
    write_unavailable_wsl(fake_bin)
    install_fake_docker(fake_bin, hang_info=True)
    env = powershell_env_with_isolated_path(fake_bin)
    env["SCREENARR_TEST_DOCKER_INFO_TIMEOUT_MS"] = "2000"

    try:
        result = run_review_gate_command(
            shell=shell,
            extra_args=["-PrintCodeRabbitRunner"],
            env=env,
        )
    finally:
        stop_fake_bin_processes(fake_bin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "missing"


@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_honors_explicit_docker_runner_over_native(
    fake_bin_with_docker: tuple[str, Path, Path],
) -> None:
    shell, fake_bin, _docker_log = fake_bin_with_docker
    fake_bin.joinpath("coderabbit.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    env = powershell_env_with_isolated_path(fake_bin)
    env["CODERABBIT_API_KEY"] = "test-review-key"

    result = run_review_gate_command(
        shell=shell,
        extra_args=["-PrintCodeRabbitRunner", "-CodeRabbitRunner", "docker"],
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "docker"


@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_explicit_unavailable_runner_does_not_fallback_to_native(
    fake_bin_with_git: tuple[str, Path],
) -> None:
    shell, fake_bin = fake_bin_with_git
    fake_bin.joinpath("coderabbit.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    env = powershell_env_with_isolated_path(fake_bin)

    result = run_review_gate_command(
        shell=shell,
        extra_args=["-PrintCodeRabbitRunner", "-CodeRabbitRunner", "docker"],
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "missing"


@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_validates_explicit_runner_override(
    fake_bin_with_git: tuple[str, Path],
) -> None:
    shell, fake_bin = fake_bin_with_git
    env = powershell_env_with_isolated_path(fake_bin)
    env["CODERABBIT_API_KEY"] = "test-review-key"

    result = run_review_gate_command(
        shell=shell,
        extra_args=["-PrintCodeRabbitRunner", "-CodeRabbitRunner", "docker"],
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "missing"


@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_explicit_docker_runner_requires_api_key_at_invocation(
    fake_bin_with_docker: tuple[str, Path, Path],
) -> None:
    shell, fake_bin, docker_log = fake_bin_with_docker
    env = powershell_env_with_isolated_path(fake_bin)
    env["CODERABBIT_API_KEY"] = "   "

    result = run_review_gate_command(
        shell=shell,
        extra_args=[
            "-CodexReviewConfirmed",
            "-SkipLocalChecks",
            "-SkipDockerBuild",
            "-CodeRabbitRunner",
            "docker",
        ],
        env=env,
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "Requested CodeRabbit runner 'docker' is unavailable." in output
    assert "CodeRabbit CLI was not found natively, in WSL, or through Docker." in output
    assert not docker_log.exists()


@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_docker_runner_invocation_shape(
    fake_bin_with_docker: tuple[str, Path, Path],
) -> None:
    shell, fake_bin, docker_log = fake_bin_with_docker
    env = powershell_env_with_isolated_path(fake_bin)
    review_key_value = "test-review-key"
    env["CODERABBIT_API_KEY"] = review_key_value

    result = run_review_gate_command(
        shell=shell,
        extra_args=[
            "-CodexReviewConfirmed",
            "-SkipLocalChecks",
            "-SkipDockerBuild",
            "-CodeRabbitRunner",
            "docker",
        ],
        env=env,
    )
    output = result.stdout + result.stderr
    docker_args = docker_log_args(docker_log)
    commands = docker_logged_commands(docker_log)
    build_command = next(command for command in commands if command[0] == "build")
    run_commands = [command for command in commands if command[0] == "run"]

    assert result.returncode == 0, output
    assert_adjacent_args(
        build_command,
        [
            "build",
            "-f",
            str(REPO_ROOT / "scripts/coderabbit.Dockerfile"),
            "-t",
            "screenarr-coderabbit-cli:local",
            str(REPO_ROOT / "scripts"),
        ],
    )
    assert len(run_commands) == 2
    for command in run_commands:
        assert_docker_runner_shape(
            command,
            f"{REPO_ROOT}:/workspace:ro",
            "screenarr-coderabbit-cli:local",
        )
    assert "coderabbit auth status --agent" in run_commands[0][-1]
    assert "coderabbit review --agent -t uncommitted -c AGENTS.md" in run_commands[1][-1]
    assert_coderabbit_secret_not_logged(
        output=output,
        docker_args=docker_args,
        secret=review_key_value,
    )


@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_surfaces_docker_review_run_failure(
    fake_bin_with_git: tuple[str, Path],
) -> None:
    shell, fake_bin = fake_bin_with_git
    write_unavailable_wsl(fake_bin)
    docker_log = install_fake_docker(fake_bin, fail_review_run=True)
    env = powershell_env_with_isolated_path(fake_bin)
    review_key_value = "test-review-key"
    bridge_key_value = "bridge-review-secret"
    env["CODERABBIT_API_KEY"] = review_key_value
    env["BRIDGE_API_KEY"] = bridge_key_value

    result = run_review_gate_command(
        shell=shell,
        extra_args=[
            "-CodexReviewConfirmed",
            "-SkipLocalChecks",
            "-SkipDockerBuild",
            "-CodeRabbitRunner",
            "docker",
        ],
        env=env,
    )
    output = result.stdout + result.stderr
    log_text = docker_log.read_text(encoding="utf-8")
    stderr_text = (REPO_ROOT / ".review-gate/coderabbit.stderr").read_text(
        encoding="utf-8"
    )
    stdout_text = (REPO_ROOT / ".review-gate/coderabbit.ndjson").read_text(
        encoding="utf-8"
    )

    assert result.returncode != 0
    assert "CodeRabbit review failed with exit code 9" in output
    assert "coderabbit review --agent" in log_text
    assert review_key_value not in output
    assert review_key_value not in stderr_text
    assert review_key_value not in stdout_text
    assert bridge_key_value not in output
    assert bridge_key_value not in stderr_text
    assert bridge_key_value not in stdout_text
    assert "[REDACTED_SECRET]" in stderr_text
    assert "[REDACTED_SECRET]" in stdout_text


@WINDOWS_REVIEW_GATE_TEST
def test_review_gate_redacts_docker_auth_failure_stderr(
    fake_bin_with_git: tuple[str, Path],
) -> None:
    shell, fake_bin = fake_bin_with_git
    write_unavailable_wsl(fake_bin)
    docker_log = install_fake_docker(fake_bin, fail_auth_run=True)
    fake_coderabbit_key = "cr-" + "0123456789abcdef0123456789abcdef"
    env = powershell_env_with_isolated_path(fake_bin)
    env["CODERABBIT_API_KEY"] = fake_coderabbit_key

    result = run_review_gate_command(
        shell=shell,
        extra_args=[
            "-CodexReviewConfirmed",
            "-SkipLocalChecks",
            "-SkipDockerBuild",
            "-CodeRabbitRunner",
            "docker",
        ],
        env=env,
    )
    output = result.stdout + result.stderr
    log_text = docker_log.read_text(encoding="utf-8")
    auth_stderr_text = (REPO_ROOT / ".review-gate/coderabbit-auth.stderr").read_text(
        encoding="utf-8"
    )

    assert result.returncode != 0
    assert "CodeRabbit auth failed with exit code 9" in output
    assert fake_coderabbit_key not in output
    assert fake_coderabbit_key not in auth_stderr_text
    assert fake_coderabbit_key not in log_text
    assert "[REDACTED_SECRET]" in output
    assert "[REDACTED_SECRET]" in auth_stderr_text


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
