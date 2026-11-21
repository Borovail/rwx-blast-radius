"""Use an executed guard line when a patch changes only its condition."""

import ast


def guard_tests(old, new, removed, insertions, record):
    if len(removed) != 1 or insertions:
        return None
    line = next(iter(removed))
    if line in record["startup"] or not record["lines"].get(line):
        return None

    def find(tree):
        matches = [node for node in ast.walk(tree) if isinstance(node, (ast.If, ast.While))
                   and node.lineno == node.test.end_lineno == line]
        return matches[0] if len(matches) == 1 else None

    left, right = find(old), find(new)
    if left is None or right is None or type(left) is not type(right):
        return None
    unsafe = (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await, ast.Lambda,
              ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)
    if any(isinstance(child, unsafe) for test in (left.test, right.test) for child in ast.walk(test)):
        return None
    if not any(isinstance(child, ast.Name) for child in ast.walk(left.test)):
        return None
    if ast.dump(left.test) == ast.dump(right.test):
        return None
    # Validate all other statements, targets and scope declarations. The new
    # AST belongs to this single patch; the cached original stays untouched.
    saved = right.test
    try:
        right.test = left.test
        if ast.dump(old) != ast.dump(new):
            return None
    finally:
        right.test = saved
    return record["lines"][line]
