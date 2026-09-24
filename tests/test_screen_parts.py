"""screen_parts(): what a module's code renders from OTHER modules (kg/doc_gaps.py)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kg.dev_graph_builder import build_dev_graph
from kg.doc_gaps import module_test_context, screen_parts
from kg.repo_parser import parse_repo


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


class ScreenPartsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        write(root, {
            "app/page.tsx": "import Carousel from '../components/carousel';\nimport Footer from '../components/layout/footer';\n"
                            "import { getItems } from '../lib/store';\n"
                            "export default function HomePage() { getItems(); return <div><Carousel /><Footer /></div>; }\n",
            "components/header.tsx": "import Search from './search';\nimport { getMenu } from '../lib/store';\nexport default function Header() { getMenu(); return <div><Search /></div>; }\n",
            "components/search.tsx": "export default function Search() { return <input placeholder='Search for products...' />; }\n",
            "components/carousel.tsx": "export default function Carousel() { return <ul />; }\n",
            "components/layout/footer.tsx": "export default function Footer() { return <p>All rights reserved.</p>; }\n",
            "lib/store.ts": "export function getItems() { return []; }\nexport function getMenu() { return []; }\n",
        })
        self.g = build_dev_graph(parse_repo(root))

    def tearDown(self):
        self._tmp.cleanup()

    def test_lists_rendered_parts_from_other_modules_with_their_on_screen_text(self):
        parts = {p["name"]: p for p in screen_parts(self.g, "app")}
        self.assertEqual(set(parts), {"Carousel", "Footer", "getItems"})
        self.assertEqual(parts["Footer"]["ui_text"], ["All rights reserved."])

    def test_supporting_modules_are_left_out_when_labelled_and_own_module_never_appears(self):
        parts = {p["name"] for p in screen_parts(self.g, "app", {"lib": {"kind": "supporting"}})}
        self.assertEqual(parts, {"Carousel", "Footer"})     # getItems lives in the supporting `lib`
        self.assertNotIn("HomePage", {p["name"] for p in screen_parts(self.g, "app")})

    def test_a_component_in_a_module_labelled_supporting_still_counts(self):
        # the naming model filed a mixed `components` folder as "supporting"; its Carousel is still on screen
        labels = {"components": {"kind": "supporting"}, "lib": {"kind": "supporting"}}
        names = {p["name"] for p in screen_parts(self.g, "app", labels)}
        self.assertIn("Carousel", names)        # PascalCase component -> kept
        self.assertNotIn("getItems", names)     # camelCase data function in a supporting module -> dropped

    def test_a_part_lists_what_it_contains_and_the_data_it_loads(self):
        write(Path(self._tmp.name), {"app/page.tsx": open(Path(self._tmp.name) / "app/page.tsx").read().replace(
            "<Footer />", "<Footer /><Header />").replace("import Footer", "import Header from '../components/header';\nimport Footer")})
        g = build_dev_graph(parse_repo(Path(self._tmp.name)))
        header = {p["name"]: p for p in screen_parts(g, "app")}["Header"]
        self.assertEqual(header["contains"], ["Search"])
        self.assertEqual(header["loads"], ["getMenu"])

    def test_context_carries_parts_and_kind(self):
        labels = {"app": {"name": "Home Page", "kind": "feature"}, "components.layout": {"name": "Page Footer", "kind": "feature"}}
        ctx = module_test_context(self.g, [], "app", display_name="Home Page", labels=labels)
        self.assertEqual(ctx["kind"], "feature")
        self.assertEqual({p["name"]: p.get("display_name") for p in ctx["parts"]}["Footer"], "Page Footer")


if __name__ == "__main__":
    unittest.main()
