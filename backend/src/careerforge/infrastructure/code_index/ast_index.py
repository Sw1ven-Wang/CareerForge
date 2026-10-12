import ast
from typing import Any


def symbols(text: str) -> dict[str, Any]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {
            "status": "parse_failed",
            "symbols": [],
            "imports": [],
            "limitations": ["词法降级；未执行代码"],
        }
    found = []
    imports = []

    def visit(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = prefix + child.name
                found.append(
                    {
                        "name": child.name,
                        "qualified_name": qualified,
                        "kind": type(child).__name__,
                        "start_line": child.lineno,
                        "end_line": child.end_lineno or child.lineno,
                        "signature": ast.unparse(child.args)
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        else None,
                        "docstring": ast.get_docstring(child),
                        "decorators": [ast.unparse(d) for d in child.decorator_list],
                    }
                )
                visit(child, qualified + ".")
            else:
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    imports.append(ast.unparse(child))
                visit(child, prefix)

    visit(tree)
    return {
        "status": "parsed",
        "symbols": found,
        "imports": imports,
        "limitations": ["AST 定义/引用不证明动态调用关系；未执行代码"],
    }
