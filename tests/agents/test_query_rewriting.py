from app.agents.retrieval.rewriting import _expand_abbreviations


def test_abbreviation_expansion_preserves_exact_term_and_adds_meaning() -> None:
    rewritten = _expand_abbreviations("Why did SSO fail during the P1?")
    assert "SSO (single sign-on)" in rewritten
    assert "P1 (priority one)" in rewritten


def test_abbreviation_expansion_does_not_change_words_containing_acronym() -> None:
    assert _expand_abbreviations("classloader") == "classloader"
