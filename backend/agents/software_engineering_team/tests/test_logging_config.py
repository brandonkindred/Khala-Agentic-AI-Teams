"""
Tests for shared/logging_config.py (setup_logging, directory-vs-file handling).
"""

import logging
from pathlib import Path
from unittest.mock import patch

from software_engineering_team.shared.logging_config import (
    JOB_LOG_FILENAME,
    setup_logging,
)


def test_setup_logging_with_directory_uses_job_log_inside(tmp_path: Path) -> None:
    """When log_file path is an existing directory, setup_logging writes to job.log inside it."""
    assert tmp_path.is_dir()
    setup_logging(level=logging.INFO, log_file=tmp_path)

    log_file = tmp_path / JOB_LOG_FILENAME
    assert log_file.exists(), f"Expected {log_file} to exist"
    assert log_file.is_file()

    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
    assert str(log_file) in file_handlers[0].baseFilename or file_handlers[0].baseFilename.endswith(
        JOB_LOG_FILENAME
    )

    # Writing to root logger should write into the file
    root.info("test_setup_logging_with_directory_uses_job_log_inside")
    file_handlers[0].flush()
    assert "test_setup_logging_with_directory_uses_job_log_inside" in log_file.read_text()


def test_setup_logging_file_handler_oserror_does_not_raise(tmp_path: Path) -> None:
    """When FileHandler creation raises OSError, setup_logging does not raise and no file handler is added."""
    log_path = tmp_path / "path.log"
    with patch("software_engineering_team.shared.logging_config.logging.FileHandler") as mock_fh:
        mock_fh.side_effect = OSError(21, "Is a directory", str(log_path))
        setup_logging(level=logging.INFO, log_file=log_path)

    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 0


def test_setup_logging_with_nonexistent_file_path(tmp_path: Path) -> None:
    """When log_file is a non-existent file path, setup_logging creates the file and adds handler."""
    log_path = tmp_path / "output.log"
    assert not log_path.exists()
    setup_logging(level=logging.INFO, log_file=log_path)

    assert log_path.exists()
    assert log_path.is_file()
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
    root.info("test_setup_logging_with_nonexistent_file_path")
    file_handlers[0].flush()
    assert "test_setup_logging_with_nonexistent_file_path" in log_path.read_text()


def test_setup_logging_with_existing_file_path(tmp_path: Path) -> None:
    """When log_file is an existing file path, setup_logging adds handler and appends to it."""
    log_path = tmp_path / "existing.log"
    log_path.write_text("existing line\n")
    setup_logging(level=logging.INFO, log_file=log_path)

    assert log_path.exists()
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
    root.info("new line")
    file_handlers[0].flush()
    content = log_path.read_text()
    assert "existing line" in content
    assert "new line" in content
