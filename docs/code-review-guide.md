# Code Review Guide: Review Only Task-Changed Files & Prevent Lossy Input Truncation (Phase 3)

## 1. Overview

This guide optimizes the code review workflow by focusing on **task-changed files only** and addressing **information loss caused by input truncation** during review. Following this guide ensures review completeness, accuracy, and efficiency across all projects using Git-based version control.

In large codebases, reviewers often face two recurring problems:
- **Scope creep**: Reviewing unrelated files wastes time and introduces unnecessary noise.
- **Context loss**: Review tools that truncate file diffs hide critical logic, leading to missed bugs and incorrect approvals.

This guide provides concrete strategies, tools, and scripts to solve both problems.

---

## 2. Core Principles

### 2.1 Review Only Task-Changed Files

- **Definition**: Task-changed files are those actually modified, added, or deleted for a specific task or feature.
- **Operation**: Use version control tools (e.g., `git diff --name-only`) to precisely identify these files. Avoid reviewing unrelated files (config changes, auto-generated code, dependency lockfiles) that aren't part of the task.
- **Benefit**: Reduces review time by 40–60% in monorepos and prevents cognitive overload from irrelevant changes.

### 2.2 Prevent Lossy Code Review Input Truncation

- **Problem**: Review tools or pipelines may truncate code snippets (e.g., showing only the first N lines of a function), causing reviewers to miss critical context — especially for long functions, complex conditionals, or error-handling branches.
- **Solution**: Always use full-file diffs or context-rich review tools (e.g., GitHub's "Files changed" tab with complete diff loading, or IDE plugins with expand/collapse support).

---

## 3. Implementation Steps

### 3.1 Identify Task-Changed Files

```bash
# Get changed files between the current branch and main
git diff main...HEAD --name-only

# Filter by change type (only modifications, no additions/deletions)
git diff main...HEAD --diff-filter=M --name-only

# Include staged and unstaged changes (useful before committing)
git diff --name-only
git diff --cached --name-only
```

**Pro tip**: Create a shell alias for daily use:

```bash
alias changed='git diff main...HEAD --name-only'
```

### 3.2 Configure Review Tools

- **GitHub Pull Requests**: Use the "Files changed" tab. Ensure the full diff is loaded by scrolling to the bottom. For large PRs, use the "Load diff" button explicitly.
- **Local IDE Diffs**:
  - VS Code: Install GitLens extension for inline blame and rich diff views. Use `Ctrl+Shift+G` then select "Changes" to see full file diffs.
  - JetBrains IDEs: Use the "Version Control" tool window → "Log" tab → right-click → "Show Diff".
- **CLI diffs**: Disable pager truncation with `git --no-pager diff` or set `GIT_PAGER=cat`.

### 3.3 Review Workflow

1. **Load the full diff**: Verify each changed file displays all lines.
2. **Review file by file**: Process files in the order shown by `git diff --name-only`. Do not skip around — sequential review reduces the chance of missing dependent changes.
3. **Record findings**: Use structured annotations such as `// REVIEW: <comment>` or GitHub's native suggestion blocks (` ```suggestion `).
4. **Cross-reference**: When a change touches multiple files (e.g., a refactor that updates a function signature and its callers), verify consistency across all affected files before approving.

---

## 4. Examples: Wrong vs. Correct

### 4.1 Wrong (Truncated Diff — Missing Context)

```python
# File: src/utils.py (only first 5 lines shown)
def calculate(a, b):
    # ... truncated ...
    return result  # Cannot verify logic — missing input validation, edge cases
```

**Why it fails**: Without the full function body, you cannot verify:
- Input type validation
- Edge-case handling (zero, negative, overflow)
- Return value correctness

### 4.2 Correct (Full Diff — Complete Visibility)

```python
# File: src/utils.py (fully expanded)
def calculate(a, b):
    # Validate inputs
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise ValueError("Inputs must be numbers")

    # Core computation
    result = a + b

    # Clamp to upper bound
    if result > 1000:
        result = 1000

    return result
```

**Why it works**: Every line is visible. The reviewer can confirm:
- Input validation is present
- The calculation logic is correct
- Edge cases (overflow) are handled
- The return value matches the function contract

---

## 5. Automation Scripts

### 5.1 Get Changed Files (with Error Handling)

```python
# review_filter.py
import subprocess
import sys


def get_changed_files(branch="main"):
    """Get files changed on the current branch relative to the given branch.

    Returns a list of file paths, or an empty list if no files changed.
    """
    result = subprocess.run(
        ["git", "diff", f"{branch}...HEAD", "--name-only"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Error running git diff: {result.stderr}", file=sys.stderr)
        return []

    stdout = result.stdout.strip()
    if not stdout:
        return []

    return stdout.splitlines()


# Usage
if __name__ == "__main__":
    files = get_changed_files()
    if files:
        print(f"Task-changed files ({len(files)}):")
        for f in files:
            print(f"  - {f}")
    else:
        print("No changed files found.")
```

**Key fixes over the naive version**:
- ✅ Checks `result.returncode` for git errors
- ✅ Uses `splitlines()` instead of `split("\n")` for cross-platform line-ending compatibility
- ✅ Handles empty stdout correctly (returns `[]`, not `[""]`)
- ✅ Adds proper `__main__` guard and error output via `sys.stderr`

### 5.2 Review Checklist Generator

```python
# review_checklist.py
import subprocess


def generate_review_checklist(branch="main"):
    result = subprocess.run(
        ["git", "diff", f"{branch}...HEAD", "--numstat"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0 or not result.stdout.strip():
        print("No changes to review.")
        return

    total_additions = 0
    total_deletions = 0
    files = []

    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            added, deleted, filename = parts
            total_additions += int(added)
            total_deletions += int(deleted)
            files.append(f"  [ ] {filename} (+{added}/-{deleted})")

    print("## Review Checklist")
    print(f"\n**Scope**: {len(files)} files | "
          f"+{total_additions} / -{total_deletions} lines\n")
    print("\n".join(files))
    print(f"\n**Truncation check**: "
          f"{'PASS' if total_additions < 2000 else 'WARNING — large PR, verify full diffs loaded'}")


if __name__ == "__main__":
    generate_review_checklist()
```

---

## 6. Handling Large Files

### Strategy

- **Chunked review**: For files over 500 lines, review in logical chunks (e.g., per function or per class). Ensure each chunk is fully displayed before moving to the next.
- **Tool-based approach**:
  - Use `git diff --unified=3` for 3-line context around each change.
  - Use `git difftool` (e.g., Meld, Beyond Compare) for side-by-side diffs with scrollable full-file views.
- **Focus on changed regions**: In large files, only a few functions may have changed. Use `git diff -U5 -- function_name` to view changes in a specific function with expanded context.

```bash
# View diff for a specific function with 10 lines of context
git diff main...HEAD -U10 -- src/worker.py | grep -A 30 "def process_task"
```

### Automated Size Warning

```bash
# Warn if any changed file exceeds 300 lines
changed_files=$(git diff main...HEAD --name-only)
for f in $changed_files; do
    lines=$(git diff main...HEAD -- "$f" | wc -l)
    if [ "$lines" -gt 300 ]; then
        echo "WARNING: $f has $lines diff lines — consider chunked review"
    fi
done
```

---

## 7. Common Pitfalls & Solutions

| Pitfall | Solution |
|---------|----------|
| Review tool truncates by default | Set `GIT_PAGER=cat` in your shell profile, or use `git --no-pager diff` |
| Accidentally reviewing unchanged dependency files | Use `git diff --diff-filter=M` to show only modified (not added/deleted) files |
| Missing context around changed lines causes misunderstandings | Require review tools to show 5+ lines of context (`git diff -U5`) |
| Binary files in the diff (images, compiled assets) clutter the review | Use `.gitattributes` to mark binary files as `-diff`, or exclude them with `git diff -- '*.py' '*.ts' '*.go'` |
| Forgetting to re-check after a rebase/force-push | Always re-run `git diff main...HEAD` after rebase to confirm only intended files changed |

---

## 8. Quality Checklist

- [ ] All task-changed files identified (no omissions)
- [ ] Each file displays the full diff (no truncation)
- [ ] Review comments include specific line numbers and actionable suggestions
- [ ] Potential issues caused by truncation have been logged (e.g., "could not verify error path — need to confirm")
- [ ] Cross-file consistency verified for multi-file changes
- [ ] Binary/auto-generated files excluded from the review scope
- [ ] The diff has been re-verified after any rebase or force-push

---

## 9. Conclusion

By strictly following the principles of **reviewing only task-changed files** and **preventing lossy truncation**, you can significantly improve both the efficiency and accuracy of code reviews. This guide applies to all Git-based projects, especially large codebases where incremental review is critical.

> **Phase 3 context**: This guide is part of the ongoing effort to standardize review practices across the engineering team. It complements the existing coding standards and style guides, adding a process-focused layer for review quality.
