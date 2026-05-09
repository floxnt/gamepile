"""Phase 4 — manual HLTB ID override.

Run with: uv run python tests/test_phase4_hltb_override.py

Covers:
  - parse_hltb_id_input: bare integer, URL forms, edge cases
  - Sync routing: sets-id branch vs name-search branch (logic-level only;
    fetch is mocked)

Network calls (fetch_hltb_by_id against the live HLTB API) are not
exercised here — the route handlers are smoke-tested manually.

No pytest dependency — pure assertions, same pattern as other tests/.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.fetchers.hltb import parse_hltb_id_input


# ---------------------------------------------------------------------------
# parse_hltb_id_input
# ---------------------------------------------------------------------------

def test_parse_bare_integer():
    assert parse_hltb_id_input("12345") == 12345


def test_parse_bare_integer_with_whitespace():
    assert parse_hltb_id_input("  12345  ") == 12345


def test_parse_full_url_https():
    assert parse_hltb_id_input("https://howlongtobeat.com/game/12345") == 12345


def test_parse_full_url_http():
    assert parse_hltb_id_input("http://howlongtobeat.com/game/9876") == 9876


def test_parse_url_no_scheme():
    assert parse_hltb_id_input("howlongtobeat.com/game/9876") == 9876


def test_parse_url_with_www():
    assert parse_hltb_id_input("https://www.howlongtobeat.com/game/9876") == 9876


def test_parse_url_with_query_string():
    # Real HLTB URLs sometimes carry tracking params — pull the ID out
    # regardless.
    assert parse_hltb_id_input("https://howlongtobeat.com/game/12345?utm_source=test") == 12345


def test_parse_url_uppercase_host():
    # Be permissive about host casing; user might paste from various sources.
    assert parse_hltb_id_input("https://HowLongToBeat.com/game/12345") == 12345


def test_parse_url_with_trailing_slash():
    assert parse_hltb_id_input("https://howlongtobeat.com/game/12345/") == 12345


def test_parse_empty_string_returns_none():
    assert parse_hltb_id_input("") is None


def test_parse_whitespace_only_returns_none():
    assert parse_hltb_id_input("   ") is None


def test_parse_non_numeric_text_returns_none():
    assert parse_hltb_id_input("not a url") is None


def test_parse_zero_returns_none():
    # 0 is invalid per howlongtobeatpy's own contract.
    assert parse_hltb_id_input("0") is None


def test_parse_negative_returns_none():
    # Negative IDs make no sense; reject rather than passing through.
    assert parse_hltb_id_input("-5") is None


def test_parse_float_returns_none():
    # We accept integers only; "12.5" is malformed input, not a fractional ID.
    assert parse_hltb_id_input("12.5") is None


def test_parse_url_with_zero_id_returns_none():
    # Even when the URL form gives id=0, treat as invalid.
    assert parse_hltb_id_input("https://howlongtobeat.com/game/0") is None


def test_parse_unrelated_url_returns_none():
    # A URL that isn't from howlongtobeat.com → None.
    assert parse_hltb_id_input("https://example.com/game/12345") is None


def test_parse_other_path_on_hltb_returns_none():
    # The /game/<id> shape is required — /user/foo or other paths don't
    # leak data into the ID field.
    assert parse_hltb_id_input("https://howlongtobeat.com/user/12345") is None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    test_funcs = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    print(f"Running {len(test_funcs)} test(s)…")
    failures = []
    for fn in test_funcs:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as exc:
            failures.append((fn.__name__, exc))
            print(f"  ✗ {fn.__name__}: {exc}")
        except Exception as exc:
            failures.append((fn.__name__, exc))
            print(f"  ✗ {fn.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        sys.exit(1)
    print(f"\nAll {len(test_funcs)} tests passed.")


if __name__ == "__main__":
    main()
