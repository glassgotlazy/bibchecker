"""Convert BibTeX/LaTeX field values into plain Unicode text.

Two different jobs are kept apart deliberately:

* :func:`latex_to_unicode` is *semantic*. It produces the text a human should
  read -- ``Caf\\'{e}`` becomes ``Café``, not ``Cafe``. This is what the report
  prints and what ``--fix`` writes back out.
* :mod:`bibcheck.normalize` then does the *aggressive* folding used only for
  equality tests. Never display folded text; never compare unfolded text.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

# A LaTeX accent command mapped to the Unicode combining mark it applies to the
# following base character. Composing and then running NFC gives us the right
# precomposed codepoint without maintaining a table of hundreds of pairs.
_COMBINING: Final[dict[str, str]] = {
    "'": "\u0301",  # acute
    "`": "\u0300",  # grave
    "^": "\u0302",  # circumflex
    '"': "\u0308",  # diaeresis
    "~": "\u0303",  # tilde
    "=": "\u0304",  # macron
    ".": "\u0307",  # dot above
    "u": "\u0306",  # breve
    "v": "\u030c",  # caron / hacek
    "H": "\u030b",  # double acute
    "c": "\u0327",  # cedilla
    "k": "\u0328",  # ogonek
    "r": "\u030a",  # ring above
    "d": "\u0323",  # dot below
    "b": "\u0331",  # macron below
    "t": "\u0361",  # double inverted breve
}

# Standalone commands producing a single character.
_SYMBOLS: Final[dict[str, str]] = {
    "ss": "ß",
    "ae": "æ",
    "AE": "Æ",
    "oe": "œ",
    "OE": "Œ",
    "aa": "å",
    "AA": "Å",
    "o": "ø",
    "O": "Ø",
    "l": "ł",
    "L": "Ł",
    "i": "ı",
    "j": "ȷ",
    "dh": "ð",
    "DH": "Ð",
    "dj": "đ",
    "DJ": "Đ",
    "th": "þ",
    "TH": "Þ",
    "ng": "ŋ",
    "NG": "Ŋ",
    "copyright": "©",
    "pounds": "£",
    "textcopyright": "©",
    "textregistered": "®",
    "texttrademark": "™",
    "textendash": "–",
    "textemdash": "—",
    "textquoteleft": "\u2018",
    "textquoteright": "\u2019",
    "textquotedblleft": "\u201c",
    "textquotedblright": "\u201d",
    "textbackslash": "\\",
    "textasciitilde": "~",
    "textbullet": "•",
    "textdegree": "°",
    "textmu": "µ",
    "textpm": "±",
    "textdiv": "÷",
    "texttimes": "×",
    "ldots": "…",
    "dots": "…",
    "textellipsis": "…",
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "lambda": "λ",
    "mu": "µ",
    "pi": "π",
    "rho": "ρ",
    "sigma": "σ",
    "tau": "τ",
    "phi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Sigma": "Σ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
    "infty": "∞",
    "leq": "≤",
    "geq": "≥",
    "neq": "≠",
    "approx": "≈",
    "times": "×",
    "pm": "±",
    "cdot": "·",
    "rightarrow": "→",
    "leftarrow": "←",
    "to": "→",
}

# Escaped punctuation: ``\&`` -> ``&``.
_ESCAPED: Final[str] = "&%$#_{}"

# Font/layout commands whose argument should survive but whose name should not.
_STRIP_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "emph", "textit", "textbf", "textrm", "textsf", "texttt", "textsc",
        "textnormal", "textsl", "textup", "textmd", "mathrm", "mathbf",
        "mathit", "mathsf", "mathtt", "mathcal", "mbox", "hbox", "text",
        "lowercase", "uppercase", "MakeLowercase", "MakeUppercase", "ensuremath",
        "protect", "relax", "noopsort", "url", "href", "textsuperscript",
        "textsubscript", "bm", "boldsymbol",
        # Old-style declarations: ``{\it Foo}`` rather than ``\textit{Foo}``.
        "it", "bf", "rm", "sc", "sl", "tt", "em", "sf", "normalfont",
        "small", "large", "Large", "LARGE", "footnotesize", "scriptsize",
    }
)

# ``\'{e}``, ``\'e``, ``\'{\i}`` and ``\c{c}`` in one pattern.
_ACCENT_RE: Final[re.Pattern[str]] = re.compile(
    r"\\(['`^\"~=.]|(?:u|v|H|c|k|r|d|b|t)(?=[\s{]))\s*"
    r"(?:\{\s*(?:\\(i|j)\s*|([^{}]))\s*\}|\\(i|j)\b|([A-Za-z]))"
)

_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(r"\\([A-Za-z]+)(?:\{\}|\s+|\b)")
_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(r"\\([" + re.escape(_ESCAPED) + r"])")
_STRIP_CMD_RE: Final[re.Pattern[str]] = re.compile(
    r"\\(" + "|".join(sorted(_STRIP_COMMANDS, key=len, reverse=True)) + r")(?![A-Za-z])\s*"
)
_DOTLESS: Final[dict[str, str]] = {"i": "i", "j": "j"}


def _apply_accent(match: re.Match[str]) -> str:
    command = match.group(1)
    dotless_braced, braced, dotless_bare, bare = match.group(2, 3, 4, 5)
    base = dotless_braced or dotless_bare
    if base is not None:
        # ``\i``/``\j`` are dotless forms that exist only to receive an accent;
        # the precomposed character is built from the dotted letter.
        base = _DOTLESS[base]
    else:
        base = braced if braced is not None else bare or ""
    mark = _COMBINING.get(command, "")
    return unicodedata.normalize("NFC", base + mark)


def _apply_symbol(match: re.Match[str]) -> str:
    name = match.group(1)
    replacement = _SYMBOLS.get(name)
    if replacement is None:
        return match.group(0)
    # ``\ldots and`` must not become ``…and``: the space terminating the
    # command name is a delimiter, but it is also a real space in the text.
    trailing = " " if match.group(0)[-1].isspace() else ""
    return replacement + trailing


def _strip_math(text: str) -> str:
    """Drop ``$`` delimiters but keep what is inside them."""
    text = re.sub(r"\$\$(.+?)\$\$", r"\1", text, flags=re.DOTALL)
    return re.sub(r"(?<!\\)\$(.+?)(?<!\\)\$", r"\1", text, flags=re.DOTALL)


def _strip_braces(text: str) -> str:
    """Remove brace groups that only exist to protect capitalisation."""
    out: list[str] = []
    escaped = False
    for char in text:
        if escaped:
            out.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            out.append(char)
        elif char in "{}":
            continue
        else:
            out.append(char)
    return "".join(out)


def latex_to_unicode(value: str) -> str:
    """Render a raw BibTeX field value as readable Unicode text.

    Accents, escaped punctuation, common symbols and font commands are resolved;
    protective braces and math delimiters are dropped; whitespace is collapsed.
    Anything unrecognised is left alone rather than silently deleted, so an
    unusual macro shows up in the report instead of quietly corrupting a title.
    """
    text = value.replace("\t", " ").replace("\n", " ")
    text = _strip_math(text)
    text = _STRIP_CMD_RE.sub("", text)

    # Accents nest (``{\'{\i}}``), so iterate to a fixed point.
    for _ in range(6):
        substituted = _ACCENT_RE.sub(_apply_accent, text)
        if substituted == text:
            break
        text = substituted

    text = _SYMBOL_RE.sub(_apply_symbol, text)
    text = re.sub(r"(?<!\\)---", "—", text)
    text = re.sub(r"(?<!\\)--", "–", text)
    text = text.replace("``", "\u201c").replace("''", "\u201d")
    text = _ESCAPE_RE.sub(r"\1", text)
    text = _strip_braces(text)
    text = text.replace("~", " ").replace("\\ ", " ")
    text = unicodedata.normalize("NFC", text)
    return re.sub(r"\s+", " ", text).strip()


# Characters NFKD will not decompose: a stroke or ligature is part of the
# letter, not a combining mark, so the fold has to be spelled out.
_ASCII_FOLD_SOURCE: Final[dict[str, str | int | None]] = {
    "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "ß": "ss",
    "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "Th", "ı": "i", "ȷ": "j",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "–": "-", "—": "-", "…": "...",
}
_ASCII_FOLD: Final[dict[int, str | int | None]] = str.maketrans(_ASCII_FOLD_SOURCE)


def strip_accents(text: str) -> str:
    """Fold accented characters to their ASCII base (``Café`` -> ``Cafe``)."""
    # Handle characters that decomposition alone will not reduce.
    text = text.translate(_ASCII_FOLD)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))
