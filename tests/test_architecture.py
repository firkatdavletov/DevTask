"""Keep workflow dependencies explicit and directed toward lower layers."""
import ast
from pathlib import Path
import unittest


PACKAGE = Path(__file__).resolve().parents[1] / "dev_task"
ALLOWED_IMPORTS = {
    "console": set(),
    "events": set(),
    "models": set(),
    "__main__": {"cli"},
    "api": {"branch_review", "config", "errors", "events", "models", "processes", "storage", "workflow"},
    "errors": set(),
    "storage": set(),
    "usage": {"events"},
    "card": {"errors", "events", "models"},
    "config": {"errors", "storage", "events"},
    "contracts": {"errors"},
    "processes": {"errors", "events"},
    "state": {"errors", "storage", "models"},
    "repository": {"errors", "processes", "storage", "events"},
    "github": {"errors", "processes"},
    "plan_context": {"errors", "storage"},
    "reports": {"events", "storage", "usage"},
    "sessions": {"events", "errors", "reports", "storage", "usage"},
    "codex": {"events", "errors", "processes", "repository", "sessions", "storage", "usage"},
    "coding": {"events", "codex", "contracts", "errors", "plan_context", "processes", "repository", "storage"},
    "planning": {"events", "codex", "contracts", "errors", "plan_context", "repository", "storage", "usage"},
    "checks": {"coding", "events", "contracts", "errors", "processes", "repository", "storage"},
    "review": {
        "events",
        "checks", "codex", "contracts", "errors", "plan_context", "processes",
        "repository", "storage", "usage",
    },
    "publishing": {
        "codex", "config", "events", "contracts", "errors", "github", "plan_context",
        "processes", "repository", "storage",
    },
    "workflow": {
        "card", "checks", "coding", "config", "events", "errors", "github",
        "plan_context", "planning", "publishing", "reports", "repository", "review",
        "state", "storage", "models", "processes",
    },
    "branch_review": {"models", "codex", "config", "events", "contracts", "errors", "processes", "reports", "repository", "storage"},
    "cli": {"console", "errors", "api", "models", "events"},
}


class ArchitectureTest(unittest.TestCase):
    def dependency_graph(self):
        graph = {}
        for path in PACKAGE.glob("*.py"):
            if path.stem == "__init__":
                continue
            tree = ast.parse(path.read_text())
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.level:
                        self.assertEqual(node.level, 1, path.name)
                        self.assertTrue(node.module, path.name)
                        imports.add(node.module.split(".")[0])
                    elif node.module and node.module.startswith("dev_task."):
                        imports.add(node.module.split(".")[1])
                    else:
                        continue
                    self.assertIn(node, tree.body, f"Local package import in {path.name}")
                    self.assertNotIn("*", {alias.name for alias in node.names}, path.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotEqual(alias.name, "dev_task", path.name)
                        if alias.name.startswith("dev_task."):
                            imports.add(alias.name.split(".")[1])
                            self.assertIn(node, tree.body, path.name)
                elif isinstance(node, ast.Call):
                    self.assertFalse(isinstance(node.func, ast.Name) and node.func.id == "__import__", path.name)
                    self.assertFalse(isinstance(node.func, ast.Attribute) and node.func.attr == "import_module", path.name)
            graph[path.stem] = imports
        return graph

    def test_imports_follow_responsibility_boundaries(self):
        graph = self.dependency_graph()
        self.assertEqual(set(graph), set(ALLOWED_IMPORTS))
        for module, imports in graph.items():
            self.assertLessEqual(imports, ALLOWED_IMPORTS[module], module)

    def test_import_graph_has_no_cycles(self):
        graph = self.dependency_graph()
        visited = set()

        def visit(module, ancestors):
            self.assertNotIn(module, ancestors, " -> ".join((*ancestors, module)))
            if module in visited:
                return
            for dependency in graph[module]:
                visit(dependency, (*ancestors, module))
            visited.add(module)

        for module in graph:
            visit(module, ())

    def test_package_initialization_has_no_side_effects(self):
        tree = ast.parse((PACKAGE / "__init__.py").read_text())
        self.assertEqual(len(tree.body), 1)
        self.assertIsInstance(tree.body[0], ast.Expr)
        self.assertIsInstance(tree.body[0].value, ast.Constant)
        self.assertIsInstance(tree.body[0].value.value, str)

    def test_core_has_no_terminal_dependencies(self):
        for path in PACKAGE.glob("*.py"):
            if path.stem in ("cli", "console", "__main__"):
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertNotIn("argparse", {item.name for item in node.names}, path.name)
                if isinstance(node, ast.Call):
                    self.assertFalse(isinstance(node.func, ast.Name) and node.func.id in ("print", "input"), path.name)
                    self.assertFalse(isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                                     and node.func.value.id == "sys" and node.func.attr == "exit", path.name)
