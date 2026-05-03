"""Test fixtures for app.fetchers.hltb.clean_title.

Run with: uv run python tests/test_hltb_cleaner.py

No pytest dependency — pure assertions. Add a case here whenever a real-
library title is observed to fail HLTB lookup, then add the matching
cleaner change in app/fetchers/hltb.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.fetchers.hltb import clean_title


CASES = [
    # ----- Trademark handling -----
    ("ACE COMBAT™7", "ACE COMBAT 7"),                # no trailing space → space inserted
    ("Halo™ Reach", "Halo Reach"),                   # already had space → still single space
    ("Halo™", "Halo"),                               # trailing trademark
    ("™Foo", "Foo"),                                 # leading trademark
    ("A™B™C", "A B C"),                              # multiple trademarks

    # ----- Edition + dangling-trailing residue -----
    ("Nioh 2 – The Complete Edition", "Nioh 2"),     # em-dash + The + suffix
    ("Game - A Complete Edition", "Game"),           # hyphen + A + suffix
    ("Game — An Definitive Edition", "Game"),        # em-dash + An + suffix
    ("Foo Bar -", "Foo Bar"),                        # bare trailing dash
    ("Foo Bar – The", "Foo Bar"),                    # em-dash + The (already-residue)

    # ----- Newly-supported suffixes -----
    ("Game Reloaded Edition", "Game"),
    ("Wasteland 1 - The Original Classic", "Wasteland 1"),
    ("Wasteland Original Classic", "Wasteland"),
    ("Game Remaster", "Game"),                       # standalone "Remaster"
    ("Game remastered", "Game"),                     # case-insensitive
    ("Game REMASTERED", "Game"),                     # case-insensitive
    ("Game GOTY", "Game"),

    # ----- Compound (multiple ops at once) -----
    ("Legacy of Kain™ Soul Reaver 1&2 Remastered", "Legacy of Kain Soul Reaver 1&2"),
    ("Foo™ - The Complete Edition Remastered", "Foo"),  # trademark + dangling + double suffix
    ("Game (2024) GOTY Edition", "Game"),            # year + edition

    # ----- Internal hyphens preserved -----
    (
        "Penny Arcade's On the Rain-Slick Precipice of Darkness 3",
        "Penny Arcade's On the Rain-Slick Precipice of Darkness 3",
    ),
    ("X-COM: UFO Defense", "X-COM: UFO Defense"),
    ("Spider-Man", "Spider-Man"),

    # ----- Untouched titles (regression sanity) -----
    ("Hades", "Hades"),
    ("Portal 2", "Portal 2"),
    ("Counter-Strike 2", "Counter-Strike 2"),

    # ----- Year parens -----
    ("Some Game (2024)", "Some Game"),
    ("Some Game (2024) ", "Some Game"),
]


def main() -> int:
    failures = 0
    for raw, expected in CASES:
        actual = clean_title(raw)
        if actual != expected:
            print(f"FAIL: clean_title({raw!r}) = {actual!r} — expected {expected!r}")
            failures += 1
    if failures:
        print(f"\n{failures}/{len(CASES)} cases failed")
        return 1
    print(f"\nall {len(CASES)} cases pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
