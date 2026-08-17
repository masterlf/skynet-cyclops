from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_runtime_has_no_llm_or_mutating_kanban_commands() -> None:
    source_files = list((REPO / "src/skynet_cyclops").glob("*.py"))
    combined = "\n".join(path.read_text(encoding="utf-8") for path in source_files)
    runtime = (REPO / "src/skynet_cyclops/tick.py").read_text(encoding="utf-8").lower()
    forbidden = ["create_task", "promote_task", "link_tasks", "run_json", "run_text"]
    assert not any(term in runtime for term in forbidden)
    assert "openai" not in combined.lower()


def test_all_subprocess_calls_are_argv_and_shell_false() -> None:
    for path in (REPO / "src/skynet_cyclops").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ):
                assert node.args and isinstance(node.args[0], (ast.Name, ast.List))
                shell = next((kw.value for kw in node.keywords if kw.arg == "shell"), None)
                assert isinstance(shell, ast.Constant) and shell.value is False
