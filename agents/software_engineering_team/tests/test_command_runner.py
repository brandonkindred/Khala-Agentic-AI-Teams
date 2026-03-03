"""Unit tests for command_runner repair helpers and scaffold."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from software_engineering_team.shared.command_runner import (
    CommandResult,
    _ensure_angular_common_in_package_json,
    _ensure_angular_material_in_package_json,
    _ensure_app_config_di_token_imports,
    _ensure_material_theme_in_styles,
    _ensure_provide_animations_in_config,
    _ensure_tsconfig_module_resolution,
    ensure_frontend_dependencies_installed,
    ensure_frontend_project_initialized,
    is_ng_build_environment_failure,
    run_command,
    run_ng_serve_smoke_test,
    _get_nvm_script_prefix,
)


def test_command_result_output_property() -> None:
    """CommandResult.output combines stdout and stderr."""
    r = CommandResult(success=True, exit_code=0, stdout="out", stderr="err")
    assert "out" in r.output and "err" in r.output
    r2 = CommandResult(success=True, exit_code=0, stdout="", stderr="")
    assert r2.output == ""


def test_command_result_error_summary_success() -> None:
    """CommandResult.error_summary returns empty when success."""
    r = CommandResult(success=True, exit_code=0, stdout="", stderr="")
    assert r.error_summary == ""


def test_command_result_error_summary_timed_out() -> None:
    """CommandResult.error_summary returns 'Command timed out' when timed_out."""
    r = CommandResult(success=False, exit_code=-1, stdout="", stderr="", timed_out=True)
    assert r.error_summary == "Command timed out"


def test_command_result_error_summary_truncates_long_output() -> None:
    """CommandResult.error_summary truncates long stderr."""
    long_err = "x" * 5000
    r = CommandResult(success=False, exit_code=1, stdout="", stderr=long_err)
    assert "[truncated]" in r.error_summary
    assert len(r.error_summary) < 4100


def test_command_result_pytest_error_summary_extracts_errors_section() -> None:
    """pytest_error_summary returns the ERRORS/FAILURES section for agent feedback."""
    stdout = (
        "============================= test session starts ==============================\n"
        "platform linux -- Python 3.8.10\n"
        "rootdir: /home/\n"
        "collected 0 items / 1 error\n\n"
        "==================================== ERRORS ====================================\n"
        "_____________________ ERROR collecting tests/test_tasks.py _____________________\n"
        "ImportError while importing test module: No module named 'sqlalchemy'\n"
    )
    r = CommandResult(success=False, exit_code=2, stdout=stdout, stderr="")
    summary = r.pytest_error_summary()
    assert "ERROR collecting" in summary or "= ERRORS =" in summary
    assert "ImportError" in summary
    assert "sqlalchemy" in summary


def test_command_result_pytest_error_summary_preserves_up_to_max_chars() -> None:
    """pytest_error_summary preserves up to max_chars (default 2500) for agent feedback."""
    long_failure = "= FAILURES =\n" + "x" * 3000
    r = CommandResult(success=False, exit_code=1, stdout=long_failure, stderr="")
    summary = r.pytest_error_summary()
    assert len(summary) <= 2500 + 50  # 2500 + "... [truncated]" and newline
    assert "= FAILURES =" in summary
    assert "[truncated]" in summary


def test_is_ng_build_environment_failure_success_returns_false() -> None:
    """is_ng_build_environment_failure returns False when result is success."""
    r = CommandResult(success=True, exit_code=0, stdout="", stderr="")
    assert is_ng_build_environment_failure(r) is False


def test_is_ng_build_environment_failure_node_version() -> None:
    """is_ng_build_environment_failure returns True when stderr mentions Node.js version."""
    r = CommandResult(success=False, exit_code=1, stdout="", stderr="Node.js version is too old")
    assert is_ng_build_environment_failure(r) is True


def test_is_ng_build_environment_failure_requires_minimum_node() -> None:
    """is_ng_build_environment_failure returns True when requires minimum Node."""
    r = CommandResult(success=False, exit_code=1, stdout="", stderr="requires a minimum Node")
    assert is_ng_build_environment_failure(r) is True


def test_is_ng_build_environment_failure_code_error_returns_false() -> None:
    """is_ng_build_environment_failure returns False for code compilation errors."""
    r = CommandResult(success=False, exit_code=1, stdout="", stderr="TS2304: Cannot find name 'foo'")
    assert is_ng_build_environment_failure(r) is False


@patch("shared.command_runner.subprocess.run")
def test_run_command_success(mock_run: object, tmp_path: Path) -> None:
    """run_command returns success when subprocess succeeds."""
    mock_run.return_value = subprocess.CompletedProcess(
        args=["echo"], returncode=0, stdout="ok", stderr=""
    )
    r = run_command(["echo", "hi"], cwd=tmp_path, timeout=5)
    assert r.success
    assert r.stdout == "ok"


@patch("shared.command_runner.subprocess.run")
def test_run_command_failure(mock_run: object, tmp_path: Path) -> None:
    """run_command returns failure when subprocess fails."""
    mock_run.return_value = subprocess.CompletedProcess(
        args=["false"], returncode=1, stdout="", stderr="error"
    )
    r = run_command(["false"], cwd=tmp_path, timeout=5)
    assert not r.success
    assert r.exit_code == 1


@patch("shared.command_runner.subprocess.run")
def test_run_command_file_not_found(mock_run: object, tmp_path: Path) -> None:
    """run_command returns failure when command not found."""
    mock_run.side_effect = FileNotFoundError()
    r = run_command(["nonexistent_cmd_xyz"], cwd=tmp_path, timeout=5)
    assert not r.success
    assert "not found" in r.stderr.lower()


@patch("shared.command_runner.subprocess.run")
def test_run_command_timeout(mock_run: object, tmp_path: Path) -> None:
    """run_command returns timed_out when subprocess times out."""
    mock_run.side_effect = subprocess.TimeoutExpired("sleep", 1)
    r = run_command(["sleep", "99"], cwd=tmp_path, timeout=1)
    assert not r.success
    assert r.timed_out


@patch("shared.command_runner.subprocess.run")
def test_run_command_generic_exception(mock_run: object, tmp_path: Path) -> None:
    """run_command returns failure on unexpected exception."""
    mock_run.side_effect = RuntimeError("unexpected")
    r = run_command(["echo"], cwd=tmp_path, timeout=5)
    assert not r.success
    assert "unexpected" in r.stderr


@patch("shared.command_runner.subprocess.run")
def test_run_command_env_override(mock_run: object, tmp_path: Path) -> None:
    """run_command passes env_override to subprocess."""
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    run_command(["echo"], cwd=tmp_path, timeout=5, env_override={"CUSTOM": "value"})
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args[1]
    assert call_kwargs["env"].get("CUSTOM") == "value"


@patch("shared.command_runner.subprocess.Popen")
def test_run_ng_serve_smoke_test_success_when_timeout(mock_popen: object, tmp_path: Path) -> None:
    """run_ng_serve_smoke_test returns success when process runs past timeout (server started)."""
    import subprocess
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.communicate.side_effect = subprocess.TimeoutExpired("ng serve", 30)
    mock_proc.wait.return_value = 0
    mock_proc.returncode = 0
    mock_popen.return_value = mock_proc
    with patch("os.killpg"):
        with patch("os.getpgid", return_value=12345):
            r = run_ng_serve_smoke_test(tmp_path, port=4300)
    assert r.success


@patch("shared.command_runner.subprocess.Popen")
def test_run_ng_serve_smoke_test_fail_when_exits_early(mock_popen: object, tmp_path: Path) -> None:
    """run_ng_serve_smoke_test returns failure when process exits within timeout."""
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = ("", "error")
    mock_popen.return_value = mock_proc
    r = run_ng_serve_smoke_test(tmp_path, port=4301)
    assert not r.success
    assert r.exit_code == 1


def test_ensure_angular_material_in_package_json_adds_missing_deps(tmp_path: Path) -> None:
    """When package.json lacks @angular/material and @angular/cdk, they are added."""
    pkg = tmp_path / "package.json"
    pkg.write_text('{"dependencies": {"@angular/core": "^18.0.0"}}', encoding="utf-8")
    _ensure_angular_material_in_package_json(tmp_path)
    data = __import__("json").loads(pkg.read_text(encoding="utf-8"))
    assert "@angular/material" in data["dependencies"]
    assert "@angular/cdk" in data["dependencies"]


def test_ensure_angular_material_in_package_json_noop_when_present(tmp_path: Path) -> None:
    """When package.json already has Material, it is unchanged."""
    orig = '{"dependencies": {"@angular/material": "^18.0.0", "@angular/cdk": "^18.0.0"}}'
    pkg = tmp_path / "package.json"
    pkg.write_text(orig, encoding="utf-8")
    _ensure_angular_material_in_package_json(tmp_path)
    assert pkg.read_text(encoding="utf-8") == orig


def test_ensure_angular_material_in_package_json_no_package_json(tmp_path: Path) -> None:
    """When package.json does not exist, no error."""
    _ensure_angular_material_in_package_json(tmp_path)
    assert not (tmp_path / "package.json").exists()


def test_ensure_angular_material_in_package_json_malformed_json(tmp_path: Path) -> None:
    """When package.json is malformed, no crash; file is left unchanged."""
    pkg = tmp_path / "package.json"
    pkg.write_text("{ invalid json }", encoding="utf-8")
    _ensure_angular_material_in_package_json(tmp_path)
    assert pkg.read_text(encoding="utf-8") == "{ invalid json }"


def test_ensure_angular_common_in_package_json_adds_missing(tmp_path: Path) -> None:
    """When package.json lacks @angular/common, it is added."""
    pkg = tmp_path / "package.json"
    pkg.write_text('{"dependencies": {"@angular/core": "^18.0.0"}}', encoding="utf-8")
    _ensure_angular_common_in_package_json(tmp_path)
    data = __import__("json").loads(pkg.read_text(encoding="utf-8"))
    assert "@angular/common" in data["dependencies"]


def test_ensure_angular_common_in_package_json_noop_when_present(tmp_path: Path) -> None:
    """When package.json already has @angular/common, it is unchanged."""
    orig = '{"dependencies": {"@angular/common": "^18.0.0"}}'
    pkg = tmp_path / "package.json"
    pkg.write_text(orig, encoding="utf-8")
    _ensure_angular_common_in_package_json(tmp_path)
    assert pkg.read_text(encoding="utf-8") == orig


def test_ensure_angular_common_in_package_json_malformed_json(tmp_path: Path) -> None:
    """When package.json is malformed, no crash."""
    pkg = tmp_path / "package.json"
    pkg.write_text("not json at all", encoding="utf-8")
    _ensure_angular_common_in_package_json(tmp_path)
    assert pkg.read_text(encoding="utf-8") == "not json at all"


def test_ensure_tsconfig_module_resolution_fixes_node(tmp_path: Path) -> None:
    """When tsconfig.json has moduleResolution node, it is changed to bundler."""
    ts = tmp_path / "tsconfig.json"
    ts.write_text('{"compilerOptions": {"moduleResolution": "node"}}', encoding="utf-8")
    _ensure_tsconfig_module_resolution(tmp_path)
    data = __import__("json").loads(ts.read_text(encoding="utf-8"))
    assert data["compilerOptions"]["moduleResolution"] == "bundler"


def test_ensure_tsconfig_module_resolution_noop_when_bundler(tmp_path: Path) -> None:
    """When tsconfig.json already has bundler, it is unchanged."""
    orig = '{"compilerOptions": {"moduleResolution": "bundler"}}'
    ts = tmp_path / "tsconfig.json"
    ts.write_text(orig, encoding="utf-8")
    _ensure_tsconfig_module_resolution(tmp_path)
    assert ts.read_text(encoding="utf-8") == orig


def test_ensure_tsconfig_module_resolution_no_tsconfig(tmp_path: Path) -> None:
    """When tsconfig.json does not exist, no error."""
    _ensure_tsconfig_module_resolution(tmp_path)
    assert not (tmp_path / "tsconfig.json").exists()


def test_ensure_tsconfig_module_resolution_malformed_json(tmp_path: Path) -> None:
    """When tsconfig.json is malformed, no crash."""
    ts = tmp_path / "tsconfig.json"
    ts.write_text("{ broken }", encoding="utf-8")
    _ensure_tsconfig_module_resolution(tmp_path)
    assert ts.read_text(encoding="utf-8") == "{ broken }"


def test_ensure_material_theme_in_styles_adds_theme_when_missing(tmp_path: Path) -> None:
    """When styles.scss has no Material theme, it is prepended."""
    src = tmp_path / "src"
    src.mkdir()
    styles = src / "styles.scss"
    styles.write_text("/* custom */\nbody { margin: 0; }\n", encoding="utf-8")
    _ensure_material_theme_in_styles(tmp_path)
    content = styles.read_text(encoding="utf-8")
    assert "@use '@angular/material/prebuilt-themes/indigo-pink.css'" in content
    assert "/* custom */" in content


def test_ensure_material_theme_in_styles_noop_when_present(tmp_path: Path) -> None:
    """When styles.scss already has Material theme, it is unchanged."""
    src = tmp_path / "src"
    src.mkdir()
    orig = "@use '@angular/material/prebuilt-themes/indigo-pink.css';\nbody { margin: 0; }\n"
    styles = src / "styles.scss"
    styles.write_text(orig, encoding="utf-8")
    _ensure_material_theme_in_styles(tmp_path)
    assert styles.read_text(encoding="utf-8") == orig


def test_ensure_material_theme_in_styles_no_styles_file(tmp_path: Path) -> None:
    """When no styles file exists, no error."""
    _ensure_material_theme_in_styles(tmp_path)
    assert not (tmp_path / "src" / "styles.scss").exists()


def test_ensure_provide_animations_in_config_adds_when_missing(tmp_path: Path) -> None:
    """When app.config.ts lacks provideAnimations, it is added."""
    app = tmp_path / "src" / "app"
    app.mkdir(parents=True)
    config = app / "app.config.ts"
    config.write_text(
        """import { ApplicationConfig, provideZoneChangeDetection } from '@angular/core';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { routes } from './app.routes';

export const appConfig: ApplicationConfig = {
  providers: [
    provideZoneChangeDetection({ eventCoalescing: true }),
    provideRouter(routes),
    provideHttpClient(),
  ],
};
""",
        encoding="utf-8",
    )
    _ensure_provide_animations_in_config(tmp_path)
    content = config.read_text(encoding="utf-8")
    assert "provideAnimations" in content
    assert "provideAnimations()" in content


def test_ensure_provide_animations_in_config_noop_when_present(tmp_path: Path) -> None:
    """When app.config.ts already has provideAnimations, it is unchanged."""
    app = tmp_path / "src" / "app"
    app.mkdir(parents=True)
    orig = """import { provideAnimations } from '@angular/platform-browser/animations';
export const appConfig = { providers: [provideAnimations()] };
"""
    config = app / "app.config.ts"
    config.write_text(orig, encoding="utf-8")
    _ensure_provide_animations_in_config(tmp_path)
    assert config.read_text(encoding="utf-8") == orig


def test_ensure_provide_animations_in_config_no_config_file(tmp_path: Path) -> None:
    """When app.config.ts does not exist, no error."""
    _ensure_provide_animations_in_config(tmp_path)
    assert not (tmp_path / "src" / "app" / "app.config.ts").exists()


def test_ensure_app_config_di_token_imports_adds_http_interceptors_when_missing(tmp_path: Path) -> None:
    """When app.config.ts uses HTTP_INTERCEPTORS but does not import it, the import is added."""
    app = tmp_path / "src" / "app"
    app.mkdir(parents=True)
    config = app / "app.config.ts"
    config.write_text(
        """import { ApplicationConfig } from '@angular/core';
import { provideHttpClient, withInterceptorsFromDi } from '@angular/common/http';
import { AuthInterceptor } from './interceptors/auth.interceptor';

export const appConfig: ApplicationConfig = {
  providers: [
    provideHttpClient(withInterceptorsFromDi()),
    { provide: HTTP_INTERCEPTORS, useClass: AuthInterceptor, multi: true },
  ],
};
""",
        encoding="utf-8",
    )
    _ensure_app_config_di_token_imports(tmp_path)
    content = config.read_text(encoding="utf-8")
    assert "HTTP_INTERCEPTORS" in content
    assert "import { HTTP_INTERCEPTORS" in content or "HTTP_INTERCEPTORS," in content
    assert "@angular/common/http" in content


def test_ensure_app_config_di_token_imports_noop_when_already_imported(tmp_path: Path) -> None:
    """When HTTP_INTERCEPTORS is already imported, app.config.ts is unchanged."""
    app = tmp_path / "src" / "app"
    app.mkdir(parents=True)
    orig = """import { provideHttpClient, withInterceptorsFromDi, HTTP_INTERCEPTORS } from '@angular/common/http';
export const appConfig = { providers: [{ provide: HTTP_INTERCEPTORS, multi: true }] };
"""
    config = app / "app.config.ts"
    config.write_text(orig, encoding="utf-8")
    _ensure_app_config_di_token_imports(tmp_path)
    assert config.read_text(encoding="utf-8") == orig


def test_ensure_app_config_di_token_imports_no_config_file(tmp_path: Path) -> None:
    """When app.config.ts does not exist, no error."""
    _ensure_app_config_di_token_imports(tmp_path)
    assert not (tmp_path / "src" / "app" / "app.config.ts").exists()


def test_ensure_frontend_dependencies_installed_noop_when_no_package_json(tmp_path: Path) -> None:
    """ensure_frontend_dependencies_installed returns success when package.json does not exist."""
    result = ensure_frontend_dependencies_installed(tmp_path)
    assert result.success
    assert not (tmp_path / "package.json").exists()


@patch("shared.command_runner.run_command", return_value=CommandResult(success=True, exit_code=0, stdout="", stderr=""))
@patch("shared.command_runner._get_nvm_script_prefix", return_value=None)
def test_ensure_frontend_dependencies_uses_run_command_when_no_nvm(
    _mock_nvm: object, mock_run: object, tmp_path: Path
) -> None:
    """ensure_frontend_dependencies_installed uses run_command when NVM not available."""
    pkg = tmp_path / "package.json"
    pkg.write_text('{"dependencies": {}}', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "styles.scss").write_text("", encoding="utf-8")
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "app.config.ts").write_text(
        "export const appConfig = { providers: []; };",
        encoding="utf-8",
    )
    result = ensure_frontend_dependencies_installed(tmp_path)
    assert result.success
    mock_run.assert_called()


@patch("shared.command_runner.run_command_with_nvm", return_value=CommandResult(success=True, exit_code=0, stdout="", stderr=""))
@patch("shared.command_runner._get_nvm_script_prefix", return_value="source ~/.nvm/nvm.sh")
def test_ensure_frontend_dependencies_calls_repairs(
    _mock_nvm: object, _mock_run: object, tmp_path: Path
) -> None:
    """ensure_frontend_dependencies_installed runs repair helpers before npm install."""
    pkg = tmp_path / "package.json"
    pkg.write_text('{"dependencies": {}}', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "styles.scss").write_text("", encoding="utf-8")
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "app.config.ts").write_text(
        """import { provideHttpClient } from '@angular/common/http';
export const appConfig = { providers: [provideHttpClient(),] };
""",
        encoding="utf-8",
    )
    result = ensure_frontend_dependencies_installed(tmp_path)
    assert result.success
    data = __import__("json").loads(pkg.read_text(encoding="utf-8"))
    assert "@angular/material" in data.get("dependencies", {})


@patch.dict("os.environ", {"NVM_DIR": ""}, clear=False)
def test_get_nvm_script_prefix_returns_none_when_no_nvm(tmp_path: Path) -> None:
    """_get_nvm_script_prefix returns None when nvm.sh does not exist."""
    with patch("pathlib.Path.home", return_value=tmp_path):
        assert _get_nvm_script_prefix() is None


def test_get_nvm_script_prefix_returns_source_when_nvm_exists(tmp_path: Path) -> None:
    """_get_nvm_script_prefix returns source command when nvm.sh exists."""
    nvm_dir = tmp_path / "nvm"
    nvm_dir.mkdir()
    (nvm_dir / "nvm.sh").write_text("")
    with patch.dict("os.environ", {"NVM_DIR": str(nvm_dir)}, clear=False):
        result = _get_nvm_script_prefix()
    assert result is not None
    assert "nvm.sh" in result


def test_ensure_frontend_project_initialized_noop_when_package_json_exists(tmp_path: Path) -> None:
    """ensure_frontend_project_initialized returns early when package.json exists."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    result = ensure_frontend_project_initialized(tmp_path)
    assert result.success
    assert "Already" in result.stdout


@patch("shared.command_runner.run_command", return_value=CommandResult(success=False, exit_code=1, stdout="", stderr="npm init failed"))
@patch("shared.command_runner._get_nvm_script_prefix", return_value=None)
def test_ensure_frontend_project_initialized_returns_on_npm_init_failure(
    _mock_nvm: object, _mock_run: object, tmp_path: Path
) -> None:
    """ensure_frontend_project_initialized returns failure when npm init fails."""
    result = ensure_frontend_project_initialized(tmp_path)
    assert not result.success
    assert "npm init failed" in result.stderr


@patch("shared.command_runner.run_command", return_value=CommandResult(success=True, exit_code=0, stdout="", stderr=""))
@patch("shared.command_runner.run_command_with_nvm", return_value=CommandResult(success=True, exit_code=0, stdout="", stderr=""))
@patch("shared.command_runner._get_nvm_script_prefix", return_value=None)
def test_ensure_frontend_project_initialized_creates_environment_files(
    _mock_nvm: object, _mock_nvm_run: object, mock_run: object, tmp_path: Path
) -> None:
    """ensure_frontend_project_initialized creates src/environments/environment.ts and environment.prod.ts."""
    result = ensure_frontend_project_initialized(tmp_path)
    assert result.success
    env_dir = tmp_path / "src" / "environments"
    assert env_dir.exists()
    env_ts = env_dir / "environment.ts"
    env_prod = env_dir / "environment.prod.ts"
    assert env_ts.exists()
    assert env_prod.exists()
    assert "apiUrl" in env_ts.read_text(encoding="utf-8")
    assert "production: true" in env_prod.read_text(encoding="utf-8")


@patch("shared.command_runner.run_command", return_value=CommandResult(success=True, exit_code=0, stdout="", stderr=""))
@patch("shared.command_runner.run_command_with_nvm", return_value=CommandResult(success=True, exit_code=0, stdout="", stderr=""))
@patch("shared.command_runner._get_nvm_script_prefix", return_value=None)
def test_ensure_frontend_project_initialized_produces_material_theme_fonts_provide_animations(
    _mock_nvm: object, _mock_nvm_run: object, mock_run: object, tmp_path: Path
) -> None:
    """ensure_frontend_project_initialized produces Material theme, fonts, provideAnimations when package.json does not exist."""
    result = ensure_frontend_project_initialized(tmp_path)
    assert result.success
    index_html = tmp_path / "src" / "index.html"
    assert index_html.exists()
    index_content = index_html.read_text(encoding="utf-8")
    assert "Roboto" in index_content
    assert "Material+Icons" in index_content
    styles = tmp_path / "src" / "styles.scss"
    assert styles.exists()
    assert "indigo-pink" in styles.read_text(encoding="utf-8")
    app_config = tmp_path / "src" / "app" / "app.config.ts"
    assert app_config.exists()
    assert "provideAnimations" in app_config.read_text(encoding="utf-8")
