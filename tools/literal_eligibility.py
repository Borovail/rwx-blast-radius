"""Source-only eligibility bound for the scalar-observation prototype."""
import ast
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from m2_dispatch import BASE_COMMIT, declarations

def scalar_declarations(tree, path):
    result = {}
    for key, node in declarations(tree, path).items():
        name = key.rsplit("|", 1)[-1]
        if name.startswith("_") or not name.isupper():
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError):
            continue
        if type(value) in (bool, int, str):
            result[key] = node
    return result

POLICY = "audited-concrete-sqlglot-dialect-scalars-v1"
ROOT_CLASS = "sqlglot.dialects.dialect.Dialect"


def resolve(node, environment):
    if isinstance(node, ast.Name):
        return environment.get(node.id)
    if isinstance(node, ast.Attribute):
        prefix = resolve(node.value, environment)
        return prefix + "." + node.attr if prefix else None
    return None


def audit(repo, metadata):
    if metadata["base_commit"] != BASE_COMMIT:
        raise ValueError("wrong baseline")
    sources = {}
    for path, expected in metadata["files"].items():
        raw = (repo / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("baseline changed: " + path)
        if path.endswith(".py") and path.startswith(("sqlglot/", "tests/")):
            sources[path] = ast.parse(raw.decode(), path)
    environments, classes = {}, {}
    for path, tree in sources.items():
        module = path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[:-9]
        environment = {}
        for node in tree.body:
            if isinstance(node, ast.Import):
                for item in node.names:
                    environment[item.asname or item.name.split(".")[0]] = item.name if item.asname else item.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for item in node.names:
                    value = node.module + "." + item.name
                    if value == "sqlglot.Dialect":
                        value = ROOT_CLASS
                    environment[item.asname or item.name] = value
            elif isinstance(node, ast.ClassDef):
                name = module + "." + node.name
                environment[node.name] = name
                classes[name] = (path, node)
        environments[path] = environment

    root_path, root = classes[ROOT_CLASS]
    if (root.decorator_list or root.bases or len(root.keywords) != 1
            or root.keywords[0].arg != "metaclass" or not isinstance(root.keywords[0].value, ast.Name)
            or root.keywords[0].value.id != "_Dialect"):
        raise ValueError("unexpected Dialect creation protocol")
    _, meta = classes["sqlglot.dialects.dialect._Dialect"]
    protocol_names = {node.name for node in meta.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if protocol_names & {"__prepare__", "__init__", "__getattribute__", "__getattr__"}:
        raise ValueError("unaudited metaclass protocol")
    creator = next(node for node in meta.body if isinstance(node, ast.FunctionDef) and node.name == "__new__")
    expected = ast.parse("klass = super().__new__(cls, clsname, bases, attrs)").body[0]
    if ast.dump(creator.body[0]) != ast.dump(expected):
        raise ValueError("metaclass no longer forwards attrs directly to type.__new__")
    if sum(isinstance(node, ast.Name) and node.id == "attrs" and isinstance(node.ctx, ast.Load)
           for node in ast.walk(creator)) != 1:
        raise ValueError("metaclass has additional raw attrs consumers")
    forbidden = {"__init_subclass__", "__getattribute__", "__getattr__", "__prepare__", "__mro_entries__"}
    if any(isinstance(node, ast.FunctionDef) and node.name in forbidden for node in root.body):
        raise ValueError("unaudited Dialect protocol")

    chains, rejected = {ROOT_CLASS: [ROOT_CLASS]}, {}
    pending = dict(classes)
    changed = True
    while changed:
        changed = False
        for name, (path, node) in list(pending.items()):
            if name == ROOT_CLASS:
                pending.pop(name)
                continue
            bases = [resolve(base, environments[path]) for base in node.bases]
            if not any(base in chains for base in bases):
                continue
            if (len(bases) != 1 or bases[0] not in chains or node.decorator_list or node.keywords
                    or any(isinstance(child, ast.FunctionDef) and child.name in forbidden for child in node.body)):
                rejected[name] = "custom base, decorator, metaclass, or class initialization protocol"
            else:
                chains[name] = [name, *chains[bases[0]]]
            pending.pop(name)
            changed = True
    eligible_classes = {name for name in chains if name != ROOT_CLASS
                        and classes[name][0].startswith("sqlglot/dialects/")}

    # A concrete SQLGlot dialect's descendants must retain the same audited
    # protocol, including local classes in tests. Reject the whole upstream
    # chain if an explicit custom protocol or dynamic type construction appears.
    tainted, exposure = set(), []
    for path, tree in sources.items():
        env = environments[path]
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = [resolve(base, env) for base in node.bases]
                relevant = [base for base in bases if base in eligible_classes]
                if relevant and (len(bases) != 1 or node.keywords or node.decorator_list
                                 or any(isinstance(child, ast.FunctionDef) and child.name in forbidden for child in node.body)):
                    for base in relevant:
                        tainted.update(chains[base])
                    exposure.append({"path": path, "line": node.lineno, "kind": "custom descendant protocol"})
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "type" and len(node.args) == 3:
                referenced = {resolve(part, env) for part in ast.walk(node.args[1])}
                relevant = referenced & eligible_classes
                if relevant:
                    for base in relevant:
                        tainted.update(chains[base])
                    exposure.append({"path": path, "line": node.lineno, "kind": "dynamic dialect type"})
    eligible_classes -= tainted

    keys = {}
    for name in sorted(eligible_classes):
        path, node = classes[name]
        for key, statement in scalar_declarations(sources[path], path).items():
            if key.split("|")[1] == node.name:
                keys[key] = {"class": name, "mro_chain": chains[name],
                             "scalar_type": type(ast.literal_eval(statement.value)).__name__}
    reflective = []
    for path, tree in sources.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = resolve(node.func, environments[path])
                if name in {"operator.attrgetter", "operator.itemgetter", "inspect.getmembers", "inspect.getmembers_static"}:
                    reflective.append({"path": path, "line": node.lineno, "consumer": name})
    return {"base_commit": BASE_COMMIT, "eligibility_policy": POLICY,
            "source_files": len(sources), "eligible_classes": sorted(eligible_classes),
            "eligible_declarations": keys, "rejected_protocols": rejected,
            "custom_concrete_descendant_exposures": exposure, "reflective_consumers": reflective,
            "metaclass_path_sha256": metadata["files"][root_path],
            "limitations": ["Base Dialect and arbitrary external classes excluded.",
                            "Module expression-class enumeration in helper.subclasses does not inspect dialect setting values.",
                            "Runtime must retain generic blocked fallback and require an isolated same-type scalar edit."]}
