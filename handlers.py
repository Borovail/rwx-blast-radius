"""Attribute changes to callbacks in declarative Python tables to their callers."""

import ast


def _table_values(tree):
    values = set()

    def collect(value):
        if isinstance(value, ast.Dict):
            for item in value.values:
                collect(item)
        elif isinstance(value, (ast.List, ast.Tuple)):
            for item in value.elts:
                collect(item)
        elif isinstance(value, (ast.Lambda, ast.Call)):
            values.add(value)

    def visit(body):
        for stmt in body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                collect(stmt.value)
            elif isinstance(stmt, ast.ClassDef):
                visit(stmt.body)

    visit(tree.body)
    return values


def _changes(left, right, tables, changes):
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(_changes(a, b, tables, changes) for a, b in zip(left, right))
    if not isinstance(left, ast.AST):
        return repr(left) == repr(right)
    if left in tables:
        if ast.dump(left) != ast.dump(right):
            changes.append((left, right))
        return True
    return all(_changes(a, b, tables, changes) for (_, a), (_, b) in zip(ast.iter_fields(left), ast.iter_fields(right)))


def _factory_handler(tree, name):
    definitions = [s for s in tree.body if isinstance(s, ast.FunctionDef) and s.name == name]
    if len(definitions) != 1 or definitions[0].decorator_list:
        return None
    body = definitions[0].body
    if ast.get_docstring(definitions[0]) is not None:
        body = body[1:]
    if len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Lambda):
        callback = body[0].value
    elif (len(body) == 2 and isinstance(body[0], ast.FunctionDef) and not body[0].decorator_list
          and isinstance(body[1], ast.Return) and isinstance(body[1].value, ast.Name)
          and body[1].value.id == body[0].name):
        callback = body[0]
    else:
        return None
    if callback.args.defaults or any(callback.args.kw_defaults):
        return None
    annotations = [arg.annotation for arg in ast.walk(callback.args) if isinstance(arg, ast.arg)]
    if isinstance(callback, ast.FunctionDef):
        annotations.append(callback.returns)
    if any(annotations) and not any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__"
        and any(a.name == "annotations" for a in node.names) for node in tree.body
    ):
        return None
    return callback


def _factory_source(call, other, tree, record, get_record):
    if not isinstance(call.func, ast.Name) or ast.dump(call.func) != ast.dump(other.func):
        return None
    args = [*call.args, *(k.value for k in call.keywords), *other.args, *(k.value for k in other.keywords)]
    if (len(call.args) != len(other.args) or not all(isinstance(a, ast.Constant) for a in args)
            or [k.arg for k in call.keywords] != [k.arg for k in other.keywords]
            or any(k.arg is None for k in call.keywords)):
        return None
    name = call.func.id
    imports = [
        (s.module, a.name) for s in tree.body if isinstance(s, ast.ImportFrom) and not s.level
        for a in s.names if (a.asname or a.name) == name
    ]
    if len(imports) == 1:
        module, name = imports[0]
        if not module or not module.startswith("sqlglot."):
            return None
        record = get_record(module.replace(".", "/") + ".py")
        tree = ast.parse(record["source"])
    elif imports:
        return None
    callback = _factory_handler(tree, name)
    return (record, callback) if callback is not None else None


def callback_tests(old, new, record, get_record):
    """Return caller coverage only if all changes are to deferred table values."""
    changes = []
    if not _changes(old, new, _table_values(old), changes) or not changes:
        return None
    selected = set()
    for before, after in changes:
        if isinstance(before, ast.Lambda):
            if ast.dump(before.args) != ast.dump(after.args):
                return None
            evidence, callback = record, before
        else:
            factory = _factory_source(before, after, old, record, get_record)
            if factory is None:
                return None
            evidence, callback = factory
        body = callback.body if isinstance(callback, ast.Lambda) else callback.body[0]
        end = callback.body.end_lineno if isinstance(callback, ast.Lambda) else callback.body[-1].end_lineno
        lines = range(body.lineno, end + 1)
        # Definition-line coverage cannot distinguish startup calls from
        # construction of a one-line lambda. Keep the full fallback there.
        shared = evidence["startup"].intersection(lines)
        if (isinstance(callback, ast.Lambda) and isinstance(callback.body, ast.Call)
                and any(a.lineno > callback.lineno for a in [
                    *callback.body.args, *(k.value for k in callback.body.keywords)
                ])):
            shared.discard(callback.lineno)
        if shared:
            return None
        consumers = {n for line in lines for n in evidence["lines"].get(line, ())}
        if not consumers:
            return None
        selected.update(consumers)
    return selected
