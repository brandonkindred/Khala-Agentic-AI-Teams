"""Subprocess sandbox for executing generated strategy Python code."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from typing import Any, Dict, List, Optional

import pandas as pd
from pydantic import BaseModel, Field

from ...market_data_service import OHLCVBar
from ...models import BacktestConfig

logger = logging.getLogger(__name__)

EXECUTION_TIMEOUT = 120  # seconds


class CodeExecutionResult(BaseModel):
    """Result of running strategy code in the sandbox."""

    success: bool
    raw_trades: List[Dict[str, Any]] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    execution_time_seconds: float = 0.0
    error_type: Optional[str] = None


class SandboxRunner:
    """Execute generated strategy code in an isolated subprocess."""

    def __init__(self, timeout: int = EXECUTION_TIMEOUT):
        self.timeout = timeout

    def run(
        self,
        strategy_code: str,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
    ) -> CodeExecutionResult:
        """Run strategy_code against market_data in a subprocess sandbox.

        The generated code must define ``run_strategy(data, config) -> list[dict]``.
        Market data is serialized as Parquet files; results are returned as JSON on stdout.
        """
        start = time.monotonic()

        with tempfile.TemporaryDirectory(prefix="stratlab_") as tmpdir:
            try:
                # 1. Write market data as CSV files
                for symbol, bars in market_data.items():
                    df = pd.DataFrame([b.model_dump() for b in bars])
                    safe_symbol = symbol.replace("/", "_").replace("\\", "_")
                    df.to_csv(os.path.join(tmpdir, f"{safe_symbol}.csv"), index=False)

                # 2. Write strategy code
                strategy_path = os.path.join(tmpdir, "strategy.py")
                with open(strategy_path, "w", encoding="utf-8") as f:
                    f.write(strategy_code)

                # 2b. Copy indicators library so `from indicators import ...` works
                indicators_src = os.path.join(os.path.dirname(__file__), "indicators.py")
                shutil.copy2(indicators_src, os.path.join(tmpdir, "indicators.py"))

                # 3. Write harness script
                harness_path = os.path.join(tmpdir, "_harness.py")
                harness_code = self._render_harness(tmpdir, config)
                with open(harness_path, "w", encoding="utf-8") as f:
                    f.write(harness_code)

                # 4. Execute in subprocess
                env = {
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", "/tmp"),
                    "LANG": os.environ.get("LANG", "C.UTF-8"),
                }
                # Include VIRTUAL_ENV and related paths so pandas/numpy/ta are importable
                venv = os.environ.get("VIRTUAL_ENV")
                if venv:
                    env["VIRTUAL_ENV"] = venv

                result = subprocess.run(
                    [sys.executable, harness_path],
                    cwd=tmpdir,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    env=env,
                )

                elapsed = time.monotonic() - start

                if result.returncode != 0:
                    error_type = self._classify_error(result.stderr)
                    return CodeExecutionResult(
                        success=False,
                        stdout=result.stdout[:5000],
                        stderr=result.stderr[:5000],
                        execution_time_seconds=round(elapsed, 2),
                        error_type=error_type,
                    )

                # 5. Parse stdout JSON
                try:
                    trades = json.loads(result.stdout)
                    if not isinstance(trades, list):
                        return CodeExecutionResult(
                            success=False,
                            stdout=result.stdout[:5000],
                            stderr="Output is not a JSON array.",
                            execution_time_seconds=round(elapsed, 2),
                            error_type="output_validation_error",
                        )
                except json.JSONDecodeError as e:
                    return CodeExecutionResult(
                        success=False,
                        stdout=result.stdout[:5000],
                        stderr=f"Failed to parse output JSON: {e}",
                        execution_time_seconds=round(elapsed, 2),
                        error_type="output_validation_error",
                    )

                # 6. Validate trade dicts
                required_keys = {"symbol", "side", "entry_date", "entry_price", "exit_date", "exit_price", "shares"}
                for i, trade in enumerate(trades):
                    if not isinstance(trade, dict):
                        return CodeExecutionResult(
                            success=False,
                            stderr=f"Trade {i} is not a dict.",
                            execution_time_seconds=round(elapsed, 2),
                            error_type="output_validation_error",
                        )
                    missing = required_keys - set(trade.keys())
                    if missing:
                        return CodeExecutionResult(
                            success=False,
                            stderr=f"Trade {i} missing keys: {missing}",
                            execution_time_seconds=round(elapsed, 2),
                            error_type="output_validation_error",
                        )

                return CodeExecutionResult(
                    success=True,
                    raw_trades=trades,
                    stdout=result.stdout[:5000],
                    stderr=result.stderr[:2000] if result.stderr else "",
                    execution_time_seconds=round(elapsed, 2),
                )

            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - start
                return CodeExecutionResult(
                    success=False,
                    stderr=f"Execution timed out after {self.timeout}s.",
                    execution_time_seconds=round(elapsed, 2),
                    error_type="timeout",
                )
            except Exception as e:
                elapsed = time.monotonic() - start
                logger.exception("Sandbox execution failed unexpectedly")
                return CodeExecutionResult(
                    success=False,
                    stderr=str(e)[:5000],
                    execution_time_seconds=round(elapsed, 2),
                    error_type="runtime_error",
                )

    @staticmethod
    def _render_harness(data_dir: str, config: BacktestConfig) -> str:
        """Render the harness script that the subprocess executes."""
        config_json = json.dumps({
            "initial_capital": config.initial_capital,
            "transaction_cost_bps": config.transaction_cost_bps,
            "slippage_bps": config.slippage_bps,
        })

        return textwrap.dedent(f"""\
            #!/usr/bin/env python3
            \"\"\"Strategy execution harness — auto-generated, do not edit.\"\"\"
            import json
            import os
            import sys
            import traceback

            import pandas as pd

            DATA_DIR = {data_dir!r}
            CONFIG = json.loads({config_json!r})

            def main():
                # Load market data from CSV files
                data = {{}}
                for fname in sorted(os.listdir(DATA_DIR)):
                    if fname.endswith(".csv"):
                        symbol = fname[:-4]  # strip .csv
                        data[symbol] = pd.read_csv(os.path.join(DATA_DIR, fname))

                if not data:
                    print("ERROR: No market data files found", file=sys.stderr)
                    sys.exit(1)

                # Import and call the strategy
                sys.path.insert(0, DATA_DIR)
                try:
                    from strategy import run_strategy
                except ImportError as e:
                    print(f"ERROR: Cannot import run_strategy: {{e}}", file=sys.stderr)
                    sys.exit(1)

                try:
                    trades = run_strategy(data, CONFIG)
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                    sys.exit(1)

                if not isinstance(trades, list):
                    print(f"ERROR: run_strategy must return a list, got {{type(trades).__name__}}", file=sys.stderr)
                    sys.exit(1)

                # Validate output shape
                required = {{"symbol", "side", "entry_date", "entry_price", "exit_date", "exit_price", "shares"}}
                for i, t in enumerate(trades):
                    if not isinstance(t, dict):
                        print(f"ERROR: trade {{i}} is not a dict", file=sys.stderr)
                        sys.exit(1)
                    missing = required - set(t.keys())
                    if missing:
                        print(f"ERROR: trade {{i}} missing keys: {{missing}}", file=sys.stderr)
                        sys.exit(1)

                # Serialize result
                print(json.dumps(trades, default=str))

            if __name__ == "__main__":
                main()
        """)

    @staticmethod
    def _classify_error(stderr: str) -> str:
        """Classify a subprocess error based on stderr content."""
        lower = stderr.lower()
        if "syntaxerror" in lower:
            return "syntax_error"
        if "importerror" in lower or "modulenotfounderror" in lower:
            return "import_error"
        if "cannot import run_strategy" in lower:
            return "missing_function"
        return "runtime_error"
