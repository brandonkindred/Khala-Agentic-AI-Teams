"""Unit tests for HTML truncation detection and recovery utilities."""

import pytest

from software_engineering_team.shared.html_utils import (
    is_html_file,
    is_html_truncated,
    validate_html_completeness,
    get_truncated_html_files,
    get_truncated_files_summary,
    merge_html_continuation,
)


class TestIsHtmlFile:
    """Tests for is_html_file function."""

    def test_html_extension(self):
        assert is_html_file("index.html") is True
        assert is_html_file("src/app/component.html") is True

    def test_htm_extension(self):
        assert is_html_file("page.htm") is True

    def test_component_html_extension(self):
        assert is_html_file("app.component.html") is True
        assert is_html_file("src/components/header.component.html") is True

    def test_non_html_files(self):
        assert is_html_file("script.js") is False
        assert is_html_file("style.css") is False
        assert is_html_file("component.ts") is False
        assert is_html_file("data.json") is False

    def test_case_insensitive(self):
        assert is_html_file("INDEX.HTML") is True
        assert is_html_file("Page.HTM") is True


class TestIsHtmlTruncated:
    """Tests for is_html_truncated function."""

    def test_empty_content_not_truncated(self):
        assert is_html_truncated("") is False
        assert is_html_truncated("   ") is False

    def test_complete_html_not_truncated(self):
        complete = "<div><span>Hello</span></div>"
        assert is_html_truncated(complete) is False

    def test_mid_tag_truncation(self):
        truncated = '<div class="container'
        assert is_html_truncated(truncated) is True

        truncated2 = "<button type="
        assert is_html_truncated(truncated2) is True

    def test_unclosed_tag_truncation(self):
        truncated = "<div><span>Hello"
        assert is_html_truncated(truncated) is True

    def test_unclosed_quote_in_attribute(self):
        truncated = '<div class="test'
        assert is_html_truncated(truncated) is True

    def test_unbalanced_div_tags(self):
        truncated = "<div><div><span>Content</span></div>"
        assert is_html_truncated(truncated) is True  # Missing closing </div>

    def test_unbalanced_button_tags(self):
        truncated = "<button>Click me<span>icon</span>"
        assert is_html_truncated(truncated) is True

    def test_nested_complete_structure(self):
        complete = """
        <div class="container">
            <button type="button">
                <span>Click me</span>
            </button>
        </div>
        """
        assert is_html_truncated(complete) is False

    def test_self_closing_tags_not_counted(self):
        complete = '<input type="text" /><br /><hr>'
        assert is_html_truncated(complete) is False

    def test_angular_template_truncation(self):
        truncated = """
        <div *ngIf="condition">
            <button (click)="onClick()">
                {{ buttonText }}
        """
        assert is_html_truncated(truncated) is True


class TestValidateHtmlCompleteness:
    """Tests for validate_html_completeness function."""

    def test_empty_content_valid(self):
        is_valid, error = validate_html_completeness("")
        assert is_valid is True
        assert error == ""

    def test_complete_html_valid(self):
        complete = "<div><span>Hello</span></div>"
        is_valid, error = validate_html_completeness(complete)
        assert is_valid is True
        assert error == ""

    def test_mid_tag_truncation_detected(self):
        truncated = '<div class="test'
        is_valid, error = validate_html_completeness(truncated)
        assert is_valid is False
        assert "div" in error.lower() or "truncat" in error.lower()

    def test_unclosed_quote_detected(self):
        truncated = '<input type="text'
        is_valid, error = validate_html_completeness(truncated)
        assert is_valid is False
        assert "quote" in error.lower() or "truncat" in error.lower()

    def test_unbalanced_tags_detected(self):
        truncated = "<div><span>content</span>"
        is_valid, error = validate_html_completeness(truncated)
        assert is_valid is False
        assert "div" in error.lower() or "unbalanced" in error.lower()


class TestGetTruncatedHtmlFiles:
    """Tests for get_truncated_html_files function."""

    def test_no_html_files(self):
        files = {
            "app.ts": "export class App {}",
            "style.css": ".container { }",
        }
        truncated = get_truncated_html_files(files)
        assert truncated == []

    def test_complete_html_files(self):
        files = {
            "index.html": "<html><body></body></html>",
            "app.component.html": "<div>Content</div>",
        }
        truncated = get_truncated_html_files(files)
        assert truncated == []

    def test_truncated_html_detected(self):
        files = {
            "index.html": "<html><body></body></html>",
            "app.component.html": "<div><span>Content",  # Truncated
            "header.component.html": '<nav class="header',  # Truncated
        }
        truncated = get_truncated_html_files(files)
        assert len(truncated) == 2
        assert "app.component.html" in truncated
        assert "header.component.html" in truncated

    def test_mixed_files(self):
        files = {
            "app.ts": "export class App {}",
            "template.html": "<div>Complete</div>",
            "broken.html": "<div><span>",
        }
        truncated = get_truncated_html_files(files)
        assert truncated == ["broken.html"]


class TestGetTruncatedFilesSummary:
    """Tests for get_truncated_files_summary function."""

    def test_no_truncated_files(self):
        files = {
            "index.html": "<html><body></body></html>",
        }
        summary = get_truncated_files_summary(files)
        assert summary == ""

    def test_summary_contains_file_paths(self):
        files = {
            "app.component.html": "<div><span>Content",
        }
        summary = get_truncated_files_summary(files)
        assert "app.component.html" in summary

    def test_summary_contains_error_description(self):
        files = {
            "template.html": "<div><div>Nested content</div>",
        }
        summary = get_truncated_files_summary(files)
        assert "template.html" in summary
        assert len(summary) > 0


class TestMergeHtmlContinuation:
    """Tests for merge_html_continuation function."""

    def test_empty_continuation(self):
        original = "<div><span>"
        result = merge_html_continuation(original, "")
        assert result == original

    def test_simple_merge(self):
        original = "<div><span>"
        continuation = "Content</span></div>"
        result = merge_html_continuation(original, continuation)
        assert result == "<div><span>Content</span></div>"

    def test_strips_whitespace(self):
        original = "<div><span>  \n"
        continuation = "  \nContent</span></div>"
        result = merge_html_continuation(original, continuation)
        assert result == "<div><span>Content</span></div>"

    def test_preserves_content(self):
        original = '<div class="container">'
        continuation = '<button>Click</button></div>'
        result = merge_html_continuation(original, continuation)
        assert '<div class="container">' in result
        assert "<button>Click</button></div>" in result


class TestIntegration:
    """Integration tests combining multiple utilities."""

    def test_detect_truncation_generate_summary_merge(self):
        files = {
            "app.component.html": '<div class="app"><button>Click',
        }

        truncated = get_truncated_html_files(files)
        assert len(truncated) == 1

        summary = get_truncated_files_summary(files)
        assert "app.component.html" in summary

        continuation = " me</button></div>"
        merged = merge_html_continuation(files["app.component.html"], continuation)

        assert is_html_truncated(merged) is False

    def test_real_world_angular_template(self):
        truncated_template = """<div class="login-container">
  <mat-card class="login-card">
    <mat-card-header>
      <mat-card-title>Login</mat-card-title>
    </mat-card-header>
    <mat-card-content>
      <form [formGroup]="loginForm" (ngSubmit)="onSubmit()">
        <mat-form-field appearance="outline" class="full-width">
          <mat-label>Email</mat-label>
          <input matInput formControlName="email" type="email">
        </mat-form-field>
        <mat-form-field appearance="outline" class="full-width">
          <mat-label>Password</mat-label>
          <input matInput formControlName="password" type="password">
        </mat-form-field>
        <button mat-raised-button color="primary" type="submit" class="full-width">
          <mat-icon>login</mat-icon>
          <span>Sign In</span"""

        assert is_html_truncated(truncated_template) is True

        is_valid, error = validate_html_completeness(truncated_template)
        assert is_valid is False

        continuation = """</span>
        </button>
      </form>
    </mat-card-content>
  </mat-card>
</div>"""

        merged = merge_html_continuation(truncated_template, continuation)
        assert is_html_truncated(merged) is False
