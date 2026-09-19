"""LaTeX to Unicode conversion."""

from __future__ import annotations

import pytest

from bibcheck.latex import latex_to_unicode, strip_accents


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Accents in every spelling BibTeX allows for the same character.
        (r"Caf\'{e}", "Café"),
        (r"Caf\'e", "Café"),
        (r"Caf{\'e}", "Café"),
        (r"Caf{\'{e}}", "Café"),
        (r"P{\'{e}}rez", "Pérez"),
        (r'Sch{\"o}lkopf', "Schölkopf"),
        (r"Erd{\H{o}}s", "Erdős"),
        (r"{\c{C}}etin", "Çetin"),
        (r'Na{\"\i}ve', "Naïve"),
        (r"Dvo\v{r}\'{a}k", "Dvořák"),
        (r"Bj{\o}rk", "Bjørk"),
        (r"Stra{\ss}e", "Straße"),
        # Protective braces carry no meaning once parsed.
        (r"A {Study} of {IEEE} 802.11", "A Study of IEEE 802.11"),
        # Escaped punctuation.
        (r"Smith \& Wesson, 100\% Pure", "Smith & Wesson, 100% Pure"),
        (r"C\#\_1", "C#_1"),
        # Dashes and quotes.
        (r"Pages 10--20 and an aside---like this", "Pages 10–20 and an aside—like this"),
        (r"``quoted''", "“quoted”"),
        # Font and math commands wrap content that must survive.
        (r"\textbf{Deep} \emph{Learning}", "Deep Learning"),
        (r"{\it Old Style} Declaration", "Old Style Declaration"),
        (r"The $n$-Body Problem", "The n-Body Problem"),
        (r"$O(n \log n)$ Sorting", "O(n \\log n) Sorting"),
        # Symbols keep the space that terminated their command name.
        (r"Three \ldots and more", "Three … and more"),
        (r"$\alpha$-Divergence", "α-Divergence"),
        # Whitespace, ties and newlines all collapse.
        ("Multi\n  line   title", "Multi line title"),
        (r"Fig.~3 Revisited", "Fig. 3 Revisited"),
        # Plain input is returned untouched.
        ("Attention Is All You Need", "Attention Is All You Need"),
        ("", ""),
    ],
)
def test_latex_to_unicode(raw: str, expected: str) -> None:
    assert latex_to_unicode(raw) == expected


def test_unknown_macro_is_preserved_not_deleted() -> None:
    """An unrecognised macro must stay visible rather than corrupt the title."""
    assert "\\weirdmacro" in latex_to_unicode(r"A \weirdmacro Title")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Café", "Cafe"),
        ("Schölkopf", "Scholkopf"),
        ("Erdős", "Erdos"),
        ("Çetin", "Cetin"),
        ("Bjørk", "Bjork"),
        ("Straße", "Strasse"),
        ("Naïve", "Naive"),
        ("plain ascii", "plain ascii"),
    ],
)
def test_strip_accents(text: str, expected: str) -> None:
    assert strip_accents(text) == expected


def test_accents_and_ascii_agree_after_folding() -> None:
    """The point of the whole module: two spellings, one comparison key."""
    accented = latex_to_unicode(r"Caf\'{e} Culture and Na{\"\i}ve Bayes")
    plain = "Cafe Culture and Naive Bayes"
    assert strip_accents(accented) == plain
    assert accented != plain  # ...but display text keeps the accents.
