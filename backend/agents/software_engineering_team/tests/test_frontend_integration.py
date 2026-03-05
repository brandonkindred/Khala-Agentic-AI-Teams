"""Integration test: init frontend project, add MatButton, run ng build."""

import os
from pathlib import Path

import pytest

from software_engineering_team.shared.command_runner import (
    _get_nvm_script_prefix,
    ensure_frontend_project_initialized,
    run_ng_build_with_nvm_fallback,
)

# Skip if NVM is not available (e.g. in CI without Node)
_has_nvm = _get_nvm_script_prefix() is not None
_skip_reason = "NVM not available; integration test requires Node/npm"


@pytest.mark.skipif(not _has_nvm, reason=_skip_reason)
def test_frontend_init_matbutton_ng_build_succeeds(tmp_path: Path) -> None:
    """
    Initialize frontend project, add a minimal MatButton component, run ng build.
    Verifies the scaffold (Material, provideAnimations, theme) supports MatButton.
    """
    result = ensure_frontend_project_initialized(tmp_path)
    assert result.success, f"ensure_frontend_project_initialized failed: {result.stderr}"

    # Add MatButton to app.component.ts
    app_component = tmp_path / "src" / "app" / "app.component.ts"
    app_component.write_text(
        """import { Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet, MatButtonModule],
  template: '<router-outlet></router-outlet><button mat-button>Test</button>',
  styleUrl: './app.component.scss',
})
export class AppComponent {
  title = 'app';
}
""",
        encoding="utf-8",
    )

    build_result = run_ng_build_with_nvm_fallback(tmp_path)
    assert build_result.success, (
        f"ng build failed: exit={build_result.exit_code} "
        f"stderr={build_result.stderr[:1500]} stdout={build_result.stdout[:500]}"
    )
