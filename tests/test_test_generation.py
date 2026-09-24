"""Tests for functional / Azure-DevOps-shaped test-case generation
(server/llm_test_generation.py), its storage (server/db.py), and the UI-text
extraction that feeds it (kg/ts_parser.py). The OpenAI call itself is mocked --
these check what WE control: the prompt, and what we accept back from a model.
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kg.repo_parser import parse_repo
from server import db
from server import llm_test_generation as gen

CONTEXT = {
    "documents": [],
    "display_name": "Shopping Cart",
    "modules": [{
        "module": "components.cart",
        "files": [
            {"file": "components.cart.add-to-cart", "purpose": None, "classes": [],
             "functions": ["AddToCart", "SubmitButton"], "ui_text": ["Add To Cart", "Out Of Stock"]},
            {"file": "components.cart.cart-service", "purpose": None,
             "classes": [{"name": "CartService", "methods": ["addItem", "removeItem"]}], "functions": []},
        ],
    }],
}
CANON = gen._known_code_names(CONTEXT)


def good_case(**over):
    c = {
        "title": "Verify that a shopper can add an in-stock product to the cart",
        "description": "Adding is the core purchase flow.",
        "priority": 1, "category": "happy_path", "feature": "Add to cart",
        "preconditions": "A product page for an in-stock item is open",
        "steps": [
            {"action": 'Click "Add To Cart"', "expected": "The cart shows 1 item"},
            {"action": "Open the cart", "expected": "The product is listed with quantity 1"},
        ],
        "covers": ["AddToCart", "CartService.addItem"],
    }
    c.update(over)
    return c


class NormalizeCaseTests(unittest.TestCase):
    def test_valid_case_is_ado_shaped(self):
        n = gen._normalize_case(good_case(), CANON)
        self.assertEqual(n["steps"][0], {"action": 'Click "Add To Cart"', "expected": "The cart shows 1 item"})
        self.assertEqual(n["target"], "Add to cart")            # `feature` is stored as the target column
        self.assertEqual(n["priority"], 1)
        self.assertEqual(n["expected_result"], "The product is listed with quantity 1")  # overall = last step's
        self.assertEqual(n["covers"], ["AddToCart", "CartService.addItem"])

    def test_every_step_needs_its_own_expected_result(self):
        self.assertIsNone(gen._normalize_case(good_case(steps=[
            {"action": "Click it", "expected": "Something happens"}, {"action": "Then this", "expected": ""}]), CANON))
        self.assertIsNone(gen._normalize_case(good_case(steps=["Click it", "Then this"]), CANON))  # bare strings: no expected result

    def test_a_one_step_scenario_is_not_a_functional_test(self):
        self.assertIsNone(gen._normalize_case(good_case(steps=[{"action": "a", "expected": "b"}]), CANON))

    def test_tolerates_key_spelling_drift(self):
        n = gen._normalize_case(good_case(steps=[
            {"step": "Open the cart", "expected_result": "It opens"},
            {"description": "Close it", "expected_outcome": "It closes"}]), CANON)
        self.assertEqual([s["action"] for s in n["steps"]], ["Open the cart", "Close it"])
        self.assertEqual([s["expected"] for s in n["steps"]], ["It opens", "It closes"])

    def test_invented_code_names_are_dropped_real_ones_kept_and_canonicalized(self):
        n = gen._normalize_case(good_case(covers=[
            "AddToCart", "InventedThing", "components.cart.cart-service.CartService.removeItem", "removeItem", "AddToCart"]), CANON)
        self.assertEqual(n["covers"], ["AddToCart", "CartService.removeItem"])  # deduped; path prefix and bare method resolved

    def test_priority_is_clamped_and_defaulted(self):
        self.assertEqual(gen._normalize_case(good_case(priority=9), CANON)["priority"], 4)
        self.assertEqual(gen._normalize_case(good_case(priority=0), CANON)["priority"], 1)
        self.assertEqual(gen._normalize_case(good_case(priority="high"), CANON)["priority"], 2)

    def test_rejects_bad_category_and_missing_title(self):
        self.assertIsNone(gen._normalize_case(good_case(category="unit_test"), CANON))
        self.assertIsNone(gen._normalize_case(good_case(title="  "), CANON))
        self.assertIsNone(gen._normalize_case("not a dict", CANON))


class PromptAndCountTests(unittest.TestCase):
    def test_prompt_asks_for_functional_ado_cases_with_real_ui_text(self):
        prompt = gen._build_prompt(CONTEXT, 5)
        for needle in ("FUNCTIONAL", "NOT a unit test", "Never write one case per function", '"expected"',
                       "Add To Cart", "Out Of Stock", 'known as "Shopping Cart"', "Do NOT invent label"):
            self.assertIn(needle, prompt)

    def test_functional_count_scales_slowly_with_units(self):
        # 2 functions + CartService(1 + 2 methods) = 5 real units -> the floor, not 1.5x like the unit-style version
        self.assertEqual(gen._target_case_count(CONTEXT), 3)
        big = {"modules": [{"module": "m", "files": [{"file": "m.a", "functions": [f"f{i}" for i in range(50)], "classes": []}]}]}
        self.assertEqual(gen._target_case_count(big), 20)                    # 50 * 0.4
        huge = {"modules": [{"module": "m", "files": [{"file": "m.a", "functions": [f"f{i}" for i in range(900)], "classes": []}]}]}
        self.assertEqual(gen._target_case_count(huge), gen.HARD_CASE_CAP)   # bounded by what one response can hold

    def test_cap_is_derived_from_the_token_budget(self):
        self.assertLessEqual(gen.HARD_CASE_CAP * gen._TOKENS_PER_CASE + gen._PROMPT_OVERHEAD_TOKENS, gen._MODEL_TOKEN_CEILING)


class RunGenerationTests(unittest.TestCase):
    def _run(self, payload):
        resp = mock.Mock()
        resp.choices = [mock.Mock(message=mock.Mock(content=json.dumps(payload)))]
        client = mock.Mock()
        client.chat.completions.create.return_value = resp
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            return gen.run_test_generation(CONTEXT), client

    def test_keeps_valid_cases_and_silently_drops_malformed_ones(self):
        cases, client = self._run({"test_cases": [good_case(), {"title": "no steps"}, good_case(steps=["bare"]), "junk"]})
        self.assertEqual(len(cases), 1)
        sent = client.chat.completions.create.call_args.kwargs
        self.assertIn("FUNCTIONAL", sent["messages"][0]["content"])
        self.assertLessEqual(sent["max_tokens"], gen._MODEL_TOKEN_CEILING)

    def test_empty_list_is_a_valid_answer(self):
        cases, _ = self._run({"test_cases": []})
        self.assertEqual(cases, [])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.object(db, "DATA_DIR", Path(self._tmp.name)),
                         mock.patch.object(db, "DB_PATH", Path(self._tmp.name) / "kg.db")]
        for p in self._patches:
            p.start()
        db.init_db()
        self.repo_id = db.create_repo({"name": "r", "source_type": "local", "local_path": "/tmp"})["id"]

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_functional_case_round_trips_and_legacy_case_still_reads(self):
        db.replace_test_cases(self.repo_id, "legacy.mod", None, [{
            "title": "Encode empty string", "category": "edge_case", "target": "enc.base64", "preconditions": None,
            "steps": ["call it with empty input"], "expected_result": "returns empty", "edge_case_description": "empty input"}])
        n = gen._normalize_case(good_case(), CANON)
        db.replace_test_cases(self.repo_id, "components.cart", None, [n])
        rows = {r["module"]: r for r in db.list_test_cases(self.repo_id)}

        fn = rows["components.cart"]
        self.assertEqual(fn["steps"][0]["action"], 'Click "Add To Cart"')      # objects survive the JSON column
        self.assertEqual((fn["priority"], fn["covers"], fn["description"]), (1, ["AddToCart", "CartService.addItem"], "Adding is the core purchase flow."))

        old = rows["legacy.mod"]
        self.assertEqual(old["steps"], ["call it with empty input"])           # legacy string steps untouched
        self.assertIsNone(old["priority"])
        self.assertEqual(old["covers"], [])

    def test_regenerating_one_module_leaves_others_alone(self):
        n = gen._normalize_case(good_case(), CANON)
        db.replace_test_cases(self.repo_id, "a", None, [n, n])
        db.replace_test_cases(self.repo_id, "b", None, [n])
        db.replace_test_cases(self.repo_id, "a", None, [n])
        by_mod = [r["module"] for r in db.list_test_cases(self.repo_id)]
        self.assertEqual(sorted(by_mod), ["a", "b"])

    def test_within_a_pass_most_important_first(self):
        cases = [gen._normalize_case(good_case(title=f"Verify {p}", priority=p), CANON) for p in (3, 1, 4, 2)]
        db.replace_test_cases(self.repo_id, "m", None, cases)
        self.assertEqual([r["priority"] for r in db.list_test_cases(self.repo_id)], [1, 2, 3, 4])


class UiTextExtractionTests(unittest.TestCase):
    def test_extracts_what_a_user_can_see_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Cart.tsx").write_text('''
                export function Cart({ items }) {
                  return (
                    <section>
                      <h1>My Cart</h1>
                      <input placeholder="Search for products..." aria-label="Search" data-testid="not-visible" className="x" />
                      <button title="Close cart">{"Checkout now"}</button>
                      <p>Your cart is empty.</p>
                      <p>{items.length}</p>
                      <span>·</span>
                      <p>This paragraph is long body copy rather than a label a test step would ever need to quote verbatim.</p>
                      <img alt="Product photo" src="/x.png" />
                    </section>
                  );
                }''')
            m = parse_repo(Path(d)).modules["Cart"]
        self.assertEqual(m.ui_text, ["My Cart", "Search for products...", "Search", "Close cart",
                                     "Checkout now", "Your cart is empty.", "Product photo"])

    def test_reads_strings_inside_ternaries_but_not_classnames_keys_or_handlers(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Checkout.tsx").write_text("""
                export function CheckoutButton({ pending, empty, t }) {
                  return (
                    <form onSubmit={() => track('Checkout Started Event')}>
                      <button className={clsx('flex items-center rounded-full', pending && 'opacity-50 cursor-wait')} aria-label={pending ? 'Redirecting to checkout' : 'Proceed to Checkout'}>
                        {pending ? <Spinner/> : 'Proceed to Checkout'}
                      </button>
                      {empty && 'Nothing to buy yet'}
                      <span>{t('cart.title')}</span>
                      <a href="/cart">{'x'}</a>
                    </form>
                  );
                }""")
            m = parse_repo(Path(d)).modules["Checkout"]
        self.assertIn("Proceed to Checkout", m.ui_text)          # the ternary's string branch (the miss found in production)
        self.assertIn("Redirecting to checkout", m.ui_text)      # a ternary inside an aria-label
        self.assertIn("Nothing to buy yet", m.ui_text)           # `&&` right-hand side
        self.assertEqual(len(m.ui_text), 3)                       # ...and nothing else:
        for junk in ("flex items-center rounded-full", "opacity-50 cursor-wait", "Checkout Started Event", "cart.title", "/cart"):
            self.assertNotIn(junk, m.ui_text)                    # classNames, handler args, i18n keys, paths

    def test_classnames_inside_nested_jsx_in_a_map_callback_do_not_leak(self):
        # the real bug: components.cart's modal renders <li className="flex h-16 flex-col ..."> inside items.map(...)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Cart.tsx").write_text("""
                export function Cart({ items }) {
                  return (
                    <ul className="flex flex-col gap-2 overflow-auto">
                      {items.map((item) => (
                        <li key={item.id} className="relative flex w-full flex-row justify-between px-1 py-4">
                          <span className={clsx('text-sm font-bold', item.big && 'text-lg')}>{item.title}</span>
                          <button aria-label="Remove cart item" className="h-4 w-4 dark:text-neutral-500">Remove</button>
                        </li>
                      ))}
                    </ul>
                  );
                }""")
            m = parse_repo(Path(d)).modules["Cart"]
        # the aria-label and the visible button text -- and no class names, though they sit right beside them
        self.assertEqual(m.ui_text, ["Remove cart item", "Remove"])
    def test_capped_and_absent_for_non_ui_files(self):
        with tempfile.TemporaryDirectory() as d:
            many = "".join(f"<li>Item number {i}</li>" for i in range(40))
            (Path(d) / "List.tsx").write_text(f"export const List = () => <ul>{many}</ul>;")
            (Path(d) / "util.ts").write_text("export function add(a: number, b: number) { return a + b; }")
            (Path(d) / "mod.py").write_text("def f():\n    return 'Not UI text'\n")
            mods = parse_repo(Path(d)).modules
        self.assertEqual(len(mods["List"].ui_text), 15)
        self.assertEqual(mods["util"].ui_text, [])
        self.assertEqual(mods["mod"].ui_text, [])


if __name__ == "__main__":
    unittest.main()
