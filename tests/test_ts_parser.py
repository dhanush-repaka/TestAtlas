"""Regression tests for TypeScript/JavaScript parsing (kg/ts_parser.py) and the
multi-language dispatch (kg/repo_parser.py). Stdlib `unittest` only, so no
test dependency is needed:  python -m unittest discover -s tests -t .

One fixture project deliberately covers the constructs whose handling is easy
to get subtly wrong: tsconfig `paths` aliases (in a config full of comments and
trailing commas), barrel re-exports, arrow-function components wrapped in
memo(), JSX tags as calls, `new` -> constructor, static calls, CommonJS
require/exports, monorepo workspace packages, and things that must be skipped.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kg.dev_graph_builder import build_dev_graph
from kg.repo_parser import is_ignored_path, is_source_file, parse_repo
from kg.ts_parser import _dotted_for, is_ts_source

FILES = {
    "tsconfig.json": """{
      // comments and trailing commas are legal in tsconfig
      "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["src/*"], }, },
    }""",
    "src/lib/math.ts": """
        export function add(a: number, b: number) { return a + b; }
        export const mul = (a: number, b: number) => a * b;
        export default function helper() { return add(1, 2); }
        function internal() {}
    """,
    "src/lib/index.ts": """
        export * from './math';
        export { default as helper } from './math';
    """,
    "src/store.ts": "export class Store { static persist(u: unknown) {} }",
    "src/models/user.ts": """
        import { add } from '@/lib/math';
        import { Store } from '../store';
        export abstract class Base { abstract id(): string; protected log() {} }
        export class User extends Base implements Serializable {
            constructor(private name: string) { super(); this.log(); }
            get label() { return this.name; }
            set label(v: string) {}
            static create(name: string) { return new User(name); }
            greet = () => { this.log(); return add(1, 2); };
            async save() { return Store.persist(this); }
        }
        interface Ignored { x: number }
        type Alias = string;
        enum Color { Red }
    """,
    "src/ui/Button.tsx": """
        import { helper, mul } from '@/lib';
        import * as math from '../lib/math';
        export const Button = memo(() => <div><Icon/>{mul(2, 3)}</div>);
        const Icon = () => <span>{math.add(1, 2)}</span>;
        export default function Page() { helper(); return <Button/>; }
    """,
    "src/ui/Button.test.tsx": "import { Button } from './Button';\ntest('x', () => { Button(); });",
    "src/legacy.js": """
        const { add } = require('./lib/math');
        exports.run = function () { return add(1, 2); };
        module.exports.other = () => {};
    """,
    "src/compiled.ts": "export function real() {}",
    "src/compiled.js": "function generated() {}",          # compiled twin of compiled.ts -> must be dropped
    "src/types.d.ts": "export declare function ambient(): void;",  # declaration file -> nothing to parse
    "src/broken.ts": "export function ok() {}\nconst = = ;",   # syntax error -> still mined
    "node_modules/pkg/index.js": "function nm() {}",
    "dist/out.js": "function built() {}",
    "vendor/lib.js": "function vendored() {}",                 # vendored JS -> skipped
    "vendor/kept.ts": "export function keptVendored() {}",       # vendored TS is real source -> kept (tRPC does this)
    "packages/core/package.json": '{"name": "@acme/core"}',
    "packages/core/src/index.ts": "export function coreFn() {}",
    "apps/web/src/main.ts": "import { coreFn } from '@acme/core';\nexport function boot() { coreFn(); }",
    "py/util.py": "def helper():\n    return 1\n",
}


def calls(parsed, qualname):
    for m in parsed.modules.values():
        for f in m.functions:
            if f.qualname == qualname:
                return set(f.calls)
    raise AssertionError(f"no function {qualname}")


class TsParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        for rel, body in FILES.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        cls.parsed = parse_repo(root)
        cls.m = cls.parsed.modules

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # ---- which files become modules
    def test_module_set_and_skips(self):
        self.assertEqual(
            set(self.m),
            {"src.lib.math", "src.lib.index", "src.store", "src.models.user", "src.ui.Button", "src.ui.Button_test",
             "src.legacy", "src.compiled", "src.broken", "packages.core.src.index", "apps.web.src.main", "py.util",
             "vendor.kept"},
        )
        self.assertEqual(self.m["src.compiled"].language, "typescript")  # the .ts, not its compiled .js twin
        self.assertEqual(self.m["src.legacy"].language, "javascript")
        self.assertEqual(self.m["py.util"].language, "python")
        self.assertEqual(self.parsed.skipped_files, [])

    def test_syntax_error_file_is_still_mined(self):
        self.assertEqual([f.name for f in self.m["src.broken"].functions], ["ok"])

    # ---- what gets extracted
    def test_functions_arrow_consts_and_default_exports(self):
        names = {f.name for f in self.m["src.lib.math"].functions}
        self.assertEqual(names, {"add", "mul", "helper", "internal"})  # `mul` is an arrow function bound to a const
        button = {f.name for f in self.m["src.ui.Button"].functions}
        self.assertEqual(button, {"Button", "Icon", "Page"})           # Button = memo(() => ...) counts

    def test_classes_methods_and_bases(self):
        classes = {c.name: c for c in self.m["src.models.user"].classes}
        self.assertEqual(set(classes), {"Base", "User"})               # interface / type alias / enum deliberately not classes
        self.assertEqual(classes["User"].bases, ["Base", "Serializable"])
        self.assertEqual(sorted(m.split(".")[-1] for m in classes["User"].methods),
                         ["constructor", "create", "greet", "label", "save"])  # getter+setter once; arrow field is a method
        self.assertEqual([m.split(".")[-1] for m in classes["Base"].methods], ["log"])  # abstract signatures aren't methods
        self.assertTrue(all(f.is_method for f in self.m["src.models.user"].functions))

    # ---- imports
    def test_imports_resolve_aliases_barrels_packages_and_require(self):
        self.assertEqual(self.m["src.models.user"].imports, ["src.lib.math", "src.store"])   # '@/lib/math' via tsconfig paths
        self.assertEqual(self.m["src.ui.Button"].imports, ["src.lib.index", "src.lib.math"])
        self.assertEqual(self.m["src.legacy"].imports, ["src.lib.math"])                       # require()
        self.assertEqual(self.m["apps.web.src.main"].imports, ["packages.core.src.index"])     # monorepo package name
        self.assertEqual(self.m["src.ui.Button_test"].imports, ["src.ui.Button"])
        self.assertEqual(self.m["src.lib.index"].imports, ["src.lib.math"])

    # ---- calls
    def test_calls(self):
        self.assertEqual(calls(self.parsed, "src.lib.math:helper"), {"src.lib.math:add"})
        self.assertEqual(calls(self.parsed, "src.models.user:User.greet"), {"src.lib.math:add"})   # via aliased import
        self.assertEqual(calls(self.parsed, "src.models.user:User.save"), {"src.store:Store.persist"})  # static call
        self.assertEqual(calls(self.parsed, "src.models.user:User.create"), {"src.models.user:User.constructor"})  # new -> ctor
        self.assertEqual(calls(self.parsed, "src.ui.Button:Icon"), {"src.lib.math:add"})            # Namespace.fn()
        # memo()-wrapped component: <Icon/> as a call, and mul() resolved THROUGH the `export *` barrel
        self.assertEqual(calls(self.parsed, "src.ui.Button:Button"), {"src.ui.Button:Icon", "src.lib.math:mul"})
        # helper() resolved through `export { default as helper } from`; <Button/> as a call
        self.assertEqual(calls(self.parsed, "src.ui.Button:Page"), {"src.lib.math:helper", "src.ui.Button:Button"})
        self.assertEqual(calls(self.parsed, "src.legacy:run"), {"src.lib.math:add"})                # CommonJS require + exports.run
        self.assertEqual(calls(self.parsed, "apps.web.src.main:boot"), {"packages.core.src.index:coreFn"})

    def test_inherited_this_call_is_an_honest_miss(self):
        # this.log() is Base's method, not User's -- inheritance isn't followed, same class of limit as the Python parser
        self.assertNotIn("src.models.user:Base.log", calls(self.parsed, "src.models.user:User.constructor"))

    # ---- graph
    def test_builds_a_graph_with_language_tags(self):
        g = build_dev_graph(self.parsed)
        langs = {g.nodes[n]["label"]: g.nodes[n]["language"] for n, d in g.nodes(data=True) if d["type"] == "File"}
        self.assertEqual(langs["src.ui.Button"], "typescript")
        self.assertEqual(langs["src.legacy"], "javascript")
        self.assertEqual(langs["py.util"], "python")
        rel = {(u, v) for u, v, d in g.edges(data=True) if d["relation"] == "CALLS"}
        self.assertIn(("function:src.ui.Button:Page", "function:src.ui.Button:Button"), rel)


class HelperTests(unittest.TestCase):
    def test_dotted_names_treat_inner_dots_as_underscores(self):
        self.assertEqual(_dotted_for("src/components/Button.tsx"), "src.components.Button")
        self.assertEqual(_dotted_for("src/foo.test.ts"), "src.foo_test")      # so test-file heuristics see it
        self.assertEqual(_dotted_for("src/components/index.ts"), "src.components.index")  # a barrel keeps its own domain

    def test_source_file_predicates(self):
        for ok in ("a.py", "a.ts", "a.tsx", "a.js", "a.jsx", "a.mjs", "a.cjs", "A.TS"):
            self.assertTrue(is_source_file(ok), ok)
        for bad in ("a.d.ts", "a.min.js", "a.json", "a.md", "a.css", "README", "a.pyc"):
            self.assertFalse(is_source_file(bad), bad)
        self.assertFalse(is_ts_source("types.d.mts"))

    def test_ignore_rules_differ_by_language_so_python_is_unchanged(self):
        self.assertTrue(is_ignored_path(Path("vendor/lib.js")))           # vendored JS: skipped
        self.assertFalse(is_ignored_path(Path("src/vendor/unpromise.ts")))  # vendored TS: real source, kept
        self.assertFalse(is_ignored_path(Path("vendor/lib.py")))          # vendored PYTHON: still parsed, as before
        self.assertTrue(is_ignored_path(Path("node_modules/x/index.ts")))
        self.assertTrue(is_ignored_path(Path("node_modules/x/y.py")))
        self.assertTrue(is_ignored_path(Path(".next/server/app.js")))
        self.assertFalse(is_ignored_path(Path("src/app.tsx")))


if __name__ == "__main__":
    unittest.main()
