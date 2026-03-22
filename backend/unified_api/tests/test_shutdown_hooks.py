"""Tests for unified API shutdown hooks."""

from unittest.mock import MagicMock, patch


def test_run_shutdown_hooks_calls_mounted_team_hook() -> None:
    """_run_shutdown_hooks calls mark_all_running_jobs_failed(reason) for each mounted team."""
    from unified_api import main as unified_main

    mock_fn = MagicMock()
    with patch.dict(unified_main._mounted_teams, {"blogging": True}, clear=False), patch("importlib.import_module") as import_mock:
        mod = MagicMock()
        mod.mark_all_running_jobs_failed = mock_fn
        import_mock.return_value = mod

        unified_main._run_shutdown_hooks("test")

    import_mock.assert_any_call("blogging.shared.blog_job_store")
    mock_fn.assert_called_once_with("test")


def test_run_shutdown_hooks_skips_unmounted_teams() -> None:
    """_run_shutdown_hooks does not call hook for teams that are not mounted."""
    from unified_api import main as unified_main

    with patch.dict(unified_main._mounted_teams, {"blogging": False, "software_engineering": False}, clear=False), patch("importlib.import_module") as import_mock:
        unified_main._run_shutdown_hooks("test")

    import_mock.assert_not_called()
