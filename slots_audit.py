"""slots_audit: find code that converting classes to __slots__ can break, SILENTLY first.

    python slots_audit.py SRC_DIR --classes Order,OrderItem
    python slots_audit.py SRC_DIR tests/ --classes Order --also .venv/lib/python3.11/site-packages/somelib
    python slots_audit.py SRC_DIR --classes Order --json audit.json

Static (AST) analysis, standard library only. Findings are graded:

  SILENT  no exception is raised but behaviour or the memory saving changes  -> exit code 1
  LOUD    raises at runtime; your tests catch it only if they execute that line
  INFO    worth a look (old pickles, unverifiable bases, weakref containers)

Each finding is `confirmed` (the object is known to be a target: annotated, constructed
from the class, or `self` inside it) or `possible` (generic pattern, type unknown).
See README_SLOTS_AUDIT.md.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

__all__ = ["audit", "main"]

SILENT, LOUD, INFO = "SILENT", "LOUD", "INFO"
_ORDER = {SILENT: 0, LOUD: 1, INFO: 2}
_EXCLUDE_DIRS = {".git", "__pycache__", ".venv", "venv", "env", ".env", "site-packages", "node_modules",
                 "build", "dist", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".eggs"}
# bases that add no __dict__ to a slotted subclass
_SAFE_BASES = {"object", "Generic", "Protocol", "ABC"}
_SWALLOW_ALL = {"Exception", "BaseException"}
_PICKLE_LOADS = {("pickle", "load"), ("pickle", "loads"), ("cPickle", "load"), ("cPickle", "loads"),
                 ("dill", "load"), ("dill", "loads"), ("joblib", "load"), ("cloudpickle", "load"),
                 ("cloudpickle", "loads"), ("shelve", "open"), ("torch", "load")}
_WEAKREF_FUNCS = {"ref", "proxy", "finalize", "WeakMethod"}
_WEAKREF_CONTAINERS = {"WeakValueDictionary", "WeakKeyDictionary", "WeakSet"}
_ATTRS_SLOTTED_BY_DEFAULT = {"define", "frozen", "mutable"}


class Finding(dict):
    def __init__(self, severity, code, path, line, message, snippet="", confidence="confirmed", target=None):
        super().__init__(severity=severity, code=code, path=path, line=line, message=message,
                         snippet=snippet, confidence=confidence, target=target)


# --------------------------------------------------------------------------- helpers
def _dotted(node):
    """`a.b.C` -> 'a.b.C'; subscripts are stripped (Generic[T] -> 'Generic')."""
    if isinstance(node, ast.Subscript):
        return _dotted(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return inner + "." + node.attr if inner else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _last(dotted):
    return dotted.rsplit(".", 1)[-1]


def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _kw(call, name):
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _slot_names(value):
    if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        return {_const_str(e) for e in value.elts if _const_str(e)}
    if _const_str(value):
        return {_const_str(value)}
    if isinstance(value, ast.Dict):
        return {_const_str(k) for k in value.keys if k is not None and _const_str(k)}
    return None  # computed: unknown


def _annotation_targets(ann, targets):
    """Target names directly named by an annotation: T, Optional[T], T | None, Union[T, U], 'T'."""
    if ann is None:
        return set()
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            ann = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return set()
    if isinstance(ann, (ast.Name, ast.Attribute)):
        name = _last(_dotted(ann))
        return {name} if name in targets else set()
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        return _annotation_targets(ann.left, targets) | _annotation_targets(ann.right, targets)
    if isinstance(ann, ast.Subscript) and _last(_dotted(ann.value)) in ("Optional", "Union", "Final", "Annotated"):
        sl = ann.slice
        if isinstance(sl, ast.Index):  # Python < 3.9
            sl = sl.value
        elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
        if _last(_dotted(ann.value)) == "Annotated":
            elts = elts[:1]
        out = set()
        for e in elts:
            out |= _annotation_targets(e, targets)
        return out
    return set()


_SEQ_TYPES = {"List", "list", "Sequence", "MutableSequence", "Iterable", "Iterator", "Collection", "Set", "set",
              "FrozenSet", "frozenset", "AbstractSet", "MutableSet", "Deque", "deque", "Tuple", "tuple",
              "Generator", "AsyncIterator", "AsyncIterable", "AsyncGenerator", "Reversible"}
_MAP_TYPES = {"Dict", "dict", "Mapping", "MutableMapping", "DefaultDict", "defaultdict", "OrderedDict"}


def _subscript_args(sub):
    sl = sub.slice
    if isinstance(sl, ast.Index):  # Python < 3.9
        sl = sl.value
    return list(sl.elts) if isinstance(sl, ast.Tuple) else [sl]


def _element_target(ann, targets):
    """T for containers of a target: List[T], Sequence[T], Dict[K, T], Optional[List[T]], 'List[T]'."""
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            ann = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return None
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        found = {_element_target(ann.left, targets), _element_target(ann.right, targets)} - {None}
        return found.pop() if len(found) == 1 else None
    if not isinstance(ann, ast.Subscript):
        return None
    base, args = _last(_dotted(ann.value)), _subscript_args(ann)
    if base in ("Optional", "Union"):
        found = {_element_target(a, targets) for a in args} - {None}
        return found.pop() if len(found) == 1 else None
    if base in _MAP_TYPES and len(args) == 2:
        elem = args[1]
    elif base in _SEQ_TYPES and args:
        elem = args[0]
    else:
        return None
    t = _annotation_targets(elem, targets)
    return next(iter(t)) if len(t) == 1 else None


_OTHER = object()        # scope entry: known to be some class that is not a target
_BUILTIN_TYPES = {"str", "int", "float", "bool", "bytes", "bytearray", "complex", "dict", "list", "set", "tuple",
                  "frozenset", "Dict", "List", "Set", "Tuple", "FrozenSet", "Optional", "Union", "None",
                  "Path", "datetime", "date", "Decimal", "UUID", "Callable", "Pattern", "Match"}


class _T(str):
    """A target class name that remembers how it was established (confirmed / inferred)."""
    kind = "confirmed"


def _tag(name, kind="confirmed"):
    t = _T(name)
    t.kind = getattr(name, "kind", "confirmed") if kind == "confirmed" else kind
    return t


def _type_maps(trees, names):
    """Project-wide, name-based hints: functions returning a target and attributes annotated as one.

    A name whose definitions disagree (any definition without the same target annotation) is dropped.
    """
    maps = {"returns": {}, "return_elems": {}, "attrs": {}, "attr_elems": {}}

    def put(kind, key, value):
        d = maps[kind]
        d[key] = None if key in d and d[key] != value else value

    def one(found):
        return next(iter(found)) if len(found) == 1 else None

    for tree, _extra in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                put("returns", node.name, one(_annotation_targets(node.returns, names)))
                put("return_elems", node.name, _element_target(node.returns, names))
            elif isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        put("attrs", stmt.target.id, one(_annotation_targets(stmt.annotation, names)))
                        put("attr_elems", stmt.target.id, _element_target(stmt.annotation, names))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute):
                put("attrs", node.target.attr, one(_annotation_targets(node.annotation, names)))
                put("attr_elems", node.target.attr, _element_target(node.annotation, names))
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                    and _last(_dotted(node.value.func)) in names:
                for t in node.targets:
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                        put("attrs", t.attr, _last(_dotted(node.value.func)))
    for d in maps.values():
        for k in [k for k, v in d.items() if v is None]:
            del d[k]
    return maps


# --------------------------------------------------------------------------- class model
class ClassInfo:
    def __init__(self, node, path):
        self.node, self.path, self.name, self.line = node, path, node.name, node.lineno
        self.bases = [_dotted(b) for b in node.bases if _dotted(b)]
        self.explicit_slots = None      # set of names, or "unknown" when computed
        self.dataclass_slots = False
        self.dataclass_weakref_kw = False
        self.slotted_decorator = None
        self.weakref = False
        self.fields = set()
        self.members = set()            # methods, properties, class attributes
        self.annotated_with_default = {}
        self.class_vars = set()
        self.cached_properties = []
        for dec in node.decorator_list:
            name = _last(_dotted(dec))
            call = dec if isinstance(dec, ast.Call) else None
            if name == "dataclass" and call is not None and _kw(call, "slots") is True:
                self.dataclass_slots = True
                self.slotted_decorator = "dataclass(slots=True)"
                self.weakref = bool(_kw(call, "weakref_slot"))
                self.dataclass_weakref_kw = _kw(call, "weakref_slot") is not None
            elif name in ("s", "attrs") and _dotted(dec).startswith(("attr.", "attrs.")) and call is not None \
                    and _kw(call, "slots") is True:
                self.slotted_decorator = "attr.s(slots=True)"
                self.weakref = _kw(call, "weakref_slot") is not False
            elif name in _ATTRS_SLOTTED_BY_DEFAULT and (call is None or _kw(call, "slots") is not False) \
                    and (_dotted(dec).startswith(("attr.", "attrs.")) or _dotted(dec) == name):
                self.slotted_decorator = "attrs.%s" % name
                self.weakref = call is None or _kw(call, "weakref_slot") is not False
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    if isinstance(t, ast.Name):
                        if t.id == "__slots__":
                            names = _slot_names(stmt.value)
                            self.explicit_slots = names if names is not None else "unknown"
                        else:
                            self.members.add(t.id)
                            self.class_vars.add(t.id)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                ann = _dotted(stmt.annotation)
                if _last(ann) == "ClassVar":
                    self.members.add(stmt.target.id)
                    self.class_vars.add(stmt.target.id)
                else:
                    self.fields.add(stmt.target.id)
                    if stmt.value is not None:
                        self.annotated_with_default[stmt.target.id] = stmt.lineno
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.members.add(stmt.name)
                if any(_last(_dotted(d)) == "cached_property" for d in stmt.decorator_list):
                    self.cached_properties.append(stmt)
        if isinstance(self.explicit_slots, set):
            self.fields |= self.explicit_slots
            self.weakref = self.weakref or "__weakref__" in self.explicit_slots

    @property
    def own_slotted(self):
        if isinstance(self.explicit_slots, set) and "__dict__" in self.explicit_slots:
            return False
        return self.explicit_slots is not None or self.slotted_decorator is not None


class _Index:
    def __init__(self):
        self.by_name = {}

    def add(self, info):
        self.by_name.setdefault(info.name, []).append(info)

    def chain_problems(self, info, seen=None):
        """(unslotted_ancestors, unverifiable_bases) for a class."""
        seen = seen or set()
        bad, unknown = [], []
        for base in info.bases:
            short = _last(base)
            if short in _SAFE_BASES:
                continue
            defs = self.by_name.get(short)
            if not defs:
                unknown.append(base)
                continue
            for d in defs:
                if id(d) in seen:
                    continue
                seen.add(id(d))
                if not d.own_slotted:
                    bad.append(d)
                b2, u2 = self.chain_problems(d, seen)
                bad += b2
                unknown += u2
        return bad, unknown

    def ancestors(self, info, seen=None):
        seen = seen or set()
        for base in info.bases:
            for d in self.by_name.get(_last(base), ()):
                if id(d) not in seen:
                    seen.add(id(d))
                    yield d
                    yield from self.ancestors(d, seen)

    def descendants(self, names):
        """All classes that inherit (transitively) from any of `names`, with the root target."""
        out, frontier = {}, {n: n for n in names}
        while frontier:
            nxt = {}
            for infos in self.by_name.values():
                for info in infos:
                    for base in info.bases:
                        root = frontier.get(_last(base))
                        if root and id(info) not in out:
                            out[id(info)] = (info, root)
                            nxt[info.name] = root
            frontier = nxt
        return list(out.values())


# --------------------------------------------------------------------------- usage visitor
class _UsageVisitor(ast.NodeVisitor):
    def __init__(self, path, lines, targets, related, index, classes_info):
        self.path, self.lines, self.targets, self.related = path, lines, targets, related
        self.index, self.classes_info = index, classes_info
        self.findings = []
        self.scopes = [{}]              # name -> target class name
        self.try_stack = []             # sets of caught exception names
        self.class_stack = []

    # -- utilities
    def _snip(self, node):
        try:
            return self.lines[node.lineno - 1].strip()
        except (IndexError, AttributeError):
            return ""

    def _add(self, severity, code, node, message, target=None, confirmed=False):
        if confirmed:
            confidence = getattr(target, "kind", "confirmed")
        elif not self.related and severity != INFO:
            confidence = "possible (file does not mention the target classes)"
        else:
            confidence = "possible"
        root = self.subclass_root.get(str(target), str(target)) if target else None
        f = Finding(severity, code, self.path, node.lineno, message, self._snip(node), confidence,
                    root if confirmed else None)
        if confirmed and root != str(target):
            f["via"] = str(target)
        self.findings.append(f)

    def _lookup(self, name):
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def _target_of(self, expr):
        """Target class the expression is known to be an instance of (a _T), else None."""
        if isinstance(expr, ast.Name):
            v = self._lookup(expr.id)
            return v if isinstance(v, str) else None
        if isinstance(expr, ast.Call):
            short = _last(_dotted(expr.func))
            if short in self.names:
                return _tag(short)
            if short in ("copy", "deepcopy", "replace") and expr.args:   # copy.copy(o), dataclasses.replace(o)
                return self._target_of(expr.args[0])
            if short == "next" and expr.args:
                return self._elems_of(expr.args[0])
            r = self.maps["returns"].get(short)
            return _tag(r, "inferred") if r else None
        if isinstance(expr, ast.Attribute):
            if self._is_class_expr(expr.value):
                return None
            a = self.maps["attrs"].get(expr.attr)
            return _tag(a, "inferred") if a else None
        if isinstance(expr, ast.Subscript) and not isinstance(_subscript_args(expr)[0], ast.Slice):
            return self._elems_of(expr.value)
        return None

    def _elems_of(self, expr):
        """Target class of the elements of a container expression, else None."""
        if isinstance(expr, ast.Name):
            v = self._lookup(expr.id)
            return v[1] if isinstance(v, tuple) and v[0] == "elems" else None
        if isinstance(expr, ast.Call):
            short = _last(_dotted(expr.func))
            if isinstance(expr.func, ast.Attribute) and short in ("values", "copy") and not expr.args:
                return self._elems_of(expr.func.value)
            if isinstance(expr.func, ast.Name) and expr.args and \
                    short in ("sorted", "list", "tuple", "set", "frozenset", "reversed", "iter", "filter"):
                return self._elems_of(expr.args[-1])
            r = self.maps["return_elems"].get(short)
            return _tag(r, "inferred") if r else None
        if isinstance(expr, ast.Attribute):
            if self._is_class_expr(expr.value):
                return None
            a = self.maps["attr_elems"].get(expr.attr)
            return _tag(a, "inferred") if a else None
        if isinstance(expr, ast.Subscript) and isinstance(_subscript_args(expr)[0], ast.Slice):
            return self._elems_of(expr.value)
        if isinstance(expr, (ast.List, ast.Tuple, ast.Set)) and expr.elts:
            found = {self._target_of(e) for e in expr.elts}
            if len(found) == 1 and None not in found:
                return found.pop()
        return None

    def _entry_from_annotation(self, ann):
        t = _annotation_targets(ann, self.names)
        if len(t) == 1:
            return _tag(next(iter(t)))
        e = _element_target(ann, self.names) if ann is not None else None
        if e:
            return ("elems", _tag(e))
        return _OTHER if self._unrelated_annotation(ann) else None

    def _unrelated_annotation(self, ann):
        """True if the annotation names only concrete types that cannot hold a target instance."""
        if ann is None or isinstance(ann, ast.Constant):
            return False
        names = {n.id for n in ast.walk(ann) if isinstance(n, ast.Name)} | \
                {n.attr for n in ast.walk(ann) if isinstance(n, ast.Attribute)}
        if not names or names & (self.names | self.target_ancestors):
            return False
        return all(n in _BUILTIN_TYPES or n in self.index.by_name for n in names)

    def _is_other(self, expr):
        return isinstance(expr, ast.Name) and self._lookup(expr.id) is _OTHER

    def _swallows(self, *exc_names):
        for caught in self.try_stack:
            if caught is None or caught & _SWALLOW_ALL or caught & set(exc_names):
                return True
        return False

    def _is_class_expr(self, expr):
        if isinstance(expr, ast.Name) and (expr.id in self.index.by_name or expr.id == "cls"):
            return True
        if isinstance(expr, ast.Attribute) and expr.attr == "__class__":
            return True
        return isinstance(expr, ast.Call) and _dotted(expr.func) == "type"

    def _is_slotted(self, target):
        infos = self.classes_info.get(target, ())
        return bool(infos) and all(i.own_slotted and not self.index.chain_problems(i)[0] for i in infos)

    def _target_weakref_ok(self, target):
        return bool(target) and all(i.weakref for i in self.classes_info.get(target, ()))

    def _target_fields(self, target):
        names = set()
        for info in self.classes_info.get(target, ()):
            names |= info.fields | info.members
            for anc in self.index.ancestors(info):
                names |= anc.fields | anc.members
        return names

    # -- scopes
    def visit_ClassDef(self, node):
        self.class_stack.append(node.name)
        self.scopes.append({})
        self.generic_visit(node)
        self.scopes.pop()
        self.class_stack.pop()

    def _visit_function(self, node):
        scope = {}
        args = node.args
        all_args = list(getattr(args, "posonlyargs", [])) + args.args + args.kwonlyargs
        for a in all_args + [x for x in (args.vararg, args.kwarg) if x]:
            scope[a.arg] = self._entry_from_annotation(a.annotation)
        if self.class_stack and all_args:
            owner = self.class_stack[-1]
            is_static = any(_last(_dotted(d)) in ("staticmethod", "classmethod") for d in node.decorator_list)
            if not is_static and owner in self.names:
                scope[all_args[0].arg] = _tag(owner)
        self.scopes.append(scope)
        saved_try, self.try_stack = self.try_stack, []
        self.generic_visit(node)
        self.try_stack = saved_try
        self.scopes.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node):
        self.generic_visit(node)

    def _forget(self, target):
        for n in ast.walk(target):
            if isinstance(n, ast.Name):
                self.scopes[-1][n.id] = None

    def _bind(self, target, value, annotation=None):
        if not isinstance(target, ast.Name):
            self._forget(target) if isinstance(target, (ast.Tuple, ast.List, ast.Starred)) else None
            return
        entry = self._entry_from_annotation(annotation) if annotation is not None else None
        if entry is None and value is not None:
            entry = self._target_of(value)
            if entry is None:
                e = self._elems_of(value)
                entry = ("elems", e) if e else None
        self.scopes[-1][target.id] = entry   # None shadows an outer binding

    def _bind_loop(self, target, it):
        if isinstance(target, ast.Name):
            self.scopes[-1][target.id] = self._elems_of(it)
            return
        self._forget(target)
        if isinstance(target, ast.Tuple) and len(target.elts) == 2 and isinstance(target.elts[1], ast.Name) \
                and isinstance(it, ast.Call):
            short = _last(_dotted(it.func))
            if short == "enumerate" and it.args:
                self.scopes[-1][target.elts[1].id] = self._elems_of(it.args[0])
            elif short == "items" and isinstance(it.func, ast.Attribute):
                self.scopes[-1][target.elts[1].id] = self._elems_of(it.func.value)

    def visit_For(self, node):
        self.visit(node.iter)
        self._bind_loop(node.target, node.iter)
        for stmt in node.body + node.orelse:
            self.visit(stmt)

    visit_AsyncFor = visit_For

    def _visit_comp(self, node):
        self.scopes.append({})
        for gen in node.generators:
            self.visit(gen.iter)
            self._bind_loop(gen.target, gen.iter)
            for cond in gen.ifs:
                self.visit(cond)
        for field in ("elt", "key", "value"):
            if getattr(node, field, None) is not None:
                self.visit(getattr(node, field))
        self.scopes.pop()

    visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _visit_comp

    def _narrowed(self, test):
        """{name: target} implied by `isinstance(x, T)` (possibly and-ed)."""
        out = {}
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
            for v in test.values:
                out.update(self._narrowed(v))
        elif isinstance(test, ast.Call) and _dotted(test.func) == "isinstance" and len(test.args) == 2 \
                and isinstance(test.args[0], ast.Name):
            cls = test.args[1]
            names = [_last(_dotted(e)) for e in cls.elts] if isinstance(cls, ast.Tuple) else [_last(_dotted(cls))]
            hits = [n for n in names if n in self.names]
            if len(hits) == 1 and len(names) == 1:
                out[test.args[0].id] = _tag(hits[0])
        return out

    def visit_If(self, node):
        self.visit(node.test)
        narrowed = self._narrowed(node.test)
        if narrowed:
            self.scopes.append(narrowed)
        for stmt in node.body:
            self.visit(stmt)
        if narrowed:
            self.scopes.pop()
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Assert(self, node):
        self.generic_visit(node)
        self.scopes[-1].update(self._narrowed(node.test))

    def visit_Assign(self, node):
        self.generic_visit(node)
        for t in node.targets:
            self._bind(t, node.value)
            self._check_store(t, node)

    def visit_AnnAssign(self, node):
        self.generic_visit(node)
        self._bind(node.target, node.value, node.annotation)
        if node.value is not None:
            self._check_store(node.target, node)

    def visit_AugAssign(self, node):
        self.generic_visit(node)
        self._check_store(node.target, node)

    # -- try / suppress
    def visit_Try(self, node):
        caught = set()
        bare = False
        for h in node.handlers:
            if h.type is None:
                bare = True
            elif isinstance(h.type, ast.Tuple):
                caught |= {_last(_dotted(e)) for e in h.type.elts}
            else:
                caught.add(_last(_dotted(h.type)))
        self.try_stack.append(None if bare else caught)
        for stmt in node.body:
            self.visit(stmt)
        self.try_stack.pop()
        for part in (node.handlers, node.orelse, node.finalbody):
            for stmt in part:
                self.visit(stmt)

    visit_TryStar = visit_Try

    def visit_With(self, node):
        caught = set()
        for item in node.items:
            ctx = item.context_expr
            if isinstance(ctx, ast.Call) and _last(_dotted(ctx.func)) == "suppress":
                caught |= {_last(_dotted(a)) for a in ctx.args}
        for item in node.items:
            self.visit(item)
        if caught:
            self.try_stack.append(caught)
        for stmt in node.body:
            self.visit(stmt)
        if caught:
            self.try_stack.pop()

    visit_AsyncWith = visit_With

    # -- patterns
    def _check_store(self, target, node):
        if not isinstance(target, ast.Attribute) or self._is_other(target.value):
            return
        subject = target.value
        # T.field = ... at runtime replaces the slot descriptor for every instance
        if isinstance(subject, ast.Name) and subject.id in self.names and not self.class_stack:
            root = subject.id if subject.id in self.targets else self.subclass_root[subject.id]
            if target.attr in self._target_fields(root):
                self._add(SILENT, "A04", node, "给类属性 %s.%s 赋值会替换 slot 描述符：已存在实例读这个字段都静默拿到"
                          "类上的值，之后新建实例才会抛 AttributeError（普通类不受影响）" % (subject.id, target.attr), root, True)
            return
        tgt = self._target_of(subject)
        if self._swallows("AttributeError"):
            if tgt is None or target.attr not in self._target_fields(tgt):
                self._add(SILENT, "A02", node, "属性赋值被 except/suppress(AttributeError) 包住：slots 对象上"
                          "未声明的属性赋值会被静默吞掉", tgt, tgt is not None)
        elif tgt and self._is_slotted(tgt) and target.attr not in self._target_fields(tgt) \
                and not target.attr.startswith("__"):
            self._add(LOUD, "L01", node, "%s 没有声明属性 %r：slots 化后赋值抛 AttributeError" % (tgt, target.attr),
                      tgt, True)

    def visit_Attribute(self, node):
        if node.attr == "__dict__" and isinstance(node.ctx, ast.Load) and not self._is_class_expr(node.value) \
                and not self._is_other(node.value):
            tgt = self._target_of(node.value)
            if self._swallows("AttributeError"):
                self._add(SILENT, "A01", node, "读取 obj.__dict__ 的异常被吞掉：slots 对象走异常分支，结果静默改变",
                          tgt, tgt is not None)
            else:
                self._add(LOUD, "L02", node, "obj.__dict__：slots 对象抛 AttributeError", tgt, tgt is not None)
        self.generic_visit(node)

    def visit_Call(self, node):
        func = _dotted(node.func)
        name = _last(func)
        args = node.args
        subject = args[0] if args else None
        if subject is not None and self._is_other(subject):
            self.generic_visit(node)
            return
        tgt = self._target_of(subject) if subject is not None else None

        if name == "getattr" and len(args) >= 2 and _const_str(args[1]) in ("__dict__", "__weakref__"):
            if len(args) == 3 or self._swallows("AttributeError"):
                self._add(SILENT, "A01", node, "getattr(obj, %r, 默认值)：slots 对象静默返回默认值"
                          "（例如序列化/日志得到空 dict）" % _const_str(args[1]), tgt, tgt is not None)
            elif not self._is_class_expr(subject):
                self._add(LOUD, "L02", node, "getattr(obj, %r)：slots 对象抛 AttributeError" % _const_str(args[1]),
                          tgt, tgt is not None)
        elif name == "hasattr" and len(args) == 2 and _const_str(args[1]) in ("__dict__", "__weakref__", "__slots__"):
            self._add(SILENT, "A01", node, "hasattr(obj, %r) 分支判断：slots 化后会走另一个分支"
                      % _const_str(args[1]), tgt, tgt is not None)
        elif func == "vars" and len(args) == 1 and not self._is_class_expr(subject):
            if self._swallows("TypeError"):
                self._add(SILENT, "A01", node, "vars(obj) 的 TypeError 被吞掉：slots 对象走异常分支", tgt,
                          tgt is not None)
            else:
                self._add(LOUD, "L02", node, "vars(obj)：slots 对象抛 TypeError", tgt, tgt is not None)
        elif name in _WEAKREF_FUNCS and (func.startswith("weakref.") or func == name) and subject is not None:
            if not self._target_weakref_ok(tgt):
                if self._swallows("TypeError"):
                    self._add(SILENT, "A03", node, "弱引用失败被吞掉：slots 对象（无 __weakref__）会走降级分支，"
                              "常见是改存强引用 -> 对象无法释放，内存不降反升", tgt, tgt is not None)
                else:
                    self._add(LOUD, "L03", node, "弱引用：无 __weakref__ 槽的 slots 对象抛 TypeError"
                              "（dataclass 用 weakref_slot=True 解决）", tgt, tgt is not None)
        elif name in _WEAKREF_CONTAINERS and (func.startswith("weakref.") or func == name):
            self._add(INFO, "I03", node, "%s：放入其中的对象必须支持弱引用；若会放整改类实例，确认有 __weakref__ 槽"
                      % name)
        elif name == "setattr" and len(args) >= 2 and isinstance(subject, ast.Name) \
                and (subject.id in self.targets or subject.id in self.subclass_root):
            self._check_patch_object(node, func)
        elif name == "setattr" and len(args) >= 2 and self._swallows("AttributeError"):
            self._add(SILENT, "A02", node, "setattr 被 except/suppress(AttributeError) 包住：未声明属性的赋值"
                      "会被静默吞掉", tgt, tgt is not None)
        elif name == "setattr" and len(args) >= 2 and tgt and self._is_slotted(tgt) and _const_str(args[1]) \
                and _const_str(args[1]) not in self._target_fields(tgt):
            self._add(LOUD, "L01", node, "setattr 给 %s 未声明的属性 %r" % (tgt, _const_str(args[1])), tgt, True)
        elif (name == "object" or func.endswith("patch.object") or name == "setattr" and "monkeypatch" in func):
            self._check_patch_object(node, func)
        elif name == "patch" and args and _const_str(args[0]):
            parts = _const_str(args[0]).split(".")
            if len(parts) >= 2:
                cls = parts[-2]
                root = cls if cls in self.targets else self.subclass_root.get(cls)
                if root and parts[-1] in self._target_fields(root):
                    self._add(SILENT, "A04", node, "patch(%r) 替换了 slot 字段描述符：patch 期间所有已存在实例"
                              "读到的都是 patch 值" % _const_str(args[0]), root, True)
        if len(func.split(".")) >= 2 and tuple(func.split(".")[-2:]) in _PICKLE_LOADS:
            self._add(INFO, "I01", node, "反序列化：整改前保存的 pickle（缓存/checkpoint/队列消息）"
                      "用 slots 类加载会抛 AttributeError，测试环境通常没有这些旧文件")
        self.generic_visit(node)

    def _check_patch_object(self, node, func):
        args = node.args
        if len(args) < 2 or not isinstance(args[0], ast.Name):
            return
        cls = args[0].id
        root = cls if cls in self.targets else self.subclass_root.get(cls)
        attr = _const_str(args[1])
        if root and attr and attr in self._target_fields(root) and attr not in self._class_members(root):
            self._add(SILENT, "A04", node, "patch %s.%s：替换了 slot 字段描述符，patch 期间所有已存在实例"
                      "读到的都是 patch 值（普通 dataclass 上这样 patch 会直接报错）" % (cls, attr), root, True)

    def _class_members(self, target):
        names = set()
        for info in self.classes_info.get(target, ()):
            names |= info.members
        return names


# --------------------------------------------------------------------------- class checks
def _class_findings(index, targets, lines_of, py=sys.version_info[:2]):
    out = []

    def snip(info, line=None):
        try:
            return lines_of[info.path][(line or info.line) - 1].strip()
        except (KeyError, IndexError):
            return ""

    def method_nodes(info):
        for stmt in info.node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield stmt

    for name in sorted(targets):
        defs = index.by_name.get(name)
        if not defs:
            out.append(Finding(INFO, "I00", "-", 0, "没找到类 %s 的定义（拼写？或定义不在扫描目录内）" % name,
                               target=name))
            continue
        for info in defs:
            if not info.own_slotted:
                why = "__slots__ 里含 '__dict__'" if isinstance(info.explicit_slots, set) else \
                    "既没有 __slots__ 也没有 dataclass(slots=True)"
                out.append(Finding(SILENT, "S01", info.path, info.line, "%s 实际没有 slots 化（%s），内存不会下降"
                                   % (name, why), snip(info), target=name))
            bad, unknown = index.chain_problems(info)
            for anc in bad:
                out.append(Finding(SILENT, "S02", info.path, info.line,
                                   "父类 %s（%s:%d）没有 __slots__：%s 的实例仍带 __dict__，内存优化静默失效"
                                   % (anc.name, anc.path, anc.line, name), snip(info), target=name))
            for base in unknown:
                out.append(Finding(INFO, "I02", info.path, info.line,
                                   "父类 %s 不在扫描范围，无法确认是否 slots 化；运行时用 "
                                   "assert not hasattr(obj, '__dict__') 验证" % base, snip(info), target=name))
            if info.dataclass_slots and py < (3, 10):
                out.append(Finding(LOUD, "L07", info.path, info.line,
                                   "Python %d.%d 的 dataclass 没有 slots 参数：类定义时抛 TypeError。3.9 可改为在类体里"
                                   "手写 __slots__（字段不能带默认值），或用 attrs 的 @define" % py,
                                   snip(info), target=name))
            elif info.dataclass_weakref_kw and py < (3, 11):
                out.append(Finding(LOUD, "L07", info.path, info.line,
                                   "Python %d.%d 的 dataclass 没有 weakref_slot 参数（3.11+）：类定义时抛 TypeError；"
                                   "改为手写 __slots__ 并包含 '__weakref__'" % py, snip(info), target=name))
            if info.dataclass_slots and info.explicit_slots is not None:
                out.append(Finding(LOUD, "L04", info.path, info.line,
                                   "同时写了 __slots__ 和 dataclass(slots=True)：类定义时抛 TypeError",
                                   snip(info), target=name))
            if isinstance(info.explicit_slots, set) and not info.dataclass_slots:
                for field, line in info.annotated_with_default.items():
                    if field in info.explicit_slots:
                        out.append(Finding(LOUD, "L04", info.path, line,
                                           "手写 __slots__ 的字段 %r 带默认值（含 field(...)）：类定义时抛 ValueError（%s）"
                                           % (field, "去掉默认值，或改用 attrs 的 @define" if py < (3, 10)
                                              else "改用 dataclass(slots=True)"), snip(info, line), target=name))
            no_dict = info.own_slotted
            for fn in info.cached_properties:
                if no_dict:
                    out.append(Finding(LOUD, "L05", info.path, fn.lineno,
                                       "cached_property 需要实例 __dict__：首次访问抛 TypeError",
                                       snip(info, fn.lineno), target=name))
            # dataclass(slots=True) re-creates the class; zero-arg super()/__class__ still see the
            # old one. Measured: broken on 3.10-3.12 and 3.13.0, fixed by 3.13.16.
            if info.dataclass_slots and (3, 10) <= py < (3, 14):
                for fn in method_nodes(info):
                    for sub in ast.walk(fn):
                        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "super" \
                                and not sub.args:
                            out.append(Finding(LOUD, "L06", info.path, sub.lineno,
                                               "dataclass(slots=True) 里无参 super()：Python 3.10–3.12 及 3.13 早期补丁版本"
                                               "（3.13.0 实测受影响，3.13.16 已修复）抛 TypeError，改成 super(%s, self)" % name, snip(info, sub.lineno), target=name))
                        elif isinstance(sub, ast.Name) and sub.id == "__class__":
                            out.append(Finding(SILENT, "A05", info.path, sub.lineno,
                                               "dataclass(slots=True) 会重新创建类，Python 3.10–3.12 及 3.13 早期补丁版本上"
                                               "方法里的 __class__ 仍指向旧类：isinstance/is 比较静默得到 False"
                                               "（3.13.0 实测受影响，3.13.16 已修复）", snip(info, sub.lineno),
                                               target=name))
    for info, root in index.descendants(targets):
        if info.name in targets:
            continue
        if not info.own_slotted:
            out.append(Finding(SILENT, "S03", info.path, info.line,
                               "子类 %s 没有 slots 化：它的实例仍带 __dict__，内存不会下降，也不会报错"
                               "（dataclass 子类同样要写 slots=True）" % info.name,
                               lines_of.get(info.path, [""] * info.line)[info.line - 1].strip(), target=root))
    return out


# --------------------------------------------------------------------------- driver
def _iter_py(paths, exclude):
    for root in paths:
        if os.path.isfile(root):
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in exclude and not d.endswith(".egg-info"))
            for f in sorted(filenames):
                if f.endswith(".py"):
                    yield os.path.join(dirpath, f)


def _parse(path):
    with open(path, "rb") as f:
        src = f.read().decode("utf-8", "replace")
    return src, ast.parse(src, filename=path)


def audit(paths, classes, also=(), exclude=_EXCLUDE_DIRS, python=None):
    """Return (findings, stats). `also` = extra trees (e.g. third-party packages) scanned for usages only."""
    targets = set(classes)
    index, trees, lines_of, findings = _Index(), {}, {}, []
    own = list(_iter_py(paths, exclude))
    extra = list(_iter_py(also, set(exclude) - {"site-packages"}))
    for path in own + extra:
        rel = os.path.relpath(path)
        try:
            src, tree = _parse(path)
        except (SyntaxError, ValueError, OSError) as exc:
            findings.append(Finding(INFO, "I09", rel, getattr(exc, "lineno", 0) or 0,
                                    "无法解析，已跳过：%s" % exc))
            continue
        trees[rel] = (tree, path in extra)
        lines_of[rel] = src.splitlines()
        if path not in extra:
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    index.add(ClassInfo(node, rel))
    py = tuple(python or sys.version_info[:2])
    findings += _class_findings(index, targets, lines_of, py)
    subclass_root = {info.name: root for info, root in index.descendants(targets) if info.name not in targets}
    mention = re.compile(r"\b(%s)\b" % "|".join(map(re.escape, sorted(targets | set(subclass_root))))) \
        if targets else None
    classes_info = {t: index.by_name.get(t, []) for t in targets}
    for sub, root in subclass_root.items():
        classes_info.setdefault(sub, index.by_name.get(sub, []))
    names = targets | set(subclass_root)
    maps = _type_maps(trees, names)
    ancestors = {a.name for t in targets for info in index.by_name.get(t, ()) for a in index.ancestors(info)}
    for rel, (tree, _is_extra) in trees.items():
        related = bool(mention and mention.search("\n".join(lines_of[rel])))
        v = _UsageVisitor(rel, lines_of[rel], targets, related, index, classes_info)
        v.subclass_root, v.names, v.maps = subclass_root, names, maps
        v.target_ancestors = ancestors
        try:
            v.visit(tree)
        except (RecursionError, ValueError, TypeError, AttributeError, IndexError, KeyError) as exc:
            v.findings.append(Finding(INFO, "I09", rel, 0, "分析中断，该文件结果可能不完整：%s: %s"
                                      % (type(exc).__name__, exc)))
        findings += v.findings
    rank = {"confirmed": 0, "inferred": 1, "possible": 2}
    findings.sort(key=lambda f: (_ORDER[f["severity"]], rank.get(f["confidence"], 3), f["path"], f["line"]))
    stats = {"files": len(trees), "own_files": len(own), "extra_files": len(extra),
             "targets": sorted(targets), "subclasses": sorted(subclass_root), "python": "%d.%d" % py,
             "subclasses_of": {t: sorted(n for n, r in subclass_root.items() if r == t) for t in targets}}
    return findings, stats


_TITLES = {SILENT: "SILENT 不报错，但行为或内存效果会改变",
           LOUD: "LOUD   执行到会抛异常",
           INFO: "INFO   需要人工确认"}


def is_specific(f):
    """Finding that is tied to one of the converted classes (not a generic, type-unknown pattern)."""
    return f.get("target") is not None and not f["confidence"].startswith("possible")


def select(findings, show_all=False, related_only=False):
    if not show_all:
        return [f for f in findings if is_specific(f) or f["code"] == "I09"]
    return [f for f in findings if not (related_only and f["confidence"].startswith("possible ("))]


def _emit(out, f, indent="  "):
    loc = "%s:%d" % (f["path"], f["line"]) if f["line"] else f["path"]
    tags = [] if f["confidence"] == "confirmed" else [f["confidence"]]
    if f.get("via"):
        tags.append("经子类 %s" % f["via"])
    out.append("%s[%s] %s%s" % (indent, f["code"], loc, "  (%s)" % "; ".join(tags) if tags else ""))
    out.append("%s      %s" % (indent, f["message"]))
    if f["snippet"]:
        out.append("%s      | %s" % (indent, f["snippet"]))


def format_text(findings, stats, show_all=False, related_only=False):
    shown = select(findings, show_all, related_only)
    out = ["slots_audit: Python %s；扫描 %d 个文件（额外目录 %d 个）" % (
        stats["python"], stats["files"], stats["extra_files"])]
    subs = stats.get("subclasses_of", {})
    for target in stats["targets"]:
        mine = [f for f in shown if f.get("target") == target]
        out.append("")
        out.append("### %s%s" % (target, "（子类：%s）" % ", ".join(subs[target]) if subs.get(target) else ""))
        if not mine:
            out.append("  未发现与该类相关的风险")
        for sev in (SILENT, LOUD, INFO):
            items = [f for f in mine if f["severity"] == sev]
            if items:
                out.append("  -- %s [%d]" % (_TITLES[sev], len(items)))
                for f in items:
                    _emit(out, f, "  ")
    rest = [f for f in shown if f.get("target") not in stats["targets"]]
    if rest:
        out.append("")
        out.append("### 类型无法确定的通用命中（可能涉及任何对象）" if show_all else "### 其他")
        for sev in (SILENT, LOUD, INFO):
            for f in (x for x in rest if x["severity"] == sev):
                f2 = dict(f)
                f2["code"] = "%s %s" % (sev, f["code"])
                _emit(out, f2, "  ")
    shown_ids = {id(f) for f in shown}
    hidden = [f for f in findings if id(f) not in shown_ids]
    out.append("")
    counts = {sev: sum(1 for f in shown if f["severity"] == sev) for sev in (SILENT, LOUD, INFO)}
    out.append("合计：SILENT %d，LOUD %d，INFO %d" % (counts[SILENT], counts[LOUD], counts[INFO]))
    if hidden:
        by_code = {}
        for f in hidden:
            by_code[f["code"]] = by_code.get(f["code"], 0) + 1
        out.append("另有 %d 条通用命中未显示（对象类型无法确定，可能是任何类：%s）。加 --all 查看；"
                   "确认是整改类的对象后，给变量/参数/返回值加类型注解即可被归到对应类下。"
                   % (len(hidden), "，".join("%s×%d" % kv for kv in sorted(by_code.items()))))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="slots_audit", description="Find code that __slots__ conversion breaks.")
    ap.add_argument("paths", nargs="+", help="source directories/files (your code and tests)")
    ap.add_argument("--classes", "-c", required=True, help="comma-separated class names you converted")
    ap.add_argument("--also", action="append", default=[], metavar="DIR",
                    help="extra trees scanned for usages only, e.g. a third-party package (repeatable)")
    ap.add_argument("--exclude", action="append", default=[], metavar="DIRNAME", help="extra directory names to skip")
    ap.add_argument("--all", action="store_true",
                    help="also list generic findings whose object type is unknown (may involve any class)")
    ap.add_argument("--related-only", action="store_true",
                    help="with --all: skip generic findings in files that never mention the target classes")
    ap.add_argument("--json", metavar="FILE", help="also write findings as JSON")
    ap.add_argument("--python", metavar="X.Y", help="Python version the code runs on (default: this interpreter)")
    args = ap.parse_args(argv)
    classes = [c.strip() for c in args.classes.split(",") if c.strip()]
    try:
        python = tuple(int(x) for x in args.python.split(".")[:2]) if args.python else None
    except ValueError:
        ap.error("--python expects X.Y, e.g. 3.9")
    findings, stats = audit(args.paths, classes, args.also, _EXCLUDE_DIRS | set(args.exclude), python)
    print(format_text(findings, stats, args.all, args.related_only))
    shown = select(findings, args.all, args.related_only)
    if args.json:
        for f in findings:
            f["shown"] = any(f is g for g in shown)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"stats": stats, "findings": findings}, fh, ensure_ascii=False, indent=1)
    return 1 if any(f["severity"] == SILENT for f in shown) else 0


if __name__ == "__main__":
    sys.exit(main())
