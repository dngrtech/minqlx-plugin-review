#!/usr/bin/env python3
"""Conservative static checks for a minqlx -> minqlxtended plugin port.

Usage: python3 check_minqlxtended_port.py PATH [PATH ...]
Only Python standard library; dynamic hook registration is reported for manual review.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

LEGACY_PREFIXES = ("RET_", "PRI_", "WP_")
REMOVED_CALLS = {
    "set_ammo", "set_armor", "set_flight", "set_health", "set_holdable",
    "set_invulnerability", "set_position", "set_powerups", "set_privileges",
    "set_score", "set_velocity", "set_weapon", "set_weapons", "noclip",
    "allow_single_player",
}


class Checker(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.errors: list[str] = []
        self.manual: list[str] = []
        self.inventory: list[str] = []

    def report(self, node: ast.AST, message: str, manual: bool = False) -> None:
        out = f"{self.path}:{getattr(node, 'lineno', '?')}: {message}"
        (self.manual if manual else self.errors).append(out)

    def visit_Import(self, node: ast.Import) -> None:
        for name in node.names:
            if name.name == "minqlx" or name.name.startswith("minqlx."):
                self.report(node, f"legacy import: import {name.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module == "minqlx" or module.startswith("minqlx."):
            for name in node.names:
                if name.name == "*":
                    self.report(node, "legacy wildcard import; expand it before porting")
                elif name.name.startswith(LEGACY_PREFIXES):
                    self.report(node, f"legacy direct constant import: {name.name}")
                else:
                    self.report(node, f"legacy from-import: {name.name}")
        elif module == "minqlxtended" or module.startswith("minqlxtended."):
            for name in node.names:
                if name.name == "*":
                    self.report(node, "target wildcard import; use explicit supported imports")
                elif name.name.startswith(LEGACY_PREFIXES):
                    self.report(node, f"invalid target direct constant import: {name.name}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "minqlx":
            self.report(node, "legacy minqlx identifier")
        elif node.id.startswith(LEGACY_PREFIXES):
            self.report(node, f"legacy constant identifier: {node.id}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in {"red_score", "blue_score"}:
            self.report(node, f"legacy score field: .{node.attr}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in REMOVED_CALLS:
            self.report(node, f"possibly removed engine API call: .{node.func.attr}(...)")
        if isinstance(node.func, ast.Attribute) and node.func.attr == "kick":
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
                self.report(node, "Plugin.kick(...) is removed; resolve player then call Player.kick(reason)")
        self._inventory_add_hook(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._inventory_decorators(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._inventory_decorators(node)
        self.generic_visit(node)

    @staticmethod
    def _literal_event(node: ast.AST) -> str | None:
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def _inventory_add_hook(self, node: ast.Call) -> None:
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "add_hook"):
            return
        event = self._literal_event(node.args[0]) if node.args else None
        handler = ast.unparse(node.args[1]) if len(node.args) > 1 else None
        if event and handler:
            self.inventory.append(f"{self.path}:{node.lineno}: add_hook({event!r}, {handler})")
        else:
            self.report(node, "dynamic/incomplete add_hook registration; manually verify target handler contract", manual=True)

    def _inventory_decorators(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            func = decorator.func
            if not (isinstance(func, ast.Attribute) and func.attr == "hook"):
                continue
            event = self._literal_event(decorator.args[0])
            if event:
                self.inventory.append(f"{self.path}:{node.lineno}: @hook({event!r}) -> {node.name}")
            else:
                self.report(decorator, f"dynamic @hook registration for {node.name}; manually verify target handler contract", manual=True)


def python_files(paths: list[str]) -> list[Path]:
    result: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file() and path.suffix == ".py":
            result.append(path)
        elif path.is_dir():
            result.extend(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)
        else:
            print(f"warning: skipped non-Python or missing path: {path}", file=sys.stderr)
    return sorted(set(result))


def main(argv: list[str]) -> int:
    if not argv:
        print("Usage: python3 check_minqlxtended_port.py PATH [PATH ...]", file=sys.stderr)
        return 2
    errors: list[str] = []
    manual: list[str] = []
    inventory: list[str] = []
    for path in python_files(argv):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            errors.append(f"{path}: cannot parse Python: {exc}")
            continue
        checker = Checker(path)
        checker.visit(tree)
        errors.extend(checker.errors)
        manual.extend(checker.manual)
        inventory.extend(checker.inventory)
    print("Hook inventory:")
    print("\n".join(inventory) if inventory else "  (none statically recognized)")
    if manual:
        print("\nManual hook review required:")
        print("\n".join(manual))
    if errors:
        print("\nPort blockers:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("\nNo static port blockers found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
