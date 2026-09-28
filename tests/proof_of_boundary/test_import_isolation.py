# PB-4: Import Isolation Verification
# Verifies that this template does not directly import Level 0 (agenticstar-platform SDK)

import ast
import os
import subprocess
import sys
from pathlib import Path
import pytest


PROHIBITED_IMPORTS = [
    "agenticstar",
    "platform",  # AgentCore platform layer (not Python stdlib)
]

# Python stdlib 'platform' is allowed
STDLIB_PLATFORM_ALLOWED = True


def _scan_imports(filepath: str) -> list[str]:
    """Scan a Python file for prohibited imports using AST."""
    with open(filepath, "r") as f:
        tree = ast.parse(f.read(), filename=filepath)

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for prohibited in PROHIBITED_IMPORTS:
                    if alias.name == prohibited or alias.name.startswith(f"{prohibited}."):
                        if prohibited == "platform" and STDLIB_PLATFORM_ALLOWED:
                            # Skip Python stdlib platform
                            if alias.name == "platform":
                                continue
                        violations.append(f"{filepath}:{node.lineno} — import {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for prohibited in PROHIBITED_IMPORTS:
                    if node.module == prohibited or node.module.startswith(f"{prohibited}."):
                        if prohibited == "platform" and STDLIB_PLATFORM_ALLOWED:
                            if node.module == "platform":
                                continue
                        violations.append(f"{filepath}:{node.lineno} — from {node.module} import ...")

    return violations


def _find_python_files(directory: str) -> list[str]:
    """Find all .py files under the given directory."""
    py_files = []
    for root, _dirs, files in os.walk(directory):
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.join(root, f))
    return py_files


class TestImportIsolation:
    """PB-4: Level 3 must not import Level 0."""

    def test_no_prohibited_imports_in_src(self):
        """All source files under src/ must not import agenticstar or platform."""
        src_dir = os.path.join(os.path.dirname(__file__), "..", "..", "src")
        if not os.path.exists(src_dir):
            pytest.skip("src/ directory not found")

        violations = []
        for filepath in _find_python_files(src_dir):
            violations.extend(_scan_imports(filepath))

        assert violations == [], (
            f"Import Isolation violations found:\n" + "\n".join(violations)
        )

    def test_vendored_parts_are_never_imported_as_top_level_packages(self):
        """PB-4-A: prevent editable-install double module identities."""
        vendored = {
            "accumulation_ledger", "cat2_graph_skeleton", "entry_adapter", "evidence_gate",
            "feedback_intake", "ingest_row_pipeline", "llm_draft_stage", "output_envelope",
            "payload_store", "scoped_retrieval",
        }
        root = Path(__file__).parents[2]
        violations: list[str] = []
        for path in root.joinpath("src").rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in vendored:
                            violations.append(f"{path}:{node.lineno}: import {alias.name}")
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    if node.module.split(".")[0] in vendored:
                        violations.append(f"{path}:{node.lineno}: from {node.module}")
        assert violations == []

    def test_src_graph_imports_without_repo_src_on_sys_path(self):
        """PB-4-B: package imports remain valid outside editable-install shims."""
        root = Path(__file__).parents[2]
        code = (
            "import pathlib, sys; root=pathlib.Path.cwd().resolve(); "
            "sys.path=[p for p in sys.path if pathlib.Path(p or '.').resolve()!=root/'src']; "
            "import src.graph.graph"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=root, text=True, capture_output=True, check=False
        )
        assert result.returncode == 0, result.stderr
