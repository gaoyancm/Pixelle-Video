from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_ROOT = PROJECT_ROOT / "pixelle_video" / "media_migration"
MIGRATED_ENTRY_FILES = (PROJECT_ROOT / "api" / "routers" / "media_jobs.py",)
PRODUCTION_SCAN_ROOTS = (
    PROJECT_ROOT / "api",
    PROJECT_ROOT / "pixelle_video",
    PROJECT_ROOT / "web",
)

FORBIDDEN_REFERENCES = {
    "AssetRepository",
    "ComfyKit",
    "ComfyUIAdapter",
    "HistoryManager",
    "MediaJobRepository",
    "PixelleVideoCore",
    "TaskManager",
    "create_task",
    "task_manager",
    "create_job",
    "create_job_with_assets",
    "file_path",
    "history_manager",
    "image_path",
    "output_path",
    "provider",
    "video_path",
    "write_history",
}

FACADE_TARGETS = {
    "pixelle_video.media_migration.CompatibilitySubmissionFacade",
    "pixelle_video.media_migration.facade.CompatibilitySubmissionFacade",
}

TASK_MANAGER_CALLS = {
    "cancel_task",
    "create_task",
    "execute_task",
    "get_task",
    "list_tasks",
}

PROVIDER_SUBMISSION_CALLS = {
    "execute",
    "generate",
    "generate_image",
    "generate_video",
    "run",
    "submit",
    "submit_prepared",
}

ADAPTER_SUBMISSION_CALLS = {
    "execute",
    "run",
    "submit",
    "submit_prepared",
}

DANGEROUS_DYNAMIC_METHODS = (
    TASK_MANAGER_CALLS
    | PROVIDER_SUBMISSION_CALLS
    | ADAPTER_SUBMISSION_CALLS
    | {"generate_video", "media", "write_history"}
)


@dataclass(frozen=True, order=True)
class FacadeSite:
    file: str
    owner: str
    occurrence: int = 1


@dataclass(frozen=True, order=True)
class BypassSite:
    file: str
    owner: str
    call_type: str
    callee: str
    occurrence: int = 1


@dataclass(frozen=True)
class FunctionSignature:
    positional: tuple[str, ...]
    keyword_only: tuple[str, ...]
    vararg: str | None
    kwarg: str | None
    defaults: Mapping[str, ast.AST]

    @property
    def named(self) -> tuple[str, ...]:
        return (*self.positional, *self.keyword_only)


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def import_bindings(tree: ast.AST) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                bindings[local] = alias.name if alias.asname else local
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bindings


def resolve_bound_name(node: ast.AST, bindings: Mapping[str, str]) -> str | None:
    dotted = dotted_name(node)
    if dotted is None:
        return None
    first, separator, remainder = dotted.partition(".")
    canonical = bindings.get(first, first)
    return f"{canonical}.{remainder}" if separator else canonical


def assignment_bindings(tree: ast.AST, imports: Mapping[str, str]) -> dict[str, str]:
    bindings = dict(imports)
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
    ]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            for target, value in _assignment_value_pairs(node):
                resolved = resolve_bound_name(value, bindings)
                if resolved is None:
                    continue
                if not isinstance(target, ast.Name):
                    continue
                previous = bindings.get(target.id)
                if previous is None:
                    bindings[target.id] = resolved
                    changed = True
                elif previous == resolved:
                    continue
        if not changed:
            break
    return bindings


def _assignment_targets(node: ast.Assign | ast.AnnAssign) -> list[ast.AST]:
    return node.targets if isinstance(node, ast.Assign) else [node.target]


def _target_value_pairs(target: ast.AST, value: ast.AST) -> list[tuple[ast.AST, ast.AST]]:
    if isinstance(target, (ast.Tuple, ast.List)):
        if not isinstance(value, (ast.Tuple, ast.List)):
            return []
        if len(target.elts) != len(value.elts):
            return []
        pairs: list[tuple[ast.AST, ast.AST]] = []
        for child_target, child_value in zip(target.elts, value.elts, strict=True):
            pairs.extend(_target_value_pairs(child_target, child_value))
        return pairs
    return [(target, value)]


def _assignment_value_pairs(
    node: ast.Assign | ast.AnnAssign,
) -> list[tuple[ast.AST, ast.AST]]:
    if node.value is None:
        return []
    pairs: list[tuple[ast.AST, ast.AST]] = []
    for target in _assignment_targets(node):
        pairs.extend(_target_value_pairs(target, node.value))
    return pairs


def _references_canonical_target(
    node: ast.AST,
    bindings: Mapping[str, str],
    targets: set[str],
) -> bool:
    return any(
        resolve_bound_name(child, bindings) in targets
        for child in ast.walk(node)
        if isinstance(child, (ast.Name, ast.Attribute))
    )


def _constant_getattr_call(node: ast.AST) -> tuple[ast.AST, str] | None:
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Name) or node.func.id != "getattr":
        return None
    if len(node.args) < 2:
        return None
    attribute = node.args[1]
    if not isinstance(attribute, ast.Constant) or not isinstance(attribute.value, str):
        return None
    return node.args[0], attribute.value


def _function_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> FunctionSignature:
    positional_arguments = [*node.args.posonlyargs, *node.args.args]
    defaults: dict[str, ast.AST] = {}
    if node.args.defaults:
        default_arguments = positional_arguments[-len(node.args.defaults) :]
        defaults.update(
            {
                argument.arg: default
                for argument, default in zip(
                    default_arguments,
                    node.args.defaults,
                    strict=True,
                )
            }
        )
    defaults.update(
        {
            argument.arg: default
            for argument, default in zip(
                node.args.kwonlyargs,
                node.args.kw_defaults,
                strict=True,
            )
            if default is not None
        }
    )
    return FunctionSignature(
        positional=tuple(argument.arg for argument in positional_arguments),
        keyword_only=tuple(argument.arg for argument in node.args.kwonlyargs),
        vararg=node.args.vararg.arg if node.args.vararg else None,
        kwarg=node.args.kwarg.arg if node.args.kwarg else None,
        defaults=defaults,
    )


def _bind_call_arguments(
    call: ast.Call,
    signature: FunctionSignature,
    controlled: Callable[[ast.AST], bool],
    context: str,
) -> dict[str, ast.AST]:
    bound: dict[str, ast.AST] = {}
    for index, argument in enumerate(call.args):
        if isinstance(argument, ast.Starred):
            if controlled(argument.value):
                raise AssertionError(
                    f"controlled capability uses unresolved *args in {context}"
                )
            continue
        if index < len(signature.positional):
            bound[signature.positional[index]] = argument
        elif controlled(argument):
            raise AssertionError(
                f"controlled capability enters unresolved varargs in {context}"
            )

    named = set(signature.named)
    for keyword in call.keywords:
        if keyword.arg is None:
            if controlled(keyword.value):
                raise AssertionError(
                    f"controlled capability uses unresolved **kwargs in {context}"
                )
            continue
        if keyword.arg in named:
            bound[keyword.arg] = keyword.value
        elif controlled(keyword.value):
            raise AssertionError(
                f"controlled capability targets unresolved keyword in {context}"
            )

    for parameter, default in signature.defaults.items():
        bound.setdefault(parameter, default)
    return bound


class _OwnedCallVisitor(ast.NodeVisitor):
    def __init__(self):
        self.owners: list[str] = []
        self.calls: list[tuple[str, ast.Call]] = []
        self.assignments: list[tuple[str, ast.Assign | ast.AnnAssign]] = []

    def _visit_owned(self, name: str, node: ast.AST) -> None:
        self.owners.append(name)
        self.generic_visit(node)
        self.owners.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_owned(node.name, node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_owned(node.name, node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_owned(node.name, node)

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append((".".join(self.owners) or "<module>", node))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.assignments.append((".".join(self.owners) or "<module>", node))
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.assignments.append((".".join(self.owners) or "<module>", node))
        self.generic_visit(node)


def _facade_parameter_call_nodes(
    tree: ast.Module,
    bindings: Mapping[str, str],
    file: str,
) -> set[ast.Call]:
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    helper_aliases = _module_helper_aliases(tree, functions)
    found: set[ast.Call] = set()

    def analyze(
        function_name: str,
        initial: frozenset[str],
        active: tuple[tuple[str, frozenset[str]], ...],
    ) -> None:
        state = (function_name, initial)
        if state in active:
            raise AssertionError(
                f"Facade capability participates in helper cycle at {function_name}"
            )
        function = functions[function_name]
        signature = _function_signature(function)
        facade_names = set(initial)
        local_helper_aliases = _function_helper_aliases(function, helper_aliases)

        def is_facade(expression: ast.AST) -> bool:
            return (
                isinstance(expression, ast.Name)
                and expression.id in facade_names
            ) or resolve_bound_name(expression, bindings) in FACADE_TARGETS

        def mentions_facade(expression: ast.AST) -> bool:
            return any(
                (
                    isinstance(part, ast.Name)
                    and part.id in facade_names
                )
                or resolve_bound_name(part, bindings) in FACADE_TARGETS
                for part in ast.walk(expression)
                if isinstance(part, (ast.Name, ast.Attribute))
            )

        for parameter, default in signature.defaults.items():
            if is_facade(default):
                facade_names.add(parameter)
            elif mentions_facade(default):
                raise AssertionError(
                    f"Facade default capability is unresolved in {file}::{function_name}"
                )

        assignments = [
            child
            for child in ast.walk(function)
            if isinstance(child, (ast.Assign, ast.AnnAssign))
            and child.value is not None
        ]
        for _ in range(len(assignments) + 1):
            changed = False
            for assignment in assignments:
                for target, value in _assignment_value_pairs(assignment):
                    if not is_facade(value):
                        if isinstance(value, ast.Call) and is_facade(value.func):
                            continue
                        if mentions_facade(value):
                            raise AssertionError(
                                f"Facade capability transfer is unresolved in "
                                f"{file}::{function_name}"
                            )
                        continue
                    if not isinstance(target, ast.Name):
                        raise AssertionError(
                            f"Facade capability is stored dynamically in "
                            f"{file}::{function_name}"
                        )
                    if target.id not in facade_names:
                        facade_names.add(target.id)
                        changed = True
            if not changed:
                break

        for call in (
            child for child in ast.walk(function) if isinstance(child, ast.Call)
        ):
            if is_facade(call.func):
                found.add(call)
                continue
            if mentions_facade(call.func):
                raise AssertionError(
                    f"dynamic Facade capability is called in {file}::{function_name}"
                )
            helper_name = (
                local_helper_aliases.get(call.func.id)
                if isinstance(call.func, ast.Name)
                else None
            )
            if helper_name is None:
                if _call_arguments_contain(call, mentions_facade):
                    raise AssertionError(
                        f"Facade capability enters unresolved callee in "
                        f"{file}::{function_name}"
                    )
                continue
            callee = functions[helper_name]
            argument_bindings = _bind_call_arguments(
                call,
                _function_signature(callee),
                mentions_facade,
                f"{file}::{function_name}->{helper_name}",
            )
            propagated: set[str] = set()
            for parameter, argument in argument_bindings.items():
                if is_facade(argument):
                    propagated.add(parameter)
                elif mentions_facade(argument):
                    raise AssertionError(
                        f"Facade helper argument is unresolved in "
                        f"{file}::{function_name}->{helper_name}"
                    )
            if propagated:
                analyze(
                    helper_name,
                    frozenset(propagated),
                    (*active, state),
                )

    for name in functions:
        analyze(name, frozenset(), ())
    return found


def facade_instantiation_sites(source: str, file: str) -> set[FacadeSite]:
    tree = ast.parse(source)
    imports = import_bindings(tree)
    bindings = assignment_bindings(tree, imports)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        pairs = _assignment_value_pairs(node)
        facade_pairs = [
            (target, value)
            for target, value in pairs
            if resolve_bound_name(value, bindings) in FACADE_TARGETS
        ]
        if any(not isinstance(target, ast.Name) for target, _ in facade_pairs):
            raise AssertionError(f"Facade capability is stored dynamically in {file}")
        if (
            _references_canonical_target(node.value, bindings, FACADE_TARGETS)
            and not facade_pairs
            and not (
                isinstance(node.value, ast.Call)
                and resolve_bound_name(node.value.func, bindings) in FACADE_TARGETS
            )
        ):
            raise AssertionError(f"Facade capability transfer is unresolved in {file}")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.NamedExpr)
            and _references_canonical_target(node.value, bindings, FACADE_TARGETS)
        ):
            raise AssertionError(f"Facade capability uses a named expression in {file}")

    visitor = _OwnedCallVisitor()
    visitor.visit(tree)
    parameter_calls = _facade_parameter_call_nodes(tree, bindings, file)
    sites: set[FacadeSite] = set()
    occurrences: dict[str, int] = {}
    for owner, call in visitor.calls:
        resolved = resolve_bound_name(call.func, bindings)
        if resolved in FACADE_TARGETS or call in parameter_calls:
            occurrences[owner] = occurrences.get(owner, 0) + 1
            sites.add(FacadeSite(file, owner, occurrences[owner]))
            continue
        if (
            not isinstance(call.func, (ast.Name, ast.Attribute))
            and _references_canonical_target(call.func, bindings, FACADE_TARGETS)
        ):
            raise AssertionError(
                f"dynamic Facade capability is called in {file}::{owner}"
            )
        dynamic = _constant_getattr_call(call.func)
        if dynamic and dynamic[1] == "CompatibilitySubmissionFacade":
            raise AssertionError(
                f"dynamic Facade lookup is called in {file}::{owner}"
            )
    return sites


def production_facade_instantiation_sites() -> set[FacadeSite]:
    sites: set[FacadeSite] = set()
    for root in PRODUCTION_SCAN_ROOTS:
        for path in root.rglob("*.py"):
            relative = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
            sites |= facade_instantiation_sites(path.read_text(encoding="utf-8"), relative)
    return sites


def assert_single_facade_site(sources: Mapping[str, str], expected: FacadeSite) -> None:
    actual: set[FacadeSite] = set()
    for file, source in sources.items():
        actual |= facade_instantiation_sites(source, file)
    assert actual == {expected}


def _function_parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return list(_function_signature(node).named)


def _annotation_name(argument: ast.arg) -> str:
    return dotted_name(argument.annotation) or ""


def _service_aliases(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    initial: set[str],
) -> set[str]:
    aliases = set(initial)
    changed = True
    while changed:
        changed = False
        for child in ast.walk(node):
            if not isinstance(child, (ast.Assign, ast.AnnAssign)):
                continue
            value = child.value
            if not isinstance(value, ast.Name) or value.id not in aliases:
                continue
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
    return aliases


def _module_helper_aliases(
    tree: ast.Module,
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> dict[str, str]:
    aliases = {name: name for name in functions}
    assignments = [
        node
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
    ]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            if not isinstance(node.value, ast.Name):
                continue
            helper = aliases.get(node.value.id)
            if helper is None:
                continue
            for target in _assignment_targets(node):
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases[target.id] = helper
                    changed = True
        if not changed:
            break
    return aliases


def _function_helper_aliases(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    module_helpers: Mapping[str, str],
) -> dict[str, str]:
    aliases = dict(module_helpers)
    assignments = [
        child
        for child in ast.walk(node)
        if isinstance(child, (ast.Assign, ast.AnnAssign))
        and child.value is not None
    ]
    for _ in range(len(assignments) + 1):
        changed = False
        for assignment in assignments:
            for target, value in _assignment_value_pairs(assignment):
                if not isinstance(target, ast.Name) or not isinstance(value, ast.Name):
                    continue
                helper = aliases.get(value.id)
                if helper is not None and target.id not in aliases:
                    aliases[target.id] = helper
                    changed = True
        if not changed:
            break
    return aliases


def _call_arguments_contain(
    call: ast.Call,
    controlled: Callable[[ast.AST], bool],
) -> bool:
    return any(controlled(argument) for argument in call.args) or any(
        controlled(keyword.value) for keyword in call.keywords
    )


def _function_call_aliases(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    service_names: set[str],
    module_helpers: Mapping[str, str],
    initial_method_aliases: set[str] | None = None,
) -> tuple[dict[str, str], set[str], set[str]]:
    helper_aliases = _function_helper_aliases(node, module_helpers)
    method_aliases = set(initial_method_aliases or ())
    unresolved_submission_aliases: set[str] = set()
    assignments = [
        child
        for child in ast.walk(node)
        if isinstance(child, (ast.Assign, ast.AnnAssign)) and child.value is not None
    ]

    for _ in range(len(assignments) + 1):
        changed = False
        for child in assignments:
            value = child.value
            pairs = _assignment_value_pairs(child)
            targets = [target for target, _ in pairs if isinstance(target, ast.Name)]
            if not targets:
                continue

            for target, pair_value in pairs:
                if not isinstance(target, ast.Name):
                    continue
                helper = (
                    helper_aliases.get(pair_value.id)
                    if isinstance(pair_value, ast.Name)
                    else None
                )
                is_method = (
                    isinstance(pair_value, ast.Attribute)
                    and pair_value.attr == "create"
                    and isinstance(pair_value.value, ast.Name)
                    and pair_value.value.id in service_names
                ) or (
                    isinstance(pair_value, ast.Name)
                    and pair_value.id in method_aliases
                )
                dynamic = _constant_getattr_call(pair_value)
                if (
                    dynamic
                    and dynamic[1] == "create"
                    and isinstance(dynamic[0], ast.Name)
                    and dynamic[0].id in service_names
                ):
                    is_method = True
                if helper is not None and target.id not in helper_aliases:
                    helper_aliases[target.id] = helper
                    changed = True
                if is_method and target.id not in method_aliases:
                    method_aliases.add(target.id)
                    changed = True

            if isinstance(value, (ast.Name, ast.Attribute)):
                continue
            mentions_submission_capability = any(
                (
                    isinstance(part, ast.Name)
                    and (
                        part.id in service_names
                        or part.id in method_aliases
                        or part.id in helper_aliases
                    )
                )
                or (
                    isinstance(part, ast.Attribute)
                    and part.attr == "create"
                    and isinstance(part.value, ast.Name)
                    and part.value.id in service_names
                )
                for part in ast.walk(value)
            )
            if mentions_submission_capability:
                unresolved_submission_aliases.update(target.id for target in targets)
        if not changed:
            break

    return helper_aliases, method_aliases, unresolved_submission_aliases


def application_service_submission_count(source: str, entry: str) -> int:
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert entry in functions
    module_helpers = _module_helper_aliases(tree, functions)
    entry_node = functions[entry]
    entry_services = {
        argument.arg
        for argument in [
            *entry_node.args.posonlyargs,
            *entry_node.args.args,
            *entry_node.args.kwonlyargs,
        ]
        if argument.arg == "service"
        or _annotation_name(argument).endswith(
            ("MediaJobServiceDep", "MediaJobApplicationService")
        )
    }
    def analyze(
        function_name: str,
        bound_names: frozenset[str],
        bound_callables: frozenset[str],
        active: tuple[tuple[str, frozenset[str], frozenset[str]], ...],
    ) -> int:
        state = (function_name, bound_names, bound_callables)
        if state in active:
            raise AssertionError(
                f"submission capability participates in helper cycle at {function_name}"
            )
        function = functions[function_name]
        service_names = _service_aliases(function, set(bound_names))
        helper_aliases, method_aliases, unresolved_aliases = _function_call_aliases(
            function,
            service_names,
            module_helpers,
            set(bound_callables),
        )
        submissions = 0

        def is_submission_callable(argument: ast.AST) -> bool:
            if isinstance(argument, ast.Name):
                return argument.id in method_aliases
            if (
                isinstance(argument, ast.Attribute)
                and argument.attr == "create"
                and isinstance(argument.value, ast.Name)
                and argument.value.id in service_names
            ):
                return True
            dynamic_argument = _constant_getattr_call(argument)
            if (
                dynamic_argument
                and dynamic_argument[1] == "create"
                and isinstance(dynamic_argument[0], ast.Name)
                and dynamic_argument[0].id in service_names
            ):
                return True
            if (
                isinstance(argument, ast.Call)
                and isinstance(argument.func, ast.Name)
                and argument.func.id == "getattr"
                and argument.args
                and isinstance(argument.args[0], ast.Name)
                and argument.args[0].id in service_names
            ):
                raise AssertionError(
                    f"reachable helper {function_name} passes an unresolved "
                    "dynamic service method"
                )
            return False

        def mentions_submission_capability(argument: ast.AST) -> bool:
            return any(
                (
                    isinstance(part, ast.Name)
                    and (
                        part.id in service_names
                        or part.id in method_aliases
                        or part.id in unresolved_aliases
                    )
                )
                or (
                    isinstance(part, ast.Attribute)
                    and part.attr == "create"
                    and isinstance(part.value, ast.Name)
                    and part.value.id in service_names
                )
                or (
                    isinstance(part, ast.Call)
                    and isinstance(part.func, ast.Name)
                    and part.func.id == "getattr"
                    and part.args
                    and isinstance(part.args[0], ast.Name)
                    and part.args[0].id in service_names
                )
                for part in ast.walk(argument)
            )

        if any(isinstance(child, ast.Try) for child in ast.walk(function)):
            raise AssertionError(f"reachable helper {function_name} contains try/except fallback")
        if any(isinstance(child, (ast.For, ast.AsyncFor, ast.While)) for child in ast.walk(function)):
            raise AssertionError(f"reachable helper {function_name} contains a retry-capable loop")

        for call in (child for child in ast.walk(function) if isinstance(child, ast.Call)):
            direct_service_create = (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "create"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id in service_names
            )
            dynamic = _constant_getattr_call(call.func)
            constant_dynamic_create = bool(
                dynamic
                and dynamic[1] == "create"
                and isinstance(dynamic[0], ast.Name)
                and dynamic[0].id in service_names
            )
            capability_getattr_constructor = bool(
                (constructed := _constant_getattr_call(call))
                and constructed[1] == "create"
                and isinstance(constructed[0], ast.Name)
                and constructed[0].id in service_names
            )
            if direct_service_create or constant_dynamic_create:
                submissions += 1
            elif isinstance(call.func, ast.Name) and call.func.id in method_aliases:
                submissions += 1
            elif isinstance(call.func, ast.Name) and call.func.id in unresolved_aliases:
                raise AssertionError(
                    f"reachable helper {function_name} calls an unresolved "
                    "submission-capable alias"
                )
            elif isinstance(call.func, ast.Call):
                inner_getattr = _constant_getattr_call(call.func)
                if (
                    isinstance(call.func.func, ast.Name)
                    and call.func.func.id == "getattr"
                    and len(call.func.args) >= 1
                    and isinstance(call.func.args[0], ast.Name)
                    and call.func.args[0].id in service_names
                    and inner_getattr is None
                ):
                    raise AssertionError(
                        f"reachable helper {function_name} dynamically selects "
                        "a service method"
                    )
                if any(
                    isinstance(part, ast.Name)
                    and (
                        part.id in service_names
                        or part.id in method_aliases
                        or part.id in helper_aliases
                    )
                    for part in ast.walk(call.func)
                ):
                    raise AssertionError(
                        f"reachable helper {function_name} uses an unresolved "
                        "dynamic submission call"
                    )

            helper_name = (
                helper_aliases.get(call.func.id)
                if isinstance(call.func, ast.Name)
                else None
            )
            if helper_name is None:
                if (
                    not direct_service_create
                    and not constant_dynamic_create
                    and not capability_getattr_constructor
                    and not (
                        isinstance(call.func, ast.Name)
                        and call.func.id in method_aliases
                    )
                    and _call_arguments_contain(
                        call,
                        mentions_submission_capability,
                    )
                ):
                    raise AssertionError(
                        f"reachable helper {function_name} passes submission "
                        "capability to an unresolved callee"
                    )
                continue
            callee = functions[helper_name]
            propagated_services: set[str] = set()
            propagated_callables: set[str] = set()
            argument_bindings = _bind_call_arguments(
                call,
                _function_signature(callee),
                mentions_submission_capability,
                f"{function_name}->{helper_name}",
            )
            for parameter, argument in argument_bindings.items():
                if isinstance(argument, ast.Name):
                    if argument.id in service_names:
                        propagated_services.add(parameter)
                    if is_submission_callable(argument):
                        propagated_callables.add(parameter)
                elif is_submission_callable(argument):
                    propagated_callables.add(parameter)
                elif mentions_submission_capability(argument):
                    raise AssertionError(
                        f"reachable helper {function_name} passes an unresolved "
                        "submission capability"
                    )
            if propagated_services or propagated_callables:
                submissions += analyze(
                    helper_name,
                    frozenset(propagated_services),
                    frozenset(propagated_callables),
                    (*active, state),
                )

        return submissions

    return analyze(entry, frozenset(entry_services), frozenset(), ())


def _capability_bindings(
    tree: ast.AST,
    bindings: Mapping[str, str],
) -> dict[str, str]:
    capabilities: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            annotation = resolve_bound_name(node.annotation, bindings) or ""
            if "Adapter" in annotation:
                capabilities[node.arg] = "adapter"
            elif "Provider" in annotation:
                capabilities[node.arg] = "provider"
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = [
            target.id
            for target in _assignment_targets(node)
            if isinstance(target, ast.Name)
        ]
        if not targets:
            continue
        resolved = resolve_bound_name(node.value, bindings) or ""
        capability = None
        if any(part.lower().endswith("adapter") for part in resolved.split(".")):
            capability = "adapter"
        elif any(part.lower().endswith("provider") for part in resolved.split(".")):
            capability = "provider"
        elif resolved.endswith(("_create_image_client", "_create_video_client")):
            capability = "provider"
        elif isinstance(node.value, ast.Call):
            factory = resolve_bound_name(node.value.func, bindings) or ""
            if "Adapter" in factory:
                capability = "adapter"
            elif "Provider" in factory or factory.endswith(
                ("._create_image_client", "._create_video_client")
            ):
                capability = "provider"
        if capability:
            capabilities.update({target: capability for target in targets})
    return capabilities


def _callee_capability(
    callee: str,
    capabilities: Mapping[str, str],
) -> str | None:
    parts = callee.split(".")
    root = parts[0]
    if root in capabilities:
        return capabilities[root]
    lowered = [part.lower() for part in parts[:-1]]
    if any(part.endswith("adapter") for part in lowered):
        return "adapter"
    if any(part.endswith("provider") for part in lowered):
        return "provider"
    return None


def _classify_bypass_call(
    callee: str,
    capabilities: Mapping[str, str] | None = None,
) -> str | None:
    capabilities = capabilities or {}
    leaf = callee.rsplit(".", 1)[-1]
    if ".task_manager." in f".{callee}" and leaf in TASK_MANAGER_CALLS:
        return "task_manager"
    if callee.endswith(".generate_video") and "pixelle_video" in callee:
        return "core_generate_video"
    if callee.endswith(".media") and "pixelle_video" in callee:
        return "media_service"
    if callee.endswith(".api_media"):
        return "api_media_service"
    if callee.endswith("._get_or_create_comfykit"):
        return "comfykit_access"
    if callee == "kit.execute":
        return "comfykit_execute"
    if callee.startswith("pixelle_video.history."):
        return "history"
    capability = _callee_capability(callee, capabilities)
    if capability == "provider" and leaf in PROVIDER_SUBMISSION_CALLS:
        return "provider_submission"
    if capability == "adapter" and leaf in ADAPTER_SUBMISSION_CALLS:
        return "adapter_submission"
    return None


def _is_controlled_bypass_object(
    callee: str,
    capabilities: Mapping[str, str],
) -> bool:
    return (
        ".task_manager." in f".{callee}."
        or callee.endswith("task_manager")
        or _callee_capability(f"{callee}.execute", capabilities) is not None
        or callee == "kit"
        or callee.startswith("pixelle_video.history")
    )


def _owned_method_aliases(
    owner: str,
    visitor: _OwnedCallVisitor,
    bindings: Mapping[str, str],
    capabilities: Mapping[str, str],
) -> tuple[dict[str, str], set[str]]:
    aliases: dict[str, str] = {}
    unresolved: set[str] = set()
    assignments = [
        node
        for assignment_owner, node in visitor.assignments
        if assignment_owner == "<module>"
        or assignment_owner == owner
        or owner.startswith(f"{assignment_owner}.")
    ]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            for target, value in _assignment_value_pairs(node):
                if not isinstance(target, ast.Name):
                    resolved_value = resolve_bound_name(value, bindings)
                    if (
                        resolved_value
                        and _classify_bypass_call(resolved_value, capabilities)
                    ):
                        raise AssertionError(
                            f"controlled bypass capability is stored dynamically in {owner}"
                        )
                    continue

                resolved_method = None
                if isinstance(value, ast.Name) and value.id in aliases:
                    resolved_method = aliases[value.id]
                elif isinstance(value, (ast.Name, ast.Attribute)):
                    candidate = resolve_bound_name(value, bindings)
                    if candidate and _classify_bypass_call(candidate, capabilities):
                        resolved_method = candidate
                else:
                    dynamic = _constant_getattr_call(value)
                    if dynamic:
                        resolved_object = resolve_bound_name(dynamic[0], bindings)
                        if resolved_object:
                            candidate = f"{resolved_object}.{dynamic[1]}"
                            if _classify_bypass_call(candidate, capabilities):
                                resolved_method = candidate
                    elif (
                        isinstance(value, ast.Call)
                        and isinstance(value.func, ast.Name)
                        and value.func.id == "getattr"
                        and value.args
                    ):
                        resolved_object = resolve_bound_name(value.args[0], bindings)
                        if resolved_object and _is_controlled_bypass_object(
                            resolved_object,
                            capabilities,
                        ):
                            if target.id not in unresolved:
                                unresolved.add(target.id)
                                changed = True

                if resolved_method and aliases.get(target.id) != resolved_method:
                    aliases[target.id] = resolved_method
                    changed = True
                if isinstance(value, ast.Name) and value.id in unresolved:
                    if target.id not in unresolved:
                        unresolved.add(target.id)
                        changed = True
        if not changed:
            break
    return aliases, unresolved


def _propagated_bypass_sites(
    file: str,
    tree: ast.Module,
    visitor: _OwnedCallVisitor,
    bindings: Mapping[str, str],
    capabilities: Mapping[str, str],
) -> set[BypassSite]:
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    helper_aliases = _module_helper_aliases(tree, functions)
    for alias, canonical in bindings.items():
        if canonical in functions:
            helper_aliases[alias] = canonical
    found: set[BypassSite] = set()
    occurrences: dict[tuple[str, str, str], int] = {}

    def analyze(
        function_name: str,
        initial_callables: Mapping[str, str],
        active: tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
    ) -> None:
        state = (function_name, tuple(sorted(initial_callables.items())))
        if state in active:
            raise AssertionError(
                f"bypass capability participates in helper cycle at "
                f"{file}::{function_name}"
            )
        function = functions[function_name]
        owner = function_name
        callable_types = dict(initial_callables)
        local_helper_aliases = _function_helper_aliases(function, helper_aliases)
        local_aliases, unresolved_aliases = _owned_method_aliases(
            owner,
            visitor,
            bindings,
            capabilities,
        )
        for name, callee in local_aliases.items():
            call_type = _classify_bypass_call(callee, capabilities)
            if call_type:
                callable_types.setdefault(name, call_type)

        def method_call_type(expression: ast.AST) -> str | None:
            if isinstance(expression, ast.Name):
                return callable_types.get(expression.id)
            if isinstance(expression, (ast.Name, ast.Attribute)):
                resolved = resolve_bound_name(expression, bindings)
                if resolved:
                    return _classify_bypass_call(resolved, capabilities)
            dynamic = _constant_getattr_call(expression)
            if dynamic:
                resolved_object = resolve_bound_name(dynamic[0], bindings)
                if resolved_object:
                    return _classify_bypass_call(
                        f"{resolved_object}.{dynamic[1]}",
                        capabilities,
                    )
            if (
                isinstance(expression, ast.Call)
                and isinstance(expression.func, ast.Name)
                and expression.func.id == "getattr"
                and expression.args
            ):
                resolved_object = resolve_bound_name(expression.args[0], bindings)
                if resolved_object and _is_controlled_bypass_object(
                    resolved_object,
                    capabilities,
                ):
                    raise AssertionError(
                        f"dynamic bypass capability is unresolved in "
                        f"{file}::{function_name}"
                    )
            return None

        def mentions_method_capability(expression: ast.AST) -> bool:
            if isinstance(expression, ast.Name):
                return (
                    expression.id in callable_types
                    or expression.id in unresolved_aliases
                )
            if method_call_type(expression):
                return True
            if isinstance(expression, ast.Attribute):
                return False
            if isinstance(expression, ast.Call):
                return any(
                    mentions_method_capability(argument)
                    for argument in expression.args
                ) or any(
                    mentions_method_capability(keyword.value)
                    for keyword in expression.keywords
                )
            return any(
                mentions_method_capability(child)
                for child in ast.iter_child_nodes(expression)
            )

        def is_controlled_invocation(expression: ast.AST) -> bool:
            while isinstance(expression, (ast.Await, ast.Expr)):
                expression = expression.value
            return isinstance(expression, ast.Call) and bool(
                method_call_type(expression.func)
            )

        signature = _function_signature(function)
        for parameter, default in signature.defaults.items():
            call_type = method_call_type(default)
            if call_type:
                callable_types[parameter] = call_type
            elif mentions_method_capability(default):
                raise AssertionError(
                    f"default bypass capability is unresolved in "
                    f"{file}::{function_name}"
                )

        assignments = [
            child
            for child in ast.walk(function)
            if isinstance(child, (ast.Assign, ast.AnnAssign))
            and child.value is not None
        ]
        for _ in range(len(assignments) + 1):
            changed = False
            for assignment in assignments:
                for target, value in _assignment_value_pairs(assignment):
                    call_type = method_call_type(value)
                    if call_type:
                        if not isinstance(target, ast.Name):
                            raise AssertionError(
                                f"bypass capability is stored dynamically in "
                                f"{file}::{function_name}"
                            )
                        if callable_types.get(target.id) != call_type:
                            callable_types[target.id] = call_type
                            changed = True
                    elif is_controlled_invocation(value):
                        continue
                    elif mentions_method_capability(value):
                        raise AssertionError(
                            f"bypass capability transfer is unresolved in "
                            f"{file}::{function_name}"
                        )
            if not changed:
                break

        for call in (
            child for child in ast.walk(function) if isinstance(child, ast.Call)
        ):
            if isinstance(call.func, ast.Name) and call.func.id in callable_types:
                call_type = callable_types[call.func.id]
                key = (owner, call_type, call.func.id)
                occurrences[key] = occurrences.get(key, 0) + 1
                found.add(
                    BypassSite(
                        file,
                        owner,
                        call_type,
                        call.func.id,
                        occurrences[key],
                    )
                )
                continue
            if (
                isinstance(call.func, ast.Name)
                and call.func.id in unresolved_aliases
            ):
                raise AssertionError(
                    f"dynamic bypass method alias is unresolved in {file}::{owner}"
                )
            helper_name = (
                local_helper_aliases.get(call.func.id)
                if isinstance(call.func, ast.Name)
                else None
            )
            if helper_name is None:
                if _call_arguments_contain(call, mentions_method_capability):
                    raise AssertionError(
                        f"bypass capability enters unresolved callee in "
                        f"{file}::{function_name}"
                    )
                continue
            callee = functions[helper_name]
            argument_bindings = _bind_call_arguments(
                call,
                _function_signature(callee),
                mentions_method_capability,
                f"{file}::{function_name}->{helper_name}",
            )
            propagated: dict[str, str] = {}
            for parameter, argument in argument_bindings.items():
                call_type = method_call_type(argument)
                if call_type:
                    propagated[parameter] = call_type
                elif mentions_method_capability(argument):
                    raise AssertionError(
                        f"helper bypass capability is unresolved in "
                        f"{file}::{function_name}->{helper_name}"
                    )
            if propagated:
                analyze(helper_name, propagated, (*active, state))

    for function_name in functions:
        analyze(function_name, {}, ())
    return found


def scan_bypass_sources(sources: Mapping[str, str]) -> set[BypassSite]:
    found: set[BypassSite] = set()
    for file, source in sources.items():
        tree = ast.parse(source)
        bindings = assignment_bindings(tree, import_bindings(tree))
        capabilities = _capability_bindings(tree, bindings)
        top_level_functions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        visitor = _OwnedCallVisitor()
        visitor.visit(tree)
        found |= _propagated_bypass_sites(
            file,
            tree,
            visitor,
            bindings,
            capabilities,
        )
        owner_flows = {
            owner: _owned_method_aliases(owner, visitor, bindings, capabilities)
            for owner, _ in visitor.calls
        }
        occurrences: dict[tuple[str, str, str], int] = {}
        for owner, call in visitor.calls:
            method_aliases, unresolved_aliases = owner_flows[owner]

            def mentions_owned_capability(expression: ast.AST) -> bool:
                if isinstance(expression, ast.Name):
                    return (
                        expression.id in method_aliases
                        or expression.id in unresolved_aliases
                    )
                resolved = resolve_bound_name(expression, bindings)
                if resolved and _classify_bypass_call(resolved, capabilities):
                    return True
                dynamic_expression = _constant_getattr_call(expression)
                if dynamic_expression:
                    resolved_object = resolve_bound_name(
                        dynamic_expression[0],
                        bindings,
                    )
                    if resolved_object and _classify_bypass_call(
                        f"{resolved_object}.{dynamic_expression[1]}",
                        capabilities,
                    ):
                        return True
                if isinstance(expression, ast.Call):
                    resolved_function = resolve_bound_name(
                        expression.func,
                        bindings,
                    )
                    if resolved_function and _classify_bypass_call(
                        resolved_function,
                        capabilities,
                    ):
                        return False
                    return _call_arguments_contain(
                        expression,
                        mentions_owned_capability,
                    )
                if isinstance(expression, ast.Attribute):
                    return False
                return any(
                    mentions_owned_capability(child)
                    for child in ast.iter_child_nodes(expression)
                )

            raw_callee = dotted_name(call.func)
            resolved_callee = resolve_bound_name(call.func, bindings)
            if isinstance(call.func, ast.Name) and call.func.id in method_aliases:
                resolved_callee = method_aliases[call.func.id]
            if isinstance(call.func, ast.Name) and call.func.id in unresolved_aliases:
                raise AssertionError(
                    f"dynamic bypass method alias is unresolved in {file}::{owner}"
                )
            dynamic = _constant_getattr_call(call.func)
            if dynamic:
                raw_object = dotted_name(dynamic[0])
                resolved_object = resolve_bound_name(dynamic[0], bindings)
                if raw_object and resolved_object:
                    raw_callee = f'getattr({raw_object},"{dynamic[1]}")'
                    resolved_callee = f"{resolved_object}.{dynamic[1]}"
            elif (
                isinstance(call.func, ast.Call)
                and isinstance(call.func.func, ast.Name)
                and call.func.func.id == "getattr"
                and call.func.args
            ):
                resolved_object = resolve_bound_name(call.func.args[0], bindings)
                if resolved_object and (
                    ".task_manager." in f".{resolved_object}."
                    or _callee_capability(resolved_object + ".execute", capabilities)
                ):
                    raise AssertionError(
                        f"dynamic bypass method is unresolved in {file}::{owner}"
                    )

            terminal_type = (
                _classify_bypass_call(resolved_callee, capabilities)
                if resolved_callee
                else None
            )
            is_execution_wrapper = resolved_callee in {
                "asyncio.to_thread",
                "loop.run_in_executor",
            }
            if (
                owner not in top_level_functions
                and terminal_type is None
                and not is_execution_wrapper
                and _call_arguments_contain(call, mentions_owned_capability)
            ):
                raise AssertionError(
                    f"bypass capability enters unresolved callee in {file}::{owner}"
                )

            call_candidates: list[tuple[str, str]] = []
            if raw_callee is not None and resolved_callee is not None:
                call_candidates.append((raw_callee, resolved_callee))
            if resolved_callee in {"asyncio.to_thread", "loop.run_in_executor"}:
                argument_index = 0 if resolved_callee == "asyncio.to_thread" else 1
                if len(call.args) > argument_index:
                    raw_argument = dotted_name(call.args[argument_index])
                    resolved_argument = resolve_bound_name(
                        call.args[argument_index],
                        bindings,
                    )
                    if raw_argument and resolved_argument:
                        call_candidates.append((raw_argument, resolved_argument))

            for raw_candidate, resolved_candidate in call_candidates:
                call_type = (
                    "task_manager_internal"
                    if file == "api/tasks/manager.py"
                    and raw_candidate == "asyncio.create_task"
                    else _classify_bypass_call(resolved_candidate, capabilities)
                )
                if not call_type:
                    continue
                key = (owner, call_type, raw_candidate)
                occurrences[key] = occurrences.get(key, 0) + 1
                found.add(
                    BypassSite(
                        file,
                        owner,
                        call_type,
                        raw_candidate,
                        occurrences[key],
                    )
                )
    return found


def production_bypass_sources() -> dict[str, str]:
    sources = {}
    for root in PRODUCTION_SCAN_ROOTS:
        for path in root.rglob("*.py"):
            relative = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
            sources[relative] = path.read_text(encoding="utf-8")
    return sources


def _allowed_sites(
    file: str,
    owner: str,
    call_type: str,
    callee: str,
    count: int,
    reason: str,
) -> dict[BypassSite, str]:
    return {
        BypassSite(file, owner, call_type, callee, occurrence): reason
        for occurrence in range(1, count + 1)
    }


LEGACY_BYPASS_ALLOWLIST: dict[BypassSite, str] = {
    **_allowed_sites(
        "api/routers/image.py",
        "image_generate",
        "media_service",
        "pixelle_video.media",
        1,
        "Legacy synchronous image API is deferred beyond 02-E2-A.",
    ),
    **_allowed_sites(
        "api/routers/tasks.py",
        "cancel_task",
        "task_manager",
        "task_manager.cancel_task",
        1,
        "Legacy task cancellation remains on the in-memory fact source.",
    ),
    **_allowed_sites(
        "api/routers/tasks.py",
        "get_task",
        "task_manager",
        "task_manager.get_task",
        1,
        "Legacy task lookup remains on the in-memory fact source.",
    ),
    **_allowed_sites(
        "api/routers/tasks.py",
        "list_tasks",
        "task_manager",
        "task_manager.list_tasks",
        1,
        "Legacy task listing remains on the in-memory fact source.",
    ),
    **_allowed_sites(
        "api/routers/video.py",
        "generate_video_async",
        "task_manager",
        "task_manager.create_task",
        1,
        "Legacy async video keeps its original task ID and state source.",
    ),
    **_allowed_sites(
        "api/routers/video.py",
        "generate_video_async",
        "task_manager",
        "task_manager.execute_task",
        1,
        "Legacy async video keeps its original background execution.",
    ),
    **_allowed_sites(
        "api/routers/video.py",
        "generate_video_async.execute_video_generation",
        "core_generate_video",
        "pixelle_video.generate_video",
        1,
        "Legacy async composite video is deferred to later orchestration.",
    ),
    **_allowed_sites(
        "api/routers/video.py",
        "generate_video_sync",
        "core_generate_video",
        "pixelle_video.generate_video",
        1,
        "Legacy synchronous composite response must remain unchanged.",
    ),
    **_allowed_sites(
        "api/tasks/manager.py",
        "TaskManager.start",
        "task_manager_internal",
        "asyncio.create_task",
        1,
        "Legacy TaskManager cleanup scheduling remains internal.",
    ),
    **_allowed_sites(
        "api/tasks/manager.py",
        "TaskManager.execute_task",
        "task_manager_internal",
        "asyncio.create_task",
        1,
        "Legacy TaskManager execution remains the old async fact source.",
    ),
    **_allowed_sites(
        "pixelle_video/services/image_analysis.py",
        "ImageAnalysisService.__call__",
        "comfykit_access",
        "self.core._get_or_create_comfykit",
        1,
        "Legacy analysis service migration is outside 02-E2-A.",
    ),
    **_allowed_sites(
        "pixelle_video/services/image_analysis.py",
        "ImageAnalysisService.__call__",
        "comfykit_execute",
        "kit.execute",
        1,
        "Legacy analysis service migration is outside 02-E2-A.",
    ),
    **_allowed_sites(
        "pixelle_video/services/media.py",
        "MediaService.__call__",
        "api_media_service",
        "self.core.api_media",
        1,
        "Library API-provider media semantics are not migrated.",
    ),
    **_allowed_sites(
        "pixelle_video/services/media.py",
        "MediaService.__call__",
        "comfykit_access",
        "self.core._get_or_create_comfykit",
        1,
        "Library path and synchronous semantics are not migrated.",
    ),
    **_allowed_sites(
        "pixelle_video/services/media.py",
        "MediaService.__call__",
        "comfykit_execute",
        "kit.execute",
        1,
        "Library path and synchronous semantics are not migrated.",
    ),
    **_allowed_sites(
        "pixelle_video/services/media.py",
        "MediaService.__call__",
        "adapter_submission",
        "self.core.comfyui_adapter.execute",
        1,
        "Library private-adapter execution remains outside the 02-E2-A entry migration.",
    ),
    **_allowed_sites(
        "pixelle_video/services/api_media.py",
        "APIProviderMediaService._generate_image",
        "provider_submission",
        "client.generate_image",
        1,
        "Library direct-provider image execution remains outside 02-E2-A.",
    ),
    **_allowed_sites(
        "pixelle_video/services/api_media.py",
        "APIProviderMediaService._generate_video",
        "provider_submission",
        "client.generate_video",
        1,
        "Library direct-provider video execution remains outside 02-E2-A.",
    ),
    **_allowed_sites(
        "pixelle_video/media_jobs/executor.py",
        "RecoverableComfyUIExecutor._submit_new",
        "adapter_submission",
        "self.adapter.submit_prepared",
        1,
        "Existing persistent worker submission is protected and outside entry migration.",
    ),
    **_allowed_sites(
        "pixelle_video/services/tts_service.py",
        "TTSService._call_comfyui_workflow",
        "comfykit_access",
        "self.core._get_or_create_comfykit",
        1,
        "TTS task migration is outside 02-E2-A.",
    ),
    **_allowed_sites(
        "pixelle_video/services/tts_service.py",
        "TTSService._call_comfyui_workflow",
        "comfykit_execute",
        "kit.execute",
        1,
        "TTS task migration is outside 02-E2-A.",
    ),
    **_allowed_sites(
        "pixelle_video/services/video_analysis.py",
        "VideoAnalysisService.__call__",
        "comfykit_access",
        "self.core._get_or_create_comfykit",
        1,
        "Legacy analysis service migration is outside 02-E2-A.",
    ),
    **_allowed_sites(
        "pixelle_video/services/video_analysis.py",
        "VideoAnalysisService.__call__",
        "comfykit_execute",
        "kit.execute",
        1,
        "Legacy analysis service migration is outside 02-E2-A.",
    ),
    **_allowed_sites(
        "web/components/output_preview.py",
        "render_single_output",
        "core_generate_video",
        "pixelle_video.generate_video",
        1,
        "Web composite preview is deferred to later orchestration.",
    ),
    **_allowed_sites(
        "web/components/style_config.py",
        "render_style_config",
        "media_service",
        "pixelle_video.media",
        1,
        "Web synchronous preview is not migrated.",
    ),
    **_allowed_sites(
        "web/pages/2_📚_History.py",
        "main",
        "history",
        "pixelle_video.history.get_task_list",
        1,
        "Legacy file History remains a separate deferred fact source.",
    ),
    **_allowed_sites(
        "web/pages/2_📚_History.py",
        "render_grid_task_card",
        "history",
        "pixelle_video.history.delete_task",
        1,
        "Legacy file History remains a separate deferred fact source.",
    ),
    **_allowed_sites(
        "web/pages/2_📚_History.py",
        "render_grid_task_card",
        "history",
        "pixelle_video.history.get_task_detail",
        1,
        "Legacy file History remains a separate deferred fact source.",
    ),
    **_allowed_sites(
        "web/pages/2_📚_History.py",
        "render_sidebar_controls",
        "history",
        "pixelle_video.history.get_statistics",
        1,
        "Legacy file History remains a separate deferred fact source.",
    ),
    **_allowed_sites(
        "web/pages/2_📚_History.py",
        "render_task_detail_modal",
        "history",
        "pixelle_video.history.get_task_detail",
        1,
        "Legacy file History remains a separate deferred fact source.",
    ),
    **_allowed_sites(
        "web/pipelines/action_transfer.py",
        "ActionTransferPipelineUI._render_output_preview.generate_audio_visual_video",
        "comfykit_access",
        "pixelle_video._get_or_create_comfykit",
        1,
        "Action Transfer is a deferred multi-asset composite pipeline.",
    ),
    **_allowed_sites(
        "web/pipelines/action_transfer.py",
        "ActionTransferPipelineUI._render_output_preview.generate_audio_visual_video",
        "comfykit_execute",
        "kit.execute",
        1,
        "Action Transfer is a deferred multi-asset composite pipeline.",
    ),
    **_allowed_sites(
        "web/pipelines/action_transfer.py",
        "ActionTransferPipelineUI._render_output_preview.generate_audio_visual_video",
        "media_service",
        "pixelle_video.media",
        1,
        "Action Transfer is a deferred multi-asset composite pipeline.",
    ),
    **_allowed_sites(
        "web/pipelines/digital_human.py",
        "DigitalHumanPipelineUI._render_output_preview.generate_digital_human_video",
        "comfykit_access",
        "pixelle_video._get_or_create_comfykit",
        3,
        "Digital Human is a deferred multi-stage composite pipeline.",
    ),
    **_allowed_sites(
        "web/pipelines/digital_human.py",
        "DigitalHumanPipelineUI._render_output_preview.generate_digital_human_video",
        "comfykit_execute",
        "kit.execute",
        5,
        "Digital Human is a deferred multi-stage composite pipeline.",
    ),
    **_allowed_sites(
        "web/pipelines/digital_human.py",
        "DigitalHumanPipelineUI._render_output_preview.generate_digital_human_video",
        "media_service",
        "pixelle_video.media",
        2,
        "Digital Human is a deferred multi-stage composite pipeline.",
    ),
    **_allowed_sites(
        "web/pipelines/digital_human.py",
        "DigitalHumanPipelineUI._render_output_preview.generate_digital_human_video."
        "generate_api_digital_human",
        "media_service",
        "pixelle_video.media",
        1,
        "Digital Human API generation remains in the deferred composite pipeline.",
    ),
    **_allowed_sites(
        "web/pipelines/i2v.py",
        "ImageToVideoPipelineUI._render_output_preview.generate_audio_visual_video",
        "comfykit_access",
        "pixelle_video._get_or_create_comfykit",
        1,
        "Web I2V path and synchronous semantics are not migrated.",
    ),
    **_allowed_sites(
        "web/pipelines/i2v.py",
        "ImageToVideoPipelineUI._render_output_preview.generate_audio_visual_video",
        "comfykit_execute",
        "kit.execute",
        1,
        "Web I2V path and synchronous semantics are not migrated.",
    ),
    **_allowed_sites(
        "web/pipelines/i2v.py",
        "ImageToVideoPipelineUI._render_output_preview.generate_audio_visual_video",
        "media_service",
        "pixelle_video.media",
        1,
        "Web I2V path and synchronous semantics are not migrated.",
    ),
    **_allowed_sites(
        "web/utils/batch_manager.py",
        "SimpleBatchManager.execute_batch",
        "core_generate_video",
        "pixelle_video.generate_video",
        1,
        "Legacy Web batch composite execution is deferred.",
    ),
}


def assert_exact_bypass_closure(
    sources: Mapping[str, str],
    allowlist: Mapping[BypassSite, str],
) -> None:
    actual = scan_bypass_sources(sources)
    assert actual == set(allowlist)
    assert all(reason.strip() for reason in allowlist.values())


def forbidden_references(source: str) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_REFERENCES:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_REFERENCES:
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            leaf = node.name.rsplit(".", 1)[-1]
            if leaf in FORBIDDEN_REFERENCES:
                found.add(leaf)
    return found


def test_declared_facade_scope_has_no_execution_or_dual_state_bypass():
    violations = {}
    scoped_paths = (*MIGRATION_ROOT.rglob("*.py"), *MIGRATED_ENTRY_FILES)
    for path in scoped_paths:
        found = forbidden_references(path.read_text(encoding="utf-8"))
        if found:
            violations[str(path.relative_to(PROJECT_ROOT))] = sorted(found)
    assert violations == {}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from x import ComfyUIAdapter\nComfyUIAdapter()", {"ComfyUIAdapter"}),
        ("repository.create_job(payload)", {"create_job"}),
        ("from api.tasks import task_manager", {"task_manager"}),
        ("from comfykit import ComfyKit as Kit\nKit()", {"ComfyKit"}),
        ("client.provider.submit(payload)", {"provider"}),
        ("helper.write_history(payload.output_path)", {"write_history", "output_path"}),
        ("payload.image_path", {"image_path"}),
        (
            "from pixelle_video.media_jobs import MediaJobRepository as Repo",
            {"MediaJobRepository"},
        ),
    ],
)
def test_architecture_gate_rejects_multiple_bypass_spellings(source, expected):
    assert forbidden_references(source) == expected


def test_architecture_gate_accepts_injected_single_route_submitters():
    source = """
async def submit(request, persistent_submitter, legacy_submitter):
    if request.is_leaf:
        return await persistent_submitter(request)
    return await legacy_submitter(request)
"""
    assert forbidden_references(source) == set()


def test_nf_e2a_01_current_facade_site_is_exact_and_unique():
    assert production_facade_instantiation_sites() == {
        FacadeSite("api/routers/media_jobs.py", "_submit_create_through_facade")
    }


@pytest.mark.parametrize(
    "extra",
    [
        """
from pixelle_video.media_migration import CompatibilitySubmissionFacade as Facade
Facade(submit_persistent_leaf=persistent, submit_legacy_composite=legacy)
""",
        """
import pixelle_video.media_migration as migration
migration.CompatibilitySubmissionFacade(
    submit_persistent_leaf=persistent,
    submit_legacy_composite=legacy,
)
""",
        """
import pixelle_video as pv
pv.media_migration.facade.CompatibilitySubmissionFacade(
    submit_persistent_leaf=persistent,
    submit_legacy_composite=legacy,
)
""",
    ],
)
def test_nf_e2a_01_alias_and_module_attribute_bypasses_fail(extra):
    expected = FacadeSite("target.py", "build")
    target = """
from pixelle_video.media_migration import CompatibilitySubmissionFacade
def build():
    return CompatibilitySubmissionFacade(
        submit_persistent_leaf=persistent,
        submit_legacy_composite=legacy,
    )
"""
    with pytest.raises(AssertionError):
        assert_single_facade_site(
            {"target.py": target, "bypass.py": extra},
            expected,
        )


def test_nf_e2a_01_non_calls_are_not_false_instantiation_sites():
    source = """
from pixelle_video.media_migration import CompatibilitySubmissionFacade as Facade
annotation: Facade
text = "Facade()"
attribute = migration.CompatibilitySubmissionFacade
"""
    assert facade_instantiation_sites(source, "annotations.py") == set()


@pytest.mark.parametrize(
    "alias_assignments",
    [
        "Facade = CompatibilitySubmissionFacade",
        "Facade = CompatibilitySubmissionFacade\nFacadeAgain = Facade",
    ],
)
def test_nf_e2a_01_assignment_alias_chains_fail(alias_assignments):
    target = """
from pixelle_video.media_migration import CompatibilitySubmissionFacade
def build():
    return CompatibilitySubmissionFacade(persistent, legacy)
"""
    alias_name = "FacadeAgain" if "FacadeAgain" in alias_assignments else "Facade"
    bypass = f"""
from pixelle_video.media_migration import CompatibilitySubmissionFacade
{alias_assignments}
def build_again():
    return {alias_name}(persistent, legacy)
"""
    with pytest.raises(AssertionError):
        assert_single_facade_site(
            {"target.py": target, "bypass.py": bypass},
            FacadeSite("target.py", "build"),
        )


def test_nf_e2a_01_assignment_alias_cycle_terminates_and_finds_second_site():
    source = """
from pixelle_video.media_migration import CompatibilitySubmissionFacade
Facade = CompatibilitySubmissionFacade
FacadeAgain = Facade
Facade = FacadeAgain
def build():
    CompatibilitySubmissionFacade(persistent, legacy)
    return FacadeAgain(persistent, legacy)
"""
    assert facade_instantiation_sites(source, "target.py") == {
        FacadeSite("target.py", "build", 1),
        FacadeSite("target.py", "build", 2),
    }


def test_nf_e2a_01_unresolved_facade_capability_fails_closed():
    source = """
from pixelle_video.media_migration import CompatibilitySubmissionFacade
Facade = choose(CompatibilitySubmissionFacade)
Facade(persistent, legacy)
"""
    with pytest.raises(AssertionError):
        facade_instantiation_sites(source, "dynamic.py")


def test_cr_e2a_01_facade_tuple_unpack_is_propagated():
    source = """
from pixelle_video.media_migration import CompatibilitySubmissionFacade
Facade, unused = CompatibilitySubmissionFacade, object
def build():
    return Facade(persistent, legacy)
"""
    assert facade_instantiation_sites(source, "probe.py") == {
        FacadeSite("probe.py", "build")
    }


@pytest.mark.parametrize(
    "source",
    [
        """
from pixelle_video.media_migration import CompatibilitySubmissionFacade
holder.facade = CompatibilitySubmissionFacade
holder.facade(persistent, legacy)
""",
        """
from pixelle_video.media_migration import CompatibilitySubmissionFacade
choose(CompatibilitySubmissionFacade)(persistent, legacy)
""",
    ],
)
def test_cr_e2a_01_dynamic_facade_transfer_fails_closed(source):
    with pytest.raises(AssertionError):
        facade_instantiation_sites(source, "probe.py")


def test_cr_e2a_01_same_owner_preserves_both_instances():
    source = """
from pixelle_video.media_migration import CompatibilitySubmissionFacade
def build():
    CompatibilitySubmissionFacade(persistent, legacy)
    CompatibilitySubmissionFacade(persistent, legacy)
"""
    assert facade_instantiation_sites(source, "probe.py") == {
        FacadeSite("probe.py", "build", 1),
        FacadeSite("probe.py", "build", 2),
    }


def test_nf_e2a_02_current_entry_reachable_closure_submits_exactly_once():
    source = MIGRATED_ENTRY_FILES[0].read_text(encoding="utf-8")
    assert application_service_submission_count(source, "create_media_job") == 1


def test_nf_e2a_02_indirect_helper_double_submit_fails():
    source = """
async def create_media_job(request, service: MediaJobServiceDep, key):
    return await _submit_create_through_facade(request, service, key)

async def _submit_create_through_facade(request, service, key):
    await service.create(request, key)
    return await _also_submit(request, service, key)

async def _also_submit(request, app_service, key):
    return await app_service.create(request, key)
"""
    assert application_service_submission_count(source, "create_media_job") == 2
    with pytest.raises(AssertionError):
        assert application_service_submission_count(source, "create_media_job") == 1


@pytest.mark.parametrize(
    "source",
    [
        """
async def create_media_job(request, service: MediaJobServiceDep, key):
    try:
        return await service.create(request, key)
    except Exception:
        return await legacy(request)
""",
        """
async def create_media_job(request, service: MediaJobServiceDep, key):
    while True:
        return await service.create(request, key)
""",
    ],
)
def test_nf_e2a_02_fallback_or_retry_loop_fails(source):
    with pytest.raises(AssertionError):
        application_service_submission_count(source, "create_media_job")


def test_nf_e2a_02_controlled_helper_cycle_fails_closed():
    source = """
async def create_media_job(request, service: MediaJobServiceDep, key):
    cache.create(key)
    return await helper_a(request, service, key)

async def helper_a(request, app_service, key):
    await app_service.create(request, key)
    return await helper_b(request, app_service, key)

async def helper_b(request, app_service, key):
    return await helper_a(request, app_service, key)
"""
    with pytest.raises(AssertionError):
        application_service_submission_count(source, "create_media_job")


def test_nf_e2a_02_uncontrolled_helper_cycle_is_ignored():
    source = """
async def create_media_job(request, service: MediaJobServiceDep, key):
    await service.create(request, key)
    return await helper_a(request)

async def helper_a(request):
    return await helper_b(request)

async def helper_b(request):
    return await helper_a(request)
"""
    assert application_service_submission_count(source, "create_media_job") == 1


@pytest.mark.parametrize(
    "aliases",
    [
        "submit_again = _also_submit",
        "submit_once = _also_submit\nsubmit_again = submit_once",
    ],
)
def test_nf_e2a_02_helper_alias_double_submit_fails(aliases):
    source = f"""
async def create_media_job(request, service: MediaJobServiceDep, key):
    await service.create(request, key)
    return await submit_again(request, service, key)

async def _also_submit(request, app_service, key):
    return await app_service.create(request, key)

{aliases}
"""
    assert application_service_submission_count(source, "create_media_job") == 2
    with pytest.raises(AssertionError):
        assert application_service_submission_count(source, "create_media_job") == 1


@pytest.mark.parametrize(
    "alias_lines",
    [
        "create_again = service.create",
        "create_once = service.create\n    create_again = create_once",
    ],
)
def test_nf_e2a_02_method_alias_double_submit_fails(alias_lines):
    source = f"""
async def create_media_job(request, service: MediaJobServiceDep, key):
    await service.create(request, key)
    {alias_lines}
    return await create_again(request, key)
"""
    assert application_service_submission_count(source, "create_media_job") == 2
    with pytest.raises(AssertionError):
        assert application_service_submission_count(source, "create_media_job") == 1


def test_nf_e2a_02_alias_cycles_terminate_without_hiding_submission():
    source = """
async def create_media_job(request, service: MediaJobServiceDep, key):
    create_once = service.create
    create_again = create_once
    create_once = create_again
    return await create_again(request, key)

helper_a = helper_b
helper_b = helper_a
"""
    assert application_service_submission_count(source, "create_media_job") == 1


def test_nf_e2a_02_dynamic_service_method_fails_closed():
    source = """
async def create_media_job(request, service: MediaJobServiceDep, key, method):
    create_again = getattr(service, method)
    return await create_again(request, key)
"""
    with pytest.raises(AssertionError):
        application_service_submission_count(source, "create_media_job")


def test_cr_e2a_02_same_helper_two_call_sites_count_twice():
    source = """
async def create_media_job(request, service: MediaJobServiceDep, key):
    await submit_once(request, service, key)
    return await submit_once(request, service, key)

async def submit_once(request, service, key):
    return await service.create(request, key)
"""
    assert application_service_submission_count(source, "create_media_job") == 2


@pytest.mark.parametrize("keyword", [False, True])
def test_cr_e2a_03_method_capability_crosses_helper_parameter(keyword):
    invocation = (
        "invoke(submitter=create_again, request=request, key=key)"
        if keyword
        else "invoke(create_again, request, key)"
    )
    source = f"""
async def create_media_job(request, service: MediaJobServiceDep, key):
    create_again = service.create
    return await {invocation}

async def invoke(submitter, request, key):
    return await submitter(request, key)
"""
    assert application_service_submission_count(source, "create_media_job") == 1


def test_cr_e2a_03_method_capability_crosses_multiple_helpers():
    source = """
async def create_media_job(request, service: MediaJobServiceDep, key):
    create_again = getattr(service, "create")
    return await relay(create_again, request, key)

async def relay(submitter, request, key):
    return await invoke(submitter, request, key)

async def invoke(submitter, request, key):
    return await submitter(request, key)
"""
    assert application_service_submission_count(source, "create_media_job") == 1


def test_nf_e2a_03_real_bypass_scan_equals_declared_allowlist():
    migrated = {
        str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
        for path in MIGRATED_ENTRY_FILES
    }
    assert migrated.isdisjoint({site.file for site in LEGACY_BYPASS_ALLOWLIST})
    assert_exact_bypass_closure(production_bypass_sources(), LEGACY_BYPASS_ALLOWLIST)


def test_nf_e2a_03_outside_allowlist_bypass_fails():
    sources = {
        "new_entry.py": (
            "from api.tasks import task_manager as tm\n"
            "def run():\n"
            "    tm.create_task()\n"
        )
    }
    with pytest.raises(AssertionError):
        assert_exact_bypass_closure(sources, {})


def test_nf_e2a_03_extra_call_inside_allowlisted_owner_fails():
    source = """
def run():
    task_manager.create_task()
    task_manager.execute_task()
"""
    expected = {
        BypassSite("legacy.py", "run", "task_manager", "task_manager.create_task"): "legacy"
    }
    with pytest.raises(AssertionError):
        assert_exact_bypass_closure({"legacy.py": source}, expected)


@pytest.mark.parametrize(
    "source",
    [
        "def moved():\n    task_manager.create_task()\n",
        "def run():\n    return None\n",
    ],
)
def test_nf_e2a_03_moved_or_deleted_real_call_fails(source):
    expected = {
        BypassSite("legacy.py", "run", "task_manager", "task_manager.create_task"): "legacy"
    }
    with pytest.raises(AssertionError):
        assert_exact_bypass_closure({"legacy.py": source}, expected)


@pytest.mark.parametrize(
    ("source", "call_type"),
    [
        ("client.provider.submit(payload)", "provider_submission"),
        ("adapter.execute(payload)", "adapter_submission"),
        ('getattr(task_manager, "create_task")()', "task_manager"),
    ],
)
def test_nf_e2a_03_second_review_probes_fail(source, call_type):
    actual = scan_bypass_sources({"probe.py": source})
    assert {site.call_type for site in actual} == {call_type}
    with pytest.raises(AssertionError):
        assert_exact_bypass_closure({"probe.py": source}, {})


def test_nf_e2a_03_static_alias_calls_are_classified():
    source = """
provider_submit = client.provider.submit
adapter_execute = adapter.execute
provider_submit(payload)
adapter_execute(payload)
"""
    actual = scan_bypass_sources({"probe.py": source})
    assert {site.call_type for site in actual} == {
        "provider_submission",
        "adapter_submission",
    }


def test_nf_e2a_03_dynamic_method_on_controlled_object_fails_closed():
    source = """
def run(adapter, method):
    return getattr(adapter, method)(payload)
"""
    with pytest.raises(AssertionError):
        scan_bypass_sources({"probe.py": source})


@pytest.mark.parametrize(
    ("source", "call_type"),
    [
        (
            'submit = getattr(adapter, "execute")\nsubmit(payload)',
            "adapter_submission",
        ),
        (
            'submit = getattr(provider, "submit")\n'
            "submit_again = submit\nsubmit_again(payload)",
            "provider_submission",
        ),
        (
            'submit = getattr(task_manager, "create_task")\nsubmit(payload)',
            "task_manager",
        ),
    ],
)
def test_cr_e2a_04_constant_getattr_alias_is_classified(source, call_type):
    actual = scan_bypass_sources({"probe.py": source})
    assert {site.call_type for site in actual} == {call_type}


def test_cr_e2a_05_dynamic_getattr_alias_chain_fails_closed():
    source = """
def run(adapter, method):
    submit = getattr(adapter, method)
    submit_again = submit
    return submit_again(payload)
"""
    with pytest.raises(AssertionError):
        scan_bypass_sources({"probe.py": source})


def test_controlled_flow_positive_ordinary_alias_and_getattr_are_ignored():
    source = """
def run(cache):
    close = getattr(cache, "close")
    close_again = close
    return close_again()
"""
    assert scan_bypass_sources({"probe.py": source}) == set()


@pytest.mark.parametrize(
    "signature",
    [
        "factory=CompatibilitySubmissionFacade",
        "*, factory=CompatibilitySubmissionFacade",
    ],
)
def test_pr_e2a_01_facade_default_parameter_is_propagated(signature):
    source = f"""
from pixelle_video.media_migration import CompatibilitySubmissionFacade
def build({signature}):
    return factory(persistent, legacy)
"""
    assert facade_instantiation_sites(source, "probe.py") == {
        FacadeSite("probe.py", "build")
    }


@pytest.mark.parametrize(
    "body",
    [
        """
def build(factory=CompatibilitySubmissionFacade):
    return invoke(factory)
def invoke(factory):
    return factory(persistent, legacy)
""",
        """
def build(factory=CompatibilitySubmissionFacade):
    return relay(factory)
def relay(factory):
    return invoke(factory)
def invoke(factory):
    return factory(persistent, legacy)
""",
        """
def build(factory=CompatibilitySubmissionFacade):
    return invoke(factory=factory)
def invoke(*, factory):
    return factory(persistent, legacy)
""",
    ],
)
def test_pr_e2a_01_facade_default_crosses_helpers(body):
    source = (
        "from pixelle_video.media_migration import CompatibilitySubmissionFacade\n"
        + body
    )
    sites = facade_instantiation_sites(source, "probe.py")
    assert len(sites) == 1
    assert next(iter(sites)).owner == "invoke"


@pytest.mark.parametrize(
    "invocation",
    [
        "invoke(*(factory,))",
        "invoke(**{'factory': factory})",
    ],
)
def test_pr_e2a_01_controlled_default_unpacking_fails_closed(invocation):
    source = f"""
from pixelle_video.media_migration import CompatibilitySubmissionFacade
def build(factory=CompatibilitySubmissionFacade):
    return {invocation}
def invoke(factory):
    return factory(persistent, legacy)
"""
    with pytest.raises(AssertionError):
        facade_instantiation_sites(source, "probe.py")


def test_pr_e2a_01_unrelated_default_callback_is_ignored():
    source = """
def ordinary(value):
    return value
def build(callback=ordinary):
    return callback(payload)
"""
    assert facade_instantiation_sites(source, "probe.py") == set()


def test_pr_e2a_02_keyword_only_helper_double_submit_counts_twice():
    source = """
async def create_media_job(request, service: MediaJobServiceDep, key):
    await service.create(request, key)
    return await invoke(submitter=service.create, request=request, key=key)
async def invoke(*, submitter, request, key):
    return await submitter(request, key)
"""
    assert application_service_submission_count(source, "create_media_job") == 2


@pytest.mark.parametrize(
    ("entry_body", "helpers"),
    [
        (
            "return await invoke(submitter=service.create, request=request, key=key)",
            """
async def invoke(*, submitter, request, key):
    return await submitter(request, key)
""",
        ),
        (
            "return await relay(submitter=service.create, request=request, key=key)",
            """
async def relay(*, submitter, request, key):
    return await invoke(submitter=submitter, request=request, key=key)
async def invoke(*, submitter, request, key):
    return await submitter(request, key)
""",
        ),
        (
            "return await relay(service.create, request=request, key=key)",
            """
async def relay(submitter, *, request, key):
    return await invoke(submitter=submitter, request=request, key=key)
async def invoke(*, submitter, request, key):
    return await submitter(request, key)
""",
        ),
    ],
)
def test_pr_e2a_02_keyword_only_method_capability_counts_once(
    entry_body,
    helpers,
):
    source = f"""
async def create_media_job(request, service: MediaJobServiceDep, key):
    {entry_body}
{helpers}
"""
    assert application_service_submission_count(source, "create_media_job") == 1


def test_pr_e2a_02_helper_alias_keeps_keyword_only_binding():
    source = """
async def create_media_job(request, service: MediaJobServiceDep, key):
    return await invoke_alias(
        submitter=service.create,
        request=request,
        key=key,
    )
async def invoke(*, submitter, request, key):
    return await submitter(request, key)
invoke_alias = invoke
"""
    assert application_service_submission_count(source, "create_media_job") == 1


@pytest.mark.parametrize(
    "invocation",
    [
        "invoke(**{'submitter': service.create, 'request': request, 'key': key})",
        "invoke(*(service.create, request, key))",
    ],
)
def test_pr_e2a_02_unresolved_submission_unpacking_fails_closed(invocation):
    source = f"""
async def create_media_job(request, service: MediaJobServiceDep, key):
    return await {invocation}
async def invoke(*args, **kwargs):
    return None
"""
    with pytest.raises(AssertionError):
        application_service_submission_count(source, "create_media_job")


def test_pr_e2a_02_unrelated_keyword_only_callback_is_ignored():
    source = """
async def create_media_job(request, service: MediaJobServiceDep, key):
    return await invoke(callback=cache.close, request=request)
async def invoke(*, callback, request):
    return callback(request)
"""
    assert application_service_submission_count(source, "create_media_job") == 0


@pytest.mark.parametrize(
    ("source", "call_type"),
    [
        (
            """
def invoke(submitter, payload):
    return submitter(payload)
def run(adapter, payload):
    return invoke(adapter.execute, payload)
""",
            "adapter_submission",
        ),
        (
            """
def invoke(submitter, payload):
    return submitter(payload)
def run(provider, payload):
    return invoke(provider.submit, payload)
""",
            "provider_submission",
        ),
        (
            """
def invoke(*, submitter, payload):
    return submitter(payload)
def run(adapter, payload):
    return invoke(submitter=adapter.execute, payload=payload)
""",
            "adapter_submission",
        ),
        (
            """
def relay(submitter, payload):
    return invoke(submitter=submitter, payload=payload)
def invoke(*, submitter, payload):
    return submitter(payload)
def run(provider, payload):
    return relay(provider.submit, payload)
""",
            "provider_submission",
        ),
        (
            """
def invoke(submitter, payload):
    return submitter(payload)
def run(adapter, payload):
    submit = getattr(adapter, "execute")
    return invoke(submit, payload)
""",
            "adapter_submission",
        ),
    ],
)
def test_pr_e2a_03_bypass_method_capability_crosses_helpers(source, call_type):
    actual = scan_bypass_sources({"probe.py": source})
    assert {site.call_type for site in actual} == {call_type}
    assert {site.owner for site in actual} == {"invoke"}


@pytest.mark.parametrize(
    "source",
    [
        """
def invoke(submitter, payload):
    return submitter(payload)
def run(adapter, method, payload):
    submit = getattr(adapter, method)
    return invoke(submit, payload)
""",
        """
def invoke(**kwargs):
    return None
def run(adapter, payload):
    return invoke(**{"submitter": adapter.execute, "payload": payload})
""",
    ],
)
def test_pr_e2a_03_unresolved_bypass_propagation_fails_closed(source):
    with pytest.raises(AssertionError):
        scan_bypass_sources({"probe.py": source})


def test_pr_e2a_03_unrelated_helper_callback_is_ignored():
    source = """
def invoke(callback, payload):
    return callback(payload)
def run(cache, payload):
    return invoke(cache.close, payload)
"""
    assert scan_bypass_sources({"probe.py": source}) == set()


def test_pr_e2a_combined_method_alias_crosses_two_keyword_only_helpers():
    source = """
async def create_media_job(request, service: MediaJobServiceDep, key):
    submit = service.create
    return await relay(submitter=submit, request=request, key=key)
async def relay(*, submitter, request, key):
    return await invoke(submitter=submitter, request=request, key=key)
async def invoke(*, submitter, request, key):
    return await submitter(request, key)
"""
    assert application_service_submission_count(source, "create_media_job") == 1


def test_pr_e2a_combined_adapter_getattr_crosses_keyword_only_helper():
    source = """
def invoke(*, submitter, payload):
    return submitter(payload)
def run(adapter, payload):
    submit = getattr(adapter, "execute")
    return invoke(submitter=submit, payload=payload)
"""
    actual = scan_bypass_sources({"probe.py": source})
    assert {site.call_type for site in actual} == {"adapter_submission"}


def test_pr_e2a_combined_provider_crosses_helper_alias_and_mixed_signature():
    source = """
def invoke(submitter, *, payload):
    return submitter(payload)
invoke_alias = invoke
def run(provider, payload):
    return invoke_alias(provider.submit, payload=payload)
"""
    actual = scan_bypass_sources({"probe.py": source})
    assert {site.call_type for site in actual} == {"provider_submission"}


def test_pr_e2a_combined_same_helper_two_controlled_call_sites_are_counted():
    source = """
def invoke(submitter, payload):
    return submitter(payload)
def run(adapter, payload):
    invoke(adapter.execute, payload)
    return invoke(adapter.execute, payload)
"""
    actual = scan_bypass_sources({"probe.py": source})
    assert actual == {
        BypassSite("probe.py", "invoke", "adapter_submission", "submitter", 1),
        BypassSite("probe.py", "invoke", "adapter_submission", "submitter", 2),
    }


def test_pr_e2a_combined_generic_helper_classifies_only_controlled_path():
    source = """
def invoke(callback, payload):
    return callback(payload)
def run_controlled(adapter, payload):
    return invoke(adapter.execute, payload)
def run_ordinary(cache, payload):
    return invoke(cache.close, payload)
"""
    actual = scan_bypass_sources({"probe.py": source})
    assert actual == {
        BypassSite("probe.py", "invoke", "adapter_submission", "callback")
    }


def test_pr_e2a_combined_uncontrolled_defaults_and_callbacks_are_ignored():
    source = """
def ordinary(payload):
    return payload
def invoke(*, callback=ordinary, payload=None):
    return callback(payload)
def run(payload):
    return invoke(payload=payload)
"""
    assert facade_instantiation_sites(source, "probe.py") == set()
    assert scan_bypass_sources({"probe.py": source}) == set()


@pytest.mark.parametrize(
    "alias_lines",
    [
        "invoke_alias = invoke",
        "invoke_once = invoke\n    invoke_alias = invoke_once",
    ],
)
def test_pc_e2a_facade_crosses_function_local_helper_alias(alias_lines):
    source = f"""
from pixelle_video.media_migration import CompatibilitySubmissionFacade
def build(factory=CompatibilitySubmissionFacade):
    {alias_lines}
    return invoke_alias(factory)
def invoke(factory):
    return factory(persistent, legacy)
"""
    assert facade_instantiation_sites(source, "probe.py") == {
        FacadeSite("probe.py", "invoke")
    }


def test_pc_e2a_facade_instance_method_helper_fails_closed():
    source = """
from pixelle_video.media_migration import CompatibilitySubmissionFacade
class Runner:
    def invoke(self, factory):
        return factory(persistent, legacy)
def build(factory=CompatibilitySubmissionFacade):
    return runner.invoke(factory)
"""
    with pytest.raises(AssertionError):
        facade_instantiation_sites(source, "probe.py")


@pytest.mark.parametrize(
    "invocation",
    [
        "runner.invoke(service, request)",
        "dispatch(service.create, request)",
    ],
)
def test_pc_e2a_submission_capability_unknown_callee_fails_closed(invocation):
    source = f"""
async def create_media_job(request, service: MediaJobServiceDep, key):
    return await {invocation}
"""
    with pytest.raises(AssertionError):
        application_service_submission_count(source, "create_media_job")


def test_pc_e2a_submission_crosses_function_local_helper_alias():
    source = """
async def create_media_job(request, service: MediaJobServiceDep, key):
    invoke_alias = invoke
    return await invoke_alias(
        submitter=service.create,
        request=request,
        key=key,
    )
async def invoke(*, submitter, request, key):
    return await submitter(request, key)
"""
    assert application_service_submission_count(source, "create_media_job") == 1


@pytest.mark.parametrize(
    ("object_name", "method"),
    [
        ("provider", "submit"),
        ("adapter", "execute"),
    ],
)
def test_pc_e2a_bypass_instance_method_helper_fails_closed(
    object_name,
    method,
):
    source = f"""
class Runner:
    def invoke(self, submitter, payload):
        return submitter(payload)
    def run(self, {object_name}, payload):
        return self.invoke({object_name}.{method}, payload)
"""
    with pytest.raises(AssertionError):
        scan_bypass_sources({"probe.py": source})


@pytest.mark.parametrize(
    "alias_lines",
    [
        "invoke_alias = invoke",
        "invoke_once = invoke\n    invoke_alias = invoke_once",
    ],
)
def test_pc_e2a_bypass_crosses_function_local_helper_alias(alias_lines):
    source = f"""
def invoke(*, submitter, payload):
    return submitter(payload)
def run(provider, payload):
    {alias_lines}
    return invoke_alias(submitter=provider.submit, payload=payload)
"""
    assert scan_bypass_sources({"probe.py": source}) == {
        BypassSite("probe.py", "invoke", "provider_submission", "submitter")
    }


def test_pc_e2a_facade_default_keyword_only_local_alias_combination():
    source = """
from pixelle_video.media_migration import CompatibilitySubmissionFacade
def build(*, factory=CompatibilitySubmissionFacade):
    invoke_alias = invoke
    return invoke_alias(factory=factory)
def invoke(*, factory):
    return factory(persistent, legacy)
"""
    assert facade_instantiation_sites(source, "probe.py") == {
        FacadeSite("probe.py", "invoke")
    }


def test_pc_e2a_ordinary_callback_unknown_attribute_is_ignored():
    source = """
def run(runner, cache, payload):
    return runner.invoke(cache.close, payload)
"""
    assert scan_bypass_sources({"probe.py": source}) == set()


def test_pc_e2a_ordinary_method_alias_unknown_attribute_is_ignored():
    source = """
def run(runner, cache, payload):
    callback = cache.close
    return runner.invoke(callback, payload)
"""
    assert scan_bypass_sources({"probe.py": source}) == set()


def test_pc_e2a_uncontrolled_unpacking_to_unknown_callee_is_ignored():
    submission_source = """
async def create_media_job(request, service: MediaJobServiceDep, key):
    return unknown(*(cache.close, request), **{"key": key})
"""
    bypass_source = """
def run(cache, payload):
    return unknown(*(cache.close, payload), **{"flag": True})
"""
    assert (
        application_service_submission_count(
            submission_source,
            "create_media_job",
        )
        == 0
    )
    assert scan_bypass_sources({"probe.py": bypass_source}) == set()
