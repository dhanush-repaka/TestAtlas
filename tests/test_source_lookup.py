"""Tests for kg/source_lookup.py -- reading a cited function/method's real
source off disk for the test-case verification pass. Python-only for now;
a non-Python file or an unresolvable name degrades to None, never raises.
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kg import source_lookup as sl

SOURCE = '''
class Cart:
    def add(self, item):
        self.items.append(item)
    def remove(self, item):
        self.items.remove(item)

def checkout(cart):
    return {"status": "ok"}
'''


class ExtractFunctionSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "cart.py").write_text(SOURCE)

    def test_extracts_a_method_by_class_dot_method(self):
        src = sl.extract_function_source(self.tmp / "cart.py", "Cart.add")
        self.assertIn("def add(self, item):", src)
        self.assertIn("self.items.append(item)", src)
        self.assertNotIn("def remove", src)  # just the one method, not the whole class

    def test_extracts_a_top_level_function_by_bare_name(self):
        src = sl.extract_function_source(self.tmp / "cart.py", "checkout")
        self.assertIn('return {"status": "ok"}', src)

    def test_unknown_name_returns_none_not_an_error(self):
        self.assertIsNone(sl.extract_function_source(self.tmp / "cart.py", "DoesNotExist"))
        self.assertIsNone(sl.extract_function_source(self.tmp / "cart.py", "Cart.alsoMissing"))

    def test_missing_file_returns_none_not_an_error(self):
        self.assertIsNone(sl.extract_function_source(self.tmp / "nope.py", "checkout"))

    def test_non_python_file_returns_none(self):
        (self.tmp / "cart.ts").write_text("function checkout() { return true; }")
        self.assertIsNone(sl.extract_function_source(self.tmp / "cart.ts", "checkout"))

    def test_unparseable_python_returns_none_not_an_error(self):
        (self.tmp / "broken.py").write_text("def this is not valid python(((")
        self.assertIsNone(sl.extract_function_source(self.tmp / "broken.py", "checkout"))


class FindCitationPathTests(unittest.TestCase):
    def _graph(self):
        import networkx as nx
        g = nx.MultiDiGraph()
        g.add_node("file:cart", type="File", domain="cart", path="cart.py")
        g.add_node("class:cart:Cart", type="Class", domain="cart", path="cart.py", label="Cart")
        g.add_node("function:cart:Cart.add", type="Function", domain="cart", path="cart.py", label="add")
        g.add_node("function:cart:checkout", type="Function", domain="cart", path="cart.py", label="checkout")
        g.add_node("function:other:checkout", type="Function", domain="other", path="other.py", label="checkout")
        return g

    def test_finds_a_method_s_file_via_its_bare_label(self):
        g = self._graph()
        self.assertEqual(sl.find_citation_path(g, "cart", "Cart.add"), "cart.py")

    def test_finds_a_top_level_function_s_file(self):
        g = self._graph()
        self.assertEqual(sl.find_citation_path(g, "cart", "checkout"), "cart.py")

    def test_only_searches_within_the_given_module_domain(self):
        g = self._graph()
        # "checkout" exists in both "cart" and "other" domains -- must resolve to cart's file when asked for cart
        self.assertEqual(sl.find_citation_path(g, "cart", "checkout"), "cart.py")
        self.assertEqual(sl.find_citation_path(g, "other", "checkout"), "other.py")

    def test_unresolvable_name_returns_none(self):
        g = self._graph()
        self.assertIsNone(sl.find_citation_path(g, "cart", "NotReal.fake"))


class GatherCitedSourcesTests(unittest.TestCase):
    def test_resolves_every_distinct_cited_name_across_cases(self):
        import networkx as nx
        tmp = Path(tempfile.mkdtemp())
        (tmp / "cart.py").write_text(SOURCE)
        g = nx.MultiDiGraph()
        g.add_node("c1", type="Class", domain="cart", path="cart.py", label="Cart")
        g.add_node("f1", type="Function", domain="cart", path="cart.py", label="add")
        g.add_node("f2", type="Function", domain="cart", path="cart.py", label="checkout")

        cases = [
            {"covers": ["Cart.add"]},
            {"covers": ["checkout", "Cart.add"]},  # shared citation -- only looked up once
            {"covers": ["NotReal.fake"]},  # unresolvable -- simply absent from the result
        ]
        sources = sl.gather_cited_sources(g, "cart", tmp, cases)
        self.assertEqual(set(sources.keys()), {"Cart.add", "checkout"})
        self.assertIn("self.items.append(item)", sources["Cart.add"])


class FindCitationDomainAndPathTests(unittest.TestCase):
    """The domain-agnostic sibling of find_citation_path -- for a Story
    Coverage requirement's citation, which isn't pre-scoped to one module."""

    def _graph(self):
        import networkx as nx
        g = nx.MultiDiGraph()
        g.add_node("f1", type="Function", domain="cart", path="cart.py", label="add")
        g.add_node("c1", type="Class", domain="cart", path="cart.py", label="Cart")
        g.add_node("f2", type="Function", domain="notifications", path="notifications.py", label="checkout")
        return g

    def test_finds_a_method_without_knowing_its_domain_in_advance(self):
        g = self._graph()
        self.assertEqual(sl.find_citation_domain_and_path(g, "Cart.add"), ("cart", "cart.py"))

    def test_finds_a_top_level_function_in_whichever_domain_it_lives(self):
        g = self._graph()
        self.assertEqual(sl.find_citation_domain_and_path(g, "checkout"), ("notifications", "notifications.py"))

    def test_unresolvable_name_returns_none(self):
        g = self._graph()
        self.assertIsNone(sl.find_citation_domain_and_path(g, "NotReal.fake"))


class GatherRequirementSourcesTests(unittest.TestCase):
    def test_resolves_every_cited_name_on_one_requirement(self):
        import networkx as nx
        tmp = Path(tempfile.mkdtemp())
        (tmp / "cart.py").write_text(SOURCE)
        g = nx.MultiDiGraph()
        g.add_node("c1", type="Class", domain="cart", path="cart.py", label="Cart")
        g.add_node("f1", type="Function", domain="cart", path="cart.py", label="add")

        requirement = {"covers": ["Cart.add", "NotReal.fake"]}
        sources = sl.gather_requirement_sources(g, tmp, requirement)
        self.assertEqual(set(sources.keys()), {"Cart.add"})
        self.assertIn("self.items.append(item)", sources["Cart.add"])

    def test_a_requirement_with_no_covers_returns_an_empty_dict(self):
        import networkx as nx
        g = nx.MultiDiGraph()
        self.assertEqual(sl.gather_requirement_sources(g, Path("/tmp"), {"covers": []}), {})


if __name__ == "__main__":
    unittest.main()
