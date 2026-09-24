"""Mechanical, LLM-free parser for TypeScript and JavaScript, built on
tree-sitter -- the counterpart of kg/python_ast_parser.py (Python's own `ast`
module can't read other languages, so this is the "clean way to add another
language" that README's Extending section called for: one library, a grammar
per language, a thin adapter onto the shared shapes in kg/code_model.py -- not
a hand-written parser, and not an LLM doing structural extraction).

What becomes what (so the rest of the app needs to know nothing about TS):

  File     -- one per .ts/.tsx/.js/.jsx/.mts/.cts/.mjs/.cjs file
  Class    -- `class` declarations (and `const X = class {}`); bases = extends + implements
  Function -- top-level functions, AND arrow/function expressions bound to a
              top-level const (`export const Button = () => ...` -- the
              dominant form in React code), AND class methods (including
              arrow-function class fields, constructors, getters/setters)
  IMPORTS  -- relative imports, tsconfig/jsconfig `paths` + `baseUrl` aliases
              (`@/components/x`), and workspace packages in a monorepo;
              `import`, `export ... from`, `require()`, and dynamic `import()`
  CALLS    -- best-effort, same spirit as the Python parser: direct calls,
              `this.method()`, calls through an import (including through
              `index.ts` barrel re-exports), `Namespace.fn()`, static
              `Class.method()`, `new Class()` (-> its constructor), and JSX
              tags (`<Button/>` -> the Button component -- without this every
              React component would look unused)

Deliberately not modeled (yet): interfaces, type aliases and enums (they carry
no behavior, and as Class nodes they'd flood the "empty class" finding),
`namespace` blocks, object-literal methods, and instance calls on a variable
of unknown type (`svc.run()`) -- the same class of limit the Python parser has.
Tree-sitter is error-tolerant, so a file with a syntax error is still mined for
whatever parsed rather than being skipped whole.

Memory: each file's syntax tree is discarded as soon as its symbols and raw
call sites are extracted (call sites are kept as tiny tuples and resolved
against the repo-wide symbol table afterwards), so a large repo never holds
thousands of trees at once -- this runs on a 512MB machine.
"""
from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from .code_model import CodeClass, CodeFunction, CodeModule, ParsedRepo, iter_files
from .python_ast_parser import _IGNORED_DIR_NAMES

TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_TS_GRAMMAR_SUFFIXES = (".ts", ".mts", ".cts")  # everything else uses the TSX grammar (JSX-capable, and it parses plain JS)
_TYPESCRIPT_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")

# On top of the Python parser's ignore list (.git, node_modules, dist, build, ...):
# framework build output and coverage reports -- generated JS that would
# otherwise dominate the graph. Applied to TS/JS files only, so Python
# discovery is exactly what it was before.
EXTRA_IGNORED_DIRS = frozenset({
    ".next", ".nuxt", ".turbo", ".svelte-kit", ".angular", ".expo", ".output", ".vercel", ".parcel-cache",
    ".yarn", ".pnpm-store", "storybook-static", "bower_components", "lcov-report",
})
IGNORED_DIRS = frozenset(_IGNORED_DIR_NAMES) | EXTRA_IGNORED_DIRS

# Third-party libraries checked in as *JavaScript* (jquery.js under vendor/) are
# noise -- but vendored *TypeScript* is source somebody imports: tRPC's own
# `src/vendor/unpromise` is first-party code, and skipping it left every import
# of it dangling (found by measuring unresolved imports on that repo). So this
# rule is by file type, not by directory alone.
_VENDOR_DIRS = frozenset({"vendor", "third_party"})
_JS_ONLY_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs")


def is_ignored_path(rel: Path) -> bool:
    """Whether a repo-relative TS/JS path sits somewhere the parser skips."""
    if any(part in IGNORED_DIRS for part in rel.parts):
        return True
    return rel.name.lower().endswith(_JS_ONLY_SUFFIXES) and any(part in _VENDOR_DIRS for part in rel.parts)


# JSX attributes whose string value is text a person actually sees or hears.
_UI_TEXT_ATTRS = {"placeholder", "aria-label", "title", "alt", "label"}
_UI_TEXT_PER_FILE = 15   # a marketing page could otherwise dump hundreds of strings into an LLM prompt
_UI_TEXT_MAX_LEN = 60    # longer is body copy, not a label a test step would reference

MAX_FILE_BYTES = 1_000_000  # bigger than this is a generated bundle, not source someone reads

_TRY_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
# `import './x.js'` in an ESM TypeScript project really means x.ts
_JS_TO_TS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
_COMPILED_TWIN = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}

_FN_EXPRS = {"arrow_function", "function_expression", "function", "generator_function"}
_WRAPPERS = {"parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression", "type_assertion"}
_FN_DECLS = {"function_declaration", "generator_function_declaration"}
_CLASS_DECLS = {"class_declaration", "abstract_class_declaration"}


def is_ts_source(name: str) -> bool:
    """A TypeScript/JavaScript file worth parsing: right extension, and not a
    declaration file (`.d.ts` has no implementation) or minified bundle."""
    n = name.lower()
    return n.endswith(TS_SUFFIXES) and not n.endswith((".d.ts", ".d.mts", ".d.cts")) and ".min." not in n


def language_for(name: str) -> str:
    return "typescript" if name.lower().endswith(_TYPESCRIPT_SUFFIXES) else "javascript"


# --------------------------------------------------------------------------- tree-sitter plumbing

_PARSERS: dict[str, object] | None = None


def _get_parsers() -> dict[str, object]:
    global _PARSERS
    if _PARSERS is None:
        try:
            import tree_sitter_typescript as tst
            from tree_sitter import Language, Parser
        except ImportError as e:
            raise RuntimeError(
                "This repo has TypeScript/JavaScript files, but tree-sitter isn't installed -- "
                "run `pip install -r requirements.txt`."
            ) from e
        _PARSERS = {
            "ts": Parser(Language(tst.language_typescript())),
            "tsx": Parser(Language(tst.language_tsx())),
        }
    return _PARSERS


def _t(node) -> str:
    return node.text.decode("utf-8", "replace")


def _walk(node):
    """Pre-order traversal without recursion -- deeply nested TS (long chained
    calls, big JSX trees) would otherwise hit Python's recursion limit."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _string_value(node) -> str | None:
    if node is None or node.type != "string":
        return None
    value = "".join(_t(c) for c in node.named_children if c.type == "string_fragment")
    return value or None


def _unwrap(node):
    while node is not None and node.type in _WRAPPERS:
        named = node.named_children
        if not named:
            break
        node = named[0]
    return node


def _function_like(value):
    """`value` if it's (or wraps) a function -- an arrow/function expression, or
    a call like `memo(() => ...)` / `forwardRef((p, r) => ...)` /
    `createHandler(async () => ...)` whose first argument is one. Returns the
    node to scan for calls, or None."""
    v = _unwrap(value)
    if v is None:
        return None
    if v.type in _FN_EXPRS:
        return v
    if v.type == "call_expression":
        args = v.child_by_field_name("arguments")
        if args is not None:
            for a in args.named_children[:1]:
                inner = _unwrap(a)
                if inner is not None and inner.type in _FN_EXPRS:
                    return v
    return None


def _ui_string(node) -> str | None:
    """The human-readable text of a JSX text node / string literal, or None if
    it's whitespace, punctuation, too long to be a label, or doesn't read as
    copy -- a single lowercase token ("flex", "cart.title", "/cart", "my-class")
    is a CSS class, an i18n key or a path, not something a user reads."""
    raw = _string_value(node) if node.type == "string" else _t(node)
    text = " ".join((raw or "").split())
    if not (2 <= len(text) <= _UI_TEXT_MAX_LEN) or not any(c.isalpha() for c in text):
        return None
    if " " not in text and not (text[0].isupper() and text.replace("'", "").isalpha()):
        return None  # a phrase, or a capitalised single word ("Checkout"), reads as copy; anything else is code
    return text


_COPY_CONTAINERS = {
    "jsx_expression", "parenthesized_expression", "binary_expression",
    "as_expression", "satisfies_expression", "non_null_expression",
}


def _strings_in(node) -> list[str]:
    """Readable strings a JSX expression can *render* -- a bare literal, either
    arm of a ternary, the right side of `&&`/`||`: `{pending ? <Spinner/> : 'Proceed to
    Checkout'}`. Deliberately follows only those branches, never into a call or
    an attribute: `{items.map(i => <li className="flex items-center">...)}` has
    class names inside it, and a first version that walked everything leaked them
    into the UI text (caught by checking the real storefront's cart)."""
    out: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "string":
            text = _ui_string(n)
            if text:
                out.append(text)
        elif n.type == "ternary_expression":  # the condition holds code, only the arms can render
            stack.extend(c for c in (n.child_by_field_name("consequence"), n.child_by_field_name("alternative")) if c is not None)
        elif n.type in _COPY_CONTAINERS:
            stack.extend(n.named_children)
    return out


def _describe_callee(callee, kind: str):
    """Reduces a call/new/JSX callee to a small tuple resolvable later, without
    keeping the syntax tree alive: (kind, object, name) where object is None
    (plain identifier), "this", or an identifier's text."""
    if callee is None:
        return None
    if callee.type == "identifier":
        name = _t(callee)
        if kind == "jsx" and not name[:1].isupper():
            return None  # <div/> is an HTML element, not a component
        return (kind, None, name)
    if callee.type == "member_expression":
        obj = callee.child_by_field_name("object")
        prop = callee.child_by_field_name("property")
        if obj is None or prop is None:
            return None
        if obj.type == "this":
            return (kind, "this", _t(prop))
        if obj.type == "identifier":
            oname = _t(obj)
            if kind == "jsx" and not oname[:1].isupper():
                return None
            return (kind, oname, _t(prop))
    return None


def _collect_calls(fn_node) -> list[tuple]:
    out = []
    for n in _walk(fn_node):
        t = n.type
        if t == "call_expression":
            d = _describe_callee(n.child_by_field_name("function"), "call")
        elif t == "new_expression":
            d = _describe_callee(n.child_by_field_name("constructor"), "new")
        elif t in ("jsx_opening_element", "jsx_self_closing_element"):
            d = _describe_callee(n.child_by_field_name("name"), "jsx")
        else:
            continue
        if d:
            out.append(d)
    return out


# --------------------------------------------------------------------------- per-file extraction

@dataclass
class _Info:
    """Everything pass 1 learns about one file -- no tree nodes, so the tree can be freed."""
    module: CodeModule
    rel: str
    symbols: dict[str, str] = field(default_factory=dict)             # top-level name -> qualname ("default" too)
    methods_by_class: dict[str, set[str]] = field(default_factory=dict)
    specs: set[str] = field(default_factory=set)                       # every import specifier seen, any form
    bindings: dict[str, tuple] = field(default_factory=dict)           # local name -> (kind, spec, original)
    exports_alias: dict[str, str] = field(default_factory=dict)        # `export { a as b }` -> b: a
    reexports: dict[str, tuple[str, str]] = field(default_factory=dict)  # `export { a as b } from 'x'` -> b: (x, a)
    star_reexports: list[str] = field(default_factory=list)            # `export * from 'x'`
    default_local: str | None = None                                   # `export default Foo;`
    raw_calls: dict[str, list[tuple]] = field(default_factory=dict)
    resolved: dict[str, str | None] = field(default_factory=dict)      # spec -> dotted module in this repo, if any


class _Extractor:
    def __init__(self, info: _Info):
        self.info = info
        self.dotted = info.module.dotted_name
        self._func_nodes: dict[str, object] = {}
        self._fn_quals: set[str] = set()

    # ---- entry point
    def run(self, root) -> None:
        for stmt in root.named_children:
            self._statement(stmt)
        # require('x') and import('x') can sit anywhere, not just at the top level.
        # The same walk collects the text a user can see (button labels, headings,
        # placeholders): functional test cases are written from the user's side of
        # the screen, and without this the model can only guess at what's on it.
        ui_text: list[str] = []

        def add_text(texts) -> None:
            for text in texts:
                if text not in ui_text:
                    ui_text.append(text)

        stack = [root]
        while stack:
            n = stack.pop()
            t = n.type
            if t == "jsx_attribute":
                # only attributes that carry copy (placeholder, aria-label, ...) are read; className,
                # handlers, styles etc. hold code, so their whole subtree is skipped
                name = next((c for c in n.children if c.type == "property_identifier"), None)
                if name is not None and _t(name) in _UI_TEXT_ATTRS:
                    for value in n.children:  # `label="x"` (a string) or `label={cond ? 'a' : 'b'}` (an expression)
                        if value.type in ("string", "jsx_expression"):
                            add_text(_strings_in(value))
                continue
            if t == "jsx_text":
                text = _ui_string(n)
                if text:
                    add_text([text])
                continue
            if t == "jsx_expression" and n.parent is not None and n.parent.type in ("jsx_element", "jsx_fragment"):
                add_text(_strings_in(n))  # a child expression: {"Sign in"}, {busy ? <Spinner/> : 'Save'}, {empty && 'Nothing here'}
            elif t == "call_expression":
                fn = n.child_by_field_name("function")
                if fn is not None and (fn.type == "import" or (fn.type == "identifier" and _t(fn) == "require")):
                    args = n.child_by_field_name("arguments")
                    spec = _string_value(args.named_children[0]) if args is not None and args.named_children else None
                    if spec:
                        self.info.specs.add(spec)
            stack.extend(reversed(n.children))
        self.info.module.ui_text = ui_text[:_UI_TEXT_PER_FILE]
        for qual, node in self._func_nodes.items():
            self.info.raw_calls[qual] = _collect_calls(node)
        self._func_nodes.clear()

    # ---- statements
    def _statement(self, stmt, default_name: str | None = None) -> str | None:
        """Handles one top-level statement/declaration. Returns the local name
        it defined (if any), so `export default <declaration>` can alias it."""
        t = stmt.type
        if t == "export_statement":
            self._export(stmt)
        elif t in _FN_DECLS:
            name = stmt.child_by_field_name("name")
            return self._add_function(_t(name) if name is not None else (default_name or "default"), stmt)
        elif t in _CLASS_DECLS or t == "class":
            name = stmt.child_by_field_name("name")
            return self._add_class(_t(name) if name is not None else (default_name or "default"), stmt)
        elif t in ("lexical_declaration", "variable_declaration"):
            return self._variables(stmt)
        elif t == "import_statement":
            self._import(stmt)
        elif t == "expression_statement":
            self._commonjs_export(stmt)
        return None

    def _add_function(self, name: str, node) -> str:
        qual = f"{self.dotted}:{name}"
        if qual not in self._fn_quals:  # overload signatures / redeclarations define the same name once
            self._fn_quals.add(qual)
            self.info.module.functions.append(
                CodeFunction(name=name, qualname=qual, is_method=False, class_qualname=None,
                             lineno=node.start_point[0] + 1)
            )
            self._func_nodes[qual] = node
        self.info.symbols[name] = qual
        return name

    def _add_class(self, name: str, node) -> str:
        cls_qual = f"{self.dotted}:{name}"
        if cls_qual in self.info.methods_by_class:
            self.info.symbols[name] = cls_qual
            return name
        bases: list[str] = []
        for child in node.named_children:
            if child.type == "class_heritage":
                for clause in child.named_children:
                    if clause.type == "extends_clause":
                        bases += [_t(v) for v in clause.children_by_field_name("value")]
                    elif clause.type == "implements_clause":
                        bases += [_t(v) for v in clause.named_children]
        cls = CodeClass(name=name, qualname=cls_qual, bases=bases, lineno=node.start_point[0] + 1)
        method_names: set[str] = set()
        body = node.child_by_field_name("body")
        for member in (body.named_children if body is not None else []):
            mname = None
            if member.type == "method_definition":
                mname = self._member_name(member)
            elif member.type == "public_field_definition":  # `handle = () => {...}` -- a method in all but syntax
                value = _unwrap(member.child_by_field_name("value"))
                if value is not None and value.type in _FN_EXPRS:
                    mname = self._member_name(member)
            if not mname:
                continue
            method_qual = f"{self.dotted}:{name}.{mname}"
            if method_qual in cls.methods:  # getter/setter pairs share a name
                continue
            cls.methods.append(method_qual)
            method_names.add(mname)
            self._fn_quals.add(method_qual)
            self.info.module.functions.append(
                CodeFunction(name=mname, qualname=method_qual, is_method=True, class_qualname=cls_qual,
                             lineno=member.start_point[0] + 1)
            )
            self._func_nodes[method_qual] = member
        self.info.module.classes.append(cls)
        self.info.methods_by_class[cls_qual] = method_names
        self.info.symbols[name] = cls_qual
        return name

    @staticmethod
    def _member_name(member) -> str | None:
        name = member.child_by_field_name("name")
        if name is None:
            return None
        return _string_value(name) if name.type == "string" else _t(name)

    def _variables(self, stmt) -> str | None:
        last = None
        for decl in stmt.named_children:
            if decl.type != "variable_declarator":
                continue
            nm = decl.child_by_field_name("name")
            val = decl.child_by_field_name("value")
            if nm is None or val is None:
                continue
            if self._require_binding(nm, val):
                continue
            if nm.type != "identifier":
                continue  # destructuring -- no single name to give a Function node
            name = _t(nm)
            fn = _function_like(val)
            if fn is not None:
                last = self._add_function(name, fn)
            elif _unwrap(val).type == "class":
                last = self._add_class(name, _unwrap(val))
        return last

    def _require_binding(self, nm, val) -> bool:
        call = _unwrap(val)
        if call is None or call.type != "call_expression":
            return False
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "identifier" or _t(fn) != "require":
            return False
        args = call.child_by_field_name("arguments")
        spec = _string_value(args.named_children[0]) if args is not None and args.named_children else None
        if not spec:
            return False
        self.info.specs.add(spec)
        if nm.type == "identifier":
            self.info.bindings[_t(nm)] = ("ns", spec, None)
        elif nm.type == "object_pattern":
            for p in nm.named_children:
                if p.type == "shorthand_property_identifier_pattern":
                    self.info.bindings[_t(p)] = ("named", spec, _t(p))
                elif p.type == "pair_pattern":
                    key, value = p.child_by_field_name("key"), p.child_by_field_name("value")
                    if key is not None and value is not None and value.type == "identifier":
                        self.info.bindings[_t(value)] = ("named", spec, _t(key))
        return True

    def _commonjs_export(self, stmt) -> None:
        """`exports.foo = () => {}` / `module.exports.foo = function () {}` /
        `module.exports = function name() {}` -- the CommonJS spelling of an export."""
        assign = next((c for c in stmt.named_children if c.type == "assignment_expression"), None)
        if assign is None:
            return
        left, right = assign.child_by_field_name("left"), assign.child_by_field_name("right")
        if left is None or right is None or left.type != "member_expression":
            return
        obj, prop = left.child_by_field_name("object"), left.child_by_field_name("property")
        if obj is None or prop is None:
            return
        target = _unwrap(right)
        if obj.type == "identifier" and _t(obj) == "exports":
            name = _t(prop)
        elif obj.type == "member_expression" and _t(obj) == "module.exports":
            name = _t(prop)
        elif obj.type == "identifier" and _t(obj) == "module" and _t(prop) == "exports":
            named = target.child_by_field_name("name") if target is not None else None
            name = _t(named) if named is not None else "default"
            if target is not None and target.type in _FN_EXPRS:
                self._add_function(name, target)
                self.info.symbols["default"] = self.info.symbols[name]
            elif target is not None and target.type == "class":
                self._add_class(name, target)
                self.info.symbols["default"] = self.info.symbols[name]
            return
        else:
            return
        fn = _function_like(right)
        if fn is not None:
            self._add_function(name, fn)

    # ---- imports / exports
    def _import(self, stmt) -> None:
        src = stmt.child_by_field_name("source")
        if src is None:  # `import x = require('y')`
            for c in stmt.named_children:
                if c.type == "import_require_clause":
                    spec = _string_value(c.child_by_field_name("source"))
                    ident = next((x for x in c.named_children if x.type == "identifier"), None)
                    if spec:
                        self.info.specs.add(spec)
                        if ident is not None:
                            self.info.bindings[_t(ident)] = ("ns", spec, None)
            return
        spec = _string_value(src)
        if not spec:
            return
        self.info.specs.add(spec)
        for c in stmt.named_children:
            if c.type != "import_clause":
                continue
            for x in c.named_children:
                if x.type == "identifier":
                    self.info.bindings[_t(x)] = ("default", spec, None)
                elif x.type == "named_imports":
                    for sp in x.named_children:
                        if sp.type != "import_specifier":
                            continue
                        nm, al = sp.child_by_field_name("name"), sp.child_by_field_name("alias")
                        if nm is not None:
                            orig = _t(nm)
                            self.info.bindings[_t(al) if al is not None else orig] = ("named", spec, orig)
                elif x.type == "namespace_import":
                    ident = next((y for y in x.named_children if y.type == "identifier"), None)
                    if ident is not None:
                        self.info.bindings[_t(ident)] = ("ns", spec, None)

    def _export(self, stmt) -> None:
        is_default = any(c.type == "default" for c in stmt.children)
        decl = stmt.child_by_field_name("declaration")
        value = stmt.child_by_field_name("value")
        source = stmt.child_by_field_name("source")

        if decl is not None:
            name = self._statement(decl, default_name="default" if is_default else None)
            if is_default and name and name in self.info.symbols:
                self.info.symbols["default"] = self.info.symbols[name]
            return

        if source is not None:  # re-exports: `export { a as b } from 'x'` / `export * from 'x'`
            spec = _string_value(source)
            if not spec:
                return
            self.info.specs.add(spec)
            clause = next((c for c in stmt.named_children if c.type == "export_clause"), None)
            if clause is None:
                self.info.star_reexports.append(spec)  # `export * from` and `export * as ns from`
                return
            for sp in clause.named_children:
                if sp.type != "export_specifier":
                    continue
                nm, al = sp.child_by_field_name("name"), sp.child_by_field_name("alias")
                if nm is not None:
                    orig = _t(nm)
                    self.info.reexports[_t(al) if al is not None else orig] = (spec, orig)
            return

        if value is not None and is_default:  # `export default <expression>`
            v = _unwrap(value)
            if v is None:
                return
            if v.type == "identifier":
                self.info.default_local = _t(v)
                return
            fn = _function_like(value)
            if fn is not None:
                self._add_function("default", fn)
            elif v.type == "class":
                self._add_class("default", v)
            return

        clause = next((c for c in stmt.named_children if c.type == "export_clause"), None)
        if clause is not None:  # `export { a, b as c }` -- local names re-exposed under new names
            for sp in clause.named_children:
                if sp.type != "export_specifier":
                    continue
                nm, al = sp.child_by_field_name("name"), sp.child_by_field_name("alias")
                if nm is not None and al is not None:
                    self.info.exports_alias[_t(al)] = _t(nm)


# --------------------------------------------------------------------------- import resolution

def _load_jsonc(path: Path) -> dict | None:
    """tsconfig.json is JSON *with comments and trailing commas*."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    out: list[str] = []
    i, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1:i + 2])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    try:
        data = json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


@dataclass
class _Cfg:
    base_dir: str | None                # repo-relative dir `baseUrl` points at
    paths_base: str                     # repo-relative dir `paths` targets are relative to
    paths: list[tuple[str, list[str]]]  # longest pattern first, as TypeScript itself prefers


class _Resolver:
    def __init__(self, root: Path, files: dict[str, str], packages: dict[str, str]):
        self.root = root.resolve()  # resolved so tsconfig paths (also resolved) compare equal even under symlinked temp dirs
        self.files = files          # repo-relative posix path -> dotted module name
        self.packages = packages    # workspace package name -> repo-relative dir
        self._pkg_names = sorted(packages, key=len, reverse=True)
        self._cfg_cache: dict[str, _Cfg | None] = {}

    def _try(self, base: str) -> str | None:
        base = posixpath.normpath(base)
        if base in self.files:
            return self.files[base]
        stem, ext = posixpath.splitext(base)
        for e in _JS_TO_TS.get(ext, ()):
            if stem + e in self.files:
                return self.files[stem + e]
        for e in _TRY_EXTS:
            if base + e in self.files:
                return self.files[base + e]
        for e in _TRY_EXTS:
            if f"{base}/index{e}" in self.files:
                return self.files[f"{base}/index{e}"]
        return None

    # ---- tsconfig / jsconfig
    def _load_cfg(self, cfg_path: Path, seen: set[Path]) -> _Cfg | None:
        if cfg_path in seen:
            return None
        seen.add(cfg_path)
        data = _load_jsonc(cfg_path)
        if data is None:
            return None
        try:
            cfg_dir = posixpath.normpath(cfg_path.parent.relative_to(self.root).as_posix())
        except ValueError:
            return None
        opts = data.get("compilerOptions") if isinstance(data.get("compilerOptions"), dict) else {}
        parent = None
        ext = data.get("extends")
        if isinstance(ext, str) and ext.startswith("."):
            parent_path = (cfg_path.parent / (ext if ext.endswith(".json") else ext + ".json")).resolve()
            parent = self._load_cfg(parent_path, seen)
        base_dir = parent.base_dir if parent else None
        if isinstance(opts.get("baseUrl"), str):
            base_dir = posixpath.normpath(posixpath.join(cfg_dir, opts["baseUrl"]))
        paths = parent.paths if parent else []
        paths_base = parent.paths_base if parent else cfg_dir
        if isinstance(opts.get("paths"), dict):
            paths = [(k, [x for x in v if isinstance(x, str)]) for k, v in opts["paths"].items() if isinstance(v, list)]
            paths.sort(key=lambda kv: len(kv[0].split("*")[0]), reverse=True)
            paths_base = base_dir if base_dir is not None else cfg_dir
        return _Cfg(base_dir=base_dir, paths_base=paths_base, paths=paths)

    def _config_for(self, rel_dir: str) -> _Cfg | None:
        rel_dir = posixpath.normpath(rel_dir) if rel_dir else "."
        if rel_dir in self._cfg_cache:
            return self._cfg_cache[rel_dir]
        cfg = None
        for name in ("tsconfig.json", "jsconfig.json"):
            candidate = self.root / rel_dir / name
            if candidate.is_file():
                cfg = self._load_cfg(candidate, set())
                if cfg is not None:
                    break
        if cfg is None and rel_dir not in (".", ""):
            cfg = self._config_for(posixpath.dirname(rel_dir) or ".")
        self._cfg_cache[rel_dir] = cfg
        return cfg

    def _via_config(self, cfg: _Cfg, spec: str) -> str | None:
        for pattern, targets in cfg.paths:
            if "*" in pattern:
                pre, _, post = pattern.partition("*")
                if len(spec) >= len(pre) + len(post) and spec.startswith(pre) and spec.endswith(post):
                    cap = spec[len(pre):len(spec) - len(post)]
                    for t in targets:
                        r = self._try(posixpath.join(cfg.paths_base, t.replace("*", cap)))
                        if r:
                            return r
            elif spec == pattern:
                for t in targets:
                    r = self._try(posixpath.join(cfg.paths_base, t))
                    if r:
                        return r
        if cfg.base_dir is not None:
            return self._try(posixpath.join(cfg.base_dir, spec))
        return None

    # ---- monorepo workspace packages
    def _via_package(self, spec: str) -> str | None:
        for name in self._pkg_names:
            if spec != name and not spec.startswith(name + "/"):
                continue
            pkg_dir = self.packages[name]
            sub = spec[len(name) + 1:]
            bases = [posixpath.join(pkg_dir, "src", sub), posixpath.join(pkg_dir, sub)] if sub else [
                posixpath.join(pkg_dir, "src", "index"), posixpath.join(pkg_dir, "index"),
            ]
            for b in bases:
                r = self._try(b)
                if r:
                    return r
        return None

    def resolve(self, spec: str, from_rel: str) -> str | None:
        if spec.startswith("."):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(from_rel), spec))
            return None if base.startswith("..") else self._try(base)
        cfg = self._config_for(posixpath.dirname(from_rel))
        if cfg is not None:
            r = self._via_config(cfg, spec)
            if r:
                return r
        return self._via_package(spec)


# --------------------------------------------------------------------------- whole-repo parse

def _dotted_for(rel: str) -> str:
    """`src/components/Button.tsx` -> `src.components.Button`. Dots *inside* a
    path segment become underscores, so segments always mean directories:
    `Button.test.tsx` -> `Button_test` (not a fake `Button` package containing a
    `test` module), which also lets the same test-file heuristics that spot
    `test_x.py` spot `x_test`/`x_spec`."""
    parts = rel.split("/")
    stem = posixpath.splitext(parts[-1])[0]
    return ".".join(p.replace(".", "_") for p in parts[:-1] + [stem])


def _discover(root: Path) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    packages: list[Path] = []
    for p in iter_files(root, IGNORED_DIRS, lambda name: is_ts_source(name) or name == "package.json"):
        (packages if p.name == "package.json" else files).append(p)
    present = {p.relative_to(root).as_posix() for p in files}

    def is_compiled_twin(p: Path) -> bool:
        stem, ext = posixpath.splitext(p.relative_to(root).as_posix())
        return any(stem + e in present for e in _COMPILED_TWIN.get(ext.lower(), ()))

    kept = (p for p in files if not is_ignored_path(p.relative_to(root)) and not is_compiled_twin(p))
    return sorted(kept), packages


def parse_ts_repo(root: Path, taken: set[str] | frozenset[str] = frozenset()) -> ParsedRepo:
    """Parses every TS/JS file under `root`. `taken` is the set of dotted names
    already claimed by other languages in the same repo (a `foo.py` next to a
    `foo.ts`), so a colliding TS module gets a distinguishing suffix instead
    of silently overwriting the other."""
    result = ParsedRepo()
    files, package_jsons = _discover(root)
    if not files:
        return result
    parsers = _get_parsers()

    infos: dict[str, _Info] = {}
    rel_to_dotted: dict[str, str] = {}
    for path in files:
        rel = path.relative_to(root).as_posix()
        dotted = _dotted_for(rel)
        if dotted in taken or dotted in infos:
            dotted = f"{dotted}_{path.suffix.lstrip('.').lower()}"
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                result.skipped_files.append(rel)
                continue
            source = path.read_bytes()
        except OSError:
            result.skipped_files.append(rel)
            continue
        module = CodeModule(path=rel, dotted_name=dotted, language=language_for(path.name))
        info = _Info(module=module, rel=rel)
        parser = parsers["ts"] if path.suffix.lower() in _TS_GRAMMAR_SUFFIXES else parsers["tsx"]
        try:
            tree = parser.parse(source)
            _Extractor(info).run(tree.root_node)
        except Exception:  # noqa: BLE001 -- one pathological file must not sink the whole run
            result.skipped_files.append(rel)
            continue
        del tree
        infos[dotted] = info
        rel_to_dotted[rel] = dotted
        result.modules[dotted] = module

    packages: dict[str, str] = {}
    for pj in package_jsons:
        data = _load_jsonc(pj)
        if data and isinstance(data.get("name"), str):
            packages.setdefault(data["name"], posixpath.normpath(pj.parent.relative_to(root).as_posix()))
    resolver = _Resolver(root, rel_to_dotted, packages)

    # resolve every specifier to an in-repo module (anything else is external and ignored)
    for dotted, info in infos.items():
        for spec in info.specs:
            info.resolved[spec] = resolver.resolve(spec, info.rel)
        info.module.imports = sorted({m for m in info.resolved.values() if m and m != dotted})

    _resolve_calls(infos)
    return result


def _resolve_calls(infos: dict[str, _Info]) -> None:
    fn_quals = {f.qualname for i in infos.values() for f in i.module.functions}
    class_quals = {c.qualname for i in infos.values() for c in i.module.classes}

    def lookup_symbol(mod: str, name: str, depth: int = 0, seen: set | None = None) -> str | None:
        seen = seen if seen is not None else set()
        if depth > 6 or (mod, name) in seen:
            return None
        seen.add((mod, name))
        info = infos.get(mod)
        if info is None:
            return None
        if name in info.symbols:
            return info.symbols[name]
        if name == "default" and info.default_local:
            return info.symbols.get(info.default_local) or via_binding(info, info.default_local, depth + 1, seen)
        if name in info.exports_alias:
            local = info.exports_alias[name]
            return info.symbols.get(local) or via_binding(info, local, depth + 1, seen)
        if name in info.reexports:
            spec, orig = info.reexports[name]
            target = info.resolved.get(spec)
            return lookup_symbol(target, orig, depth + 1, seen) if target else None
        for spec in info.star_reexports:  # `export * from './x'` -- the barrel-file pattern
            target = info.resolved.get(spec)
            found = lookup_symbol(target, name, depth + 1, seen) if target else None
            if found:
                return found
        return None

    def via_binding(info: _Info, local: str, depth: int = 0, seen: set | None = None) -> str | None:
        b = info.bindings.get(local)
        if not b:
            return None
        kind, spec, orig = b
        target = info.resolved.get(spec)
        if not target:
            return None
        if kind == "named":
            return lookup_symbol(target, orig, depth, seen)
        if kind == "default":
            return lookup_symbol(target, "default", depth, seen)
        return None

    def finish(q: str | None, kind: str) -> str | None:
        if q is None:
            return None
        if kind == "new":  # `new Foo()` runs Foo's constructor (if it declares one)
            ctor = f"{q}.constructor"
            return ctor if q in class_quals and ctor in fn_quals else None
        if kind == "jsx" and q in class_quals:  # `<Foo/>` on a class component runs render()
            render = f"{q}.render"
            return render if render in fn_quals else None
        return q if q in fn_quals else None

    for info in infos.values():
        for fn in info.module.functions:
            found: set[str] = set()
            for kind, obj, name in info.raw_calls.get(fn.qualname, ()):
                q: str | None = None
                if obj is None:
                    q = finish(info.symbols.get(name) or via_binding(info, name), kind)
                elif obj == "this":
                    if fn.class_qualname and name in info.methods_by_class.get(fn.class_qualname, ()):
                        q = f"{fn.class_qualname}.{name}"
                else:
                    b = info.bindings.get(obj)
                    if b and b[0] == "ns":
                        target = info.resolved.get(b[1])
                        q = finish(lookup_symbol(target, name), kind) if target else None
                    else:
                        owner = info.symbols.get(obj) or via_binding(info, obj)
                        static = f"{owner}.{name}" if owner in class_quals else None
                        if static in fn_quals:  # `Foo.create()`
                            q = static
                if q and q in fn_quals:
                    found.add(q)
            fn.calls = sorted(found)
