"""Regression tests for the human-mode fallback list renderer."""

from __future__ import annotations

from lium.cli.provider import _render


def test_render_generic_rows_sorts_without_mutating_keys(capsys) -> None:
    """``_render_generic_rows`` used to call ``keys.index`` from inside the
    ``list.sort`` key function; CPython empties the list during the sort so the
    first lookup raised ``ValueError: 'id' is not in list`` and every
    human-mode listing without a curated preset exited 1."""
    rows = [
        {"id": "e-1", "specs": {"gpu": 1}, "name": "alpha", "price": 1.5},
        {"id": "e-2", "specs": {"gpu": 8}, "name": "beta", "price": 2.0},
    ]
    _render._render_generic_rows(rows)  # must not raise
    out = capsys.readouterr().out
    # Scalars keep their original order and the nested dict is pushed last.
    assert out.index("Id") < out.index("Name") < out.index("Price") < out.index("Specs")
    assert "alpha" in out and "beta" in out
