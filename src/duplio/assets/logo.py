"""The Duplio mark for the terminal: the icon (``assets/duplio-icon-original.png``) sampled
into colour half-block characters.

``GRID`` holds the picture at 38x40 pixels, one palette index per pixel (space = transparent);
two grid rows make one text row, drawn with ``▀`` (top pixel as foreground, bottom pixel as
background). Terminals without true colour get the nearest palette colours automatically.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

PALETTE = (
    '#ffffff', '#fffdfd', '#fdfafc', '#fbe8ee', '#ecf3fb', '#c3e6f9', '#f9cedb', '#c4ccf1', '#ecadc8', '#8fcdf3', '#87b8ec', '#57aeeb', '#f48fab', '#f3668b', '#a67baf', '#2e8ed5', '#227ac2', '#0c5ea7', '#d43c62', '#c4274c', '#a91237', '#7a0b2a', '#65041e', '#53031a', '#06417c', '#04376b', '#042c56', '#031f3e', '#031831', '#1d0b20',
)

GRID = (
    '       9b9                            ',
    '      9hoha                           ',
    '      fqsqf                           ',
    '      frsqb                      dji  ',
    '      bqso9                     dlnni ',
    '       hrh5                     jnttk ',
    '      5hsob                    ckntni ',
    '    49fpsrhb5                68imnnk8 ',
    '   9fhoqrqogbb5            cddiijkkc  ',
    '  9hoprrsrpog955         8cddkllkc    ',
    '  boprssssqghbfb75        cdjlntnj    ',
    '  bphqssssqqfa7a     8   cdjknttnk6   ',
    '  bqoorsssqhhb77       6ccjjkntnnj    ',
    '  fqghqsssqggbaaa77 68cc8dillnnlni    ',
    '  gqbfqsssrobaabaae8888dccjlnnkjnj    ',
    ' 5ho9fpsssrog955773    8ddjknmidnj    ',
    ' bph fpsssqofa777     8dddjlnkc8mk    ',
    ' gqf fqsssphfb97      cdcdjmnkc lni   ',
    ' ho9 grsssqhhaa7     cd8cdknnkc 8llc  ',
    '5of 9orsssrhgb9a778cc68 6iknnld   kk  ',
    '9o  bpssssspfaaaaee6    cilnnnj6   ik ',
    'bh  fqsrprsqgb7 57     cddkmnnmi    lc',
    'fp9 grsqgpsqg977a     88cjjkmtnkc   kj',
    'gga grsoborqgaaf7    6  cikmkmtni   di',
    'af  grrh5grqf57fb7788  8djlnkknnl8  c ',
    ' a  frrf fqqb bffe6 8    ilmicknni    ',
    '    bqqb bppbafb7        imlc dlnk    ',
    '    9ppb 9opffb9        8jmk   inl8   ',
    '    9oo9 5hpbba         8kli   jnl    ',
    '    9oo5  gpfb         8jlk8  jnmd    ',
    '    bqp9  gqg5        8kmkc  inni     ',
    '    fqp9  hrh         imld   knk      ',
    '    bqo9  grh        8lmi   cml8      ',
    '    aph   fqg        dlj    jmc       ',
    '     og   aqf        kk    cmi        ',
    '     og   9pa       il8   cll         ',
    '     og   9oa      ilk    knk         ',
    '    9og   aob      knk     ml         ',
    '   5gpg   bpg5      imc    cmc        ',
    '   goga   aopf       ik     ek        ',
)

_DIGITS = "0123456789abcdefghijklmnopqrstuv"

WORDMARK = "D U P L I O"
TAGLINE = "Duplicates in. One survivor out."


def logo_text(*, wordmark: bool = True) -> Text:
    """The mark as Rich text."""
    out = Text(no_wrap=True)
    for top_row, bottom_row in zip(GRID[0::2], GRID[1::2], strict=True):
        for top, bottom in zip(top_row, bottom_row, strict=True):
            if top == " " and bottom == " ":
                out.append(" ")
            elif top != " " and bottom != " ":
                out.append("▀", style=Style(color=PALETTE[_DIGITS.index(top)], bgcolor=PALETTE[_DIGITS.index(bottom)]))
            elif top != " ":
                out.append("▀", style=Style(color=PALETTE[_DIGITS.index(top)]))
            else:
                out.append("▄", style=Style(color=PALETTE[_DIGITS.index(bottom)]))
        out.append("\n")
    if wordmark:
        out.append(f"{WORDMARK}\n", style="bold")
        out.append(TAGLINE, style="italic dim")
    else:
        out.rstrip()
    return out
