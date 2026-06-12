# 代码审查指南：仅审查任务变更文件，避免有损的代码审查输入截断（第3阶段）

## 1. 概述

本指南旨在优化代码审查流程，重点关注任务变更文件（task-changed files），并解决在审查过程中由于输入截断导致的信息丢失问题。通过实施本指南，可以确保审查的完整性和准确性。

## 2. 核心原则

### 2.1 仅审查任务变更文件
- **定义**：任务变更文件指在特定任务或功能开发中实际修改、添加或删除的文件。
- **操作**：使用版本控制工具（如 `git diff --name-only`）精确识别这些文件，避免审查无关文件。

### 2.2 避免有损的代码审查输入截断
- **问题**：在审查工具或流程中，代码片段可能被截断（例如，仅显示前几行），导致上下文丢失。
- **解决方案**：使用全文件差异（full diff）或上下文丰富的审查工具（如支持展开/折叠的IDE插件）。

## 3. 实施步骤

### 3.1 识别任务变更文件
```bash
# 示例：获取当前分支与主分支的差异文件
git diff main...HEAD --name-only
```

### 3.2 配置审查工具
- **推荐工具**：使用支持“无截断”模式的审查工具，如：
  - GitHub Pull Request 的“Files changed”视图（确保加载完整差异）
  - 本地 IDE 的差异视图（如 VS Code 的“GitLens”插件）

### 3.3 审查流程
1. **加载完整差异**：确认每个变更文件显示全部代码行。
2. **逐文件审查**：按文件列表顺序审查，避免跳跃。
3. **记录发现**：使用结构化注释（如 `// REVIEW: <comment>`）标记问题。

## 4. 示例：错误与正确做法

### 4.1 错误做法（有损截断）
```python
# 文件：src/utils.py（仅显示前5行）
def calculate(a, b):
    # ... 截断 ...
    return result  # 缺少上下文，无法验证逻辑
```

### 4.2 正确做法（完整差异）
```python
# 文件：src/utils.py（完整显示）
def calculate(a, b):
    # 验证输入
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise ValueError("Inputs must be numbers")
    
    # 计算逻辑
    result = a + b
    
    # 边界处理
    if result > 1000:
        result = 1000  # 上限截断
    
    return result
```

## 5. 高级技巧

### 5.1 使用脚本自动化
```python
# review_filter.py
import subprocess

def get_changed_files(branch="main"):
    """获取当前分支相对于主分支的变更文件"""
    result = subprocess.run(
        ["git", "diff", f"{branch}...HEAD", "--name-only"],
        capture_output=True,
        text=True
    )
    return result.stdout.strip().split("\n")

# 使用示例
files = get_changed_files()
print(f"任务变更文件: {files}")
```

### 5.2 处理大型文件
- **策略**：对超过500行的大型文件，使用“分块审查”但确保每个块完整显示。
- **工具**：`diff --unified=3` 或 `git difftool` 支持分页。

## 6. 常见陷阱与解决方案

| 陷阱 | 解决方案 |
|------|----------|
| 审查工具默认截断 | 设置环境变量 `GIT_PAGER=cat` 禁用分页 |
| 忽略未变更的依赖文件 | 使用 `git diff --diff-filter=M` 仅显示修改文件 |
| 上下文丢失导致误解 | 要求审查工具显示前后各5行上下文（`git diff -U5`） |

## 7. 质量检查清单

- [ ] 已识别所有任务变更文件（无遗漏）
- [ ] 每个文件显示完整差异（无截断）
- [ ] 审查注释包含具体行号和建议
- [ ] 已记录因截断导致的潜在问题

## 8. 结语

通过严格遵循“仅审查任务变更文件”和“避免有损截断”的原则，可以显著提升代码审查的效率和准确性。本指南适用于所有使用 Git 的项目，特别是大型代码库中的增量审查场景。