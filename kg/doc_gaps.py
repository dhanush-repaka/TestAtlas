"""Shared graph+docs context builder, and the LLM gap-analysis layer built
on top of it, which compares a repo's project documents, taken together,
against what the whole codebase actually contains, producing structured
findings.

Documents describe the system overall -- a process/architecture doc covering
multiple flows -- not one module each, so the comparison always runs against
the entire graph rather than a single business module's contents. (An
earlier version scoped this per-module; that added a linking step for no
real benefit, since a genuinely high-level doc usually touches several
modules at once anyway.)

Likewise, when a repo has more than one document, the comparison bundles
ALL of them into a single pass rather than analyzing each in isolation --
comparing one doc at a time can't see that another doc already covers what
looks like a gap, and can't tell "undocumented" from "documented elsewhere".
A repo's documents are one corpus being checked against one codebase.

Same non-live-API-call pattern as kg/enrichment.py: this module only builds
the comparison context -- the actual comparison is done by an agent (Claude
Code, or another LLM session) reading both sides and posting findings back
through the API, or by server/llm_gap_analysis.py's opt-in live call. There's
no mechanical way to compute "is this documented capability actually
implemented" -- it's a judgment call over natural language, same reasoning
as why enrichment's purpose summaries aren't AST-derived either.

server/llm_test_generation.py also reuses gap_analysis_context() -- the
graph-derived "modules" half of the context (files/classes/functions/purpose
summaries) is exactly what test-case generation needs too, and unlike gap
analysis (which is meaningless without documents to compare against), test
generation only NEEDS the code -- documents just add extra grounding when
present. Hence `require_docs`, defaulting to True to keep gap analysis's
existing behavior.

Test generation additionally uses module_test_context() below to scope that
same context down to ONE module, rather than the whole codebase -- a large
repo (hundreds of modules) sent as one context made one OpenAI call spread
its single ~59-case output-token budget across every module in the repo,
leaving most modules with zero cases. A per-module call gives each module
its own full budget instead. Deliberately NOT the same move gap analysis's
docstring above warns against (per-module doc comparison, which added a
linking step for no benefit) -- this is scoping the CODE side of an
already-built context to control a single LLM call's output size, not
re-introducing a document-to-module linking step.
"""
from __future__ import annotations

import networkx as nx

ALLOWED_GAP_CATEGORIES = {"missing_implementation", "undocumented_capability", "mismatch"}


def gap_analysis_context(
    g: nx.MultiDiGraph, docs: list[dict], require_docs: bool = True, include_ui_text: bool = False
) -> dict:
    """Everything an agent needs to compare a repo's documents -- combined,
    not one at a time -- against the codebase's real, complete contents:
    every module (business-domain grouping, see kg/dev_graph_builder.py),
    each with its purpose summary (if enriched) and the actual class/function
    names AST found in each of its files -- each class's real METHODS
    included, not just its bare name (a File only DEFINES its top-level
    classes/functions; a class's own methods are a separate Class-DEFINES->
    Function edge that's easy to miss walking file-level edges alone, and
    doing so left the consumer -- test-case generation -- unable to see or
    target ~2/3 of a typical class-heavy codebase's real testable surface).
    Pass require_docs=False (test generation does) to build the same context
    with zero documents -- the "documents" list in the result is just empty
    in that case. include_ui_text=True (test generation does) also lists the
    visible strings kg/ts_parser.py found in a file's JSX -- button labels,
    headings, placeholders -- so a functional test step can name what's really
    on screen; off by default so gap analysis prompts don't grow for it."""
    if require_docs and not docs:
        return {"ok": False, "message": "This repo has no documents yet -- add at least one first."}

    file_nodes = [n for n, d in g.nodes(data=True) if d.get("type") == "File"]
    if not file_nodes:
        return {"ok": False, "message": "This run's graph has no files to compare against."}

    by_domain: dict[str, list[dict]] = {}
    for file_id in sorted(file_nodes):
        data = g.nodes[file_id]
        domain = data.get("domain") or "(ungrouped)"
        class_ids = [
            v for _, v, ed in g.out_edges(file_id, data=True)
            if ed.get("relation") == "DEFINES" and g.nodes[v].get("type") == "Class"
        ]
        functions = [
            g.nodes[v]["label"] for _, v, ed in g.out_edges(file_id, data=True)
            if ed.get("relation") == "DEFINES" and g.nodes[v].get("type") == "Function"
        ]
        classes = []
        for cls_id in sorted(class_ids, key=lambda cid: g.nodes[cid]["label"]):
            methods = sorted(
                g.nodes[m]["label"] for _, m, med in g.out_edges(cls_id, data=True)
                if med.get("relation") == "DEFINES" and g.nodes[m].get("type") == "Function"
            )
            classes.append({"name": g.nodes[cls_id]["label"], "methods": methods})
        entry = {
            "file": data["label"],
            "purpose": data.get("purpose"),
            "classes": classes,
            "functions": sorted(functions),
        }
        if include_ui_text and data.get("ui_text"):
            entry["ui_text"] = data["ui_text"]
        by_domain.setdefault(domain, []).append(entry)

    modules = [{"module": domain, "files": files} for domain, files in sorted(by_domain.items())]

    return {
        "ok": True,
        "documents": [{"id": d["id"], "name": d["name"], "content": d["content"]} for d in docs],
        "modules": modules,
    }


def module_test_context(g: nx.MultiDiGraph, docs: list[dict], module: str, display_name: str | None = None) -> dict:
    """Same context gap_analysis_context() builds, narrowed to a single
    module's code -- documents stay the full set (a doc may describe this
    module's role within the wider system; there's no per-doc split here),
    only the "modules" list is filtered down to the one requested. See the
    module docstring for why test generation needs this and gap analysis
    doesn't."""
    full = gap_analysis_context(g, docs, require_docs=False, include_ui_text=True)
    if not full["ok"]:
        return full
    matching = [m for m in full["modules"] if m["module"] == module]
    if not matching:
        return {"ok": False, "message": f"No module named '{module}' found in this run's graph."}
    # display_name (the friendly, business-English label from server/llm_module_naming.py,
    # if this module has one) tells the model what the module is FOR -- which is what
    # decides whether a functional scenario is even meaningful for it.
    return {"ok": True, "documents": full["documents"], "modules": matching, "display_name": display_name}
