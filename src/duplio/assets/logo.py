"""The Duplio mark as terminal art: two faceless, hunched silhouettes peeling apart.

Colourless on purpose. The figure in front is drawn bold and the one behind it
dim, so the logo adapts to any terminal theme. The pixel grid below is the icon
(``assets/duplio-icon.svg``) sampled at low resolution: ``A`` is the front
figure, ``B`` the one behind, and two grid rows make one text row (half blocks).
"""

from __future__ import annotations

from rich.text import Text

GRID = (
    '        AAAA                    BBBB        ',
    '       AAAAAA                  BBBBBB       ',
    '      AAAAAAAA                BBBBBBBB      ',
    '      AAAAAAAA              BBBBBBBBBB      ',
    '      AAAAAAAABBBBBB  BBBBBB  BBBBBBBB      ',
    '      AAAAAAAA                BBBBBBBB      ',
    '      AAAAAAAA                BBBBBBBB      ',
    '      AAAAAAAAB           BBB BBBBBBBB      ',
    '       AAAAAAAAAAAABBBBBBBBBBBBBBBBBB       ',
    '        AAAAAAAAAAAAA  BBBBBBBBBBBBB        ',
    '      AAAAAAAAAAAAAAAAABBBBBBBBBBBBBBB      ',
    '    AAAAAAAAAAAAAAAAAAAABBBBBBBBBBBBBBBB    ',
    '   AAAAAAAAAAAAAAAAAAAAAABBBBBBBBBBBBBBBB   ',
    '  AAAAAAAAAAAAAAAAAAAAAAABBBBBBBBBBBBBBBBB  ',
    ' AAAAAAAAAAAAAAAAAAAAAAAAABBBBBBBBBBBBBBBBB ',
    ' AAAAAAAAAAAAAAAAAAAAAAAAABBBBBBBBBBBBBBBBB ',
    ' AAAAAAAAAAAAAAAAAAAAABAAAABBBBBBBBBBBBBBBB ',
    'AAAAAAAAAAAAAAAAAAAAAABBAAABBBBBBBBBBBBBBBBB',
    'AAAAAAAAAAAAAAAAAAAAA  BAAABBBBBBBBBBBBBBBBB',
    'AAAAAAAAAAAAAAAAAAAAA  BAAAABBBBBBBBBBBBBBBB',
    'AAA AAAAAAAAAAAAAAAAA  BAAAABBBBBBBBBBBB BBB',
    'AAA  AAAAAAAAAAAAAAA    BAAABBBBBBBBBBB  BBB',
    'AAA   AAAAAAAAAAAAAA    BAAABBBBBBBBBB   BBB',
    'AAA    AAAAAAAAAAAAA    BAAABBBBBBBBB    BBB',
    'AAA     AAAAAAAAAAAA    BAAABBBBBBBB     BBB',
    'AAAA     AAAAAAAAAAAA  BBAAABBBBBBB     BBBB',
    ' AAA      AAAAAAAAAAAABBBAAABBBBBB      BBB ',
    ' AAA     AAAAA  AAAAAAABBAAA  BBBBB     BBB ',
    ' AAAA    AAAAA  BAAAAAAABAAA  BBBBB    BBBB ',
    '  AAA    AAAAA    AAAAAABB    BBBBB    BBB  ',
    '  AAAA  AAAAA      AAAAAA      BBBBB  BBBB  ',
    '   AAA  AAAAA      BAAAAA      BBBBB  BBB   ',
    '   AAAA AAAAA     BBAAAAAA     BBBBB BBBB   ',
    '    AA  AAAAA      BBAAAA      BBBBB  BB    ',
    '        AAAAA      BBBAAA      BBBBB        ',
    '        AAAA                    BBBB        ',
    '        AAAA                    BBBB        ',
    '         AAA                     BB         ',
)

# (top pixel filled, bottom pixel filled) -> the half-block character that draws it
_HALF_BLOCKS = {(True, True): "█", (True, False): "▀", (False, True): "▄"}

WORDMARK = "D U P L I O"
TAGLINE = "Duplicates in. One survivor out."


def logo_text(*, wordmark: bool = True) -> Text:
    """The mark as Rich text. It has no colour of its own: style the widget or console."""
    out = Text(no_wrap=True)
    for top_row, bottom_row in zip(GRID[0::2], GRID[1::2], strict=True):
        for top, bottom in zip(top_row, bottom_row, strict=True):
            if top == " " and bottom == " ":
                out.append(" ")
                continue
            glyph = _HALF_BLOCKS[(top != " ", bottom != " ")]
            out.append(glyph, style="bold" if "A" in (top, bottom) else "dim")
        out.append("\n")
    if wordmark:
        out.append(f"{WORDMARK}\n", style="bold")
        out.append(TAGLINE, style="italic dim")
    else:
        out.rstrip()
    return out
