"""
Game-type classification — pure-functional.

Replaces the four-way `compute_game_type()` formerly in `app/backlog.py`
with the eleven-type taxonomy from SPEC_V3_GAME_TYPE_CLASSIFICATION.md.
The new types control how the v3 Phase 1a engagement metrics are
displayed (and, in Phase 1b, categorized) per game.

Detection rules apply top-to-bottom; first match wins. Order matters —
beta detection runs before multiplayer because a "Counter-Strike Beta"
should classify as beta even though it'd otherwise fit multiplayer.
Software runs second because some software has weird Steam categories
that would otherwise misfire.

No DB / FastAPI imports — keep this layer testable in isolation.
"""

from typing import Optional


# ---------------------------------------------------------------------------
# Type values
# ---------------------------------------------------------------------------

GAME_TYPE_LINEAR = "linear"
GAME_TYPE_MULTIPLAYER = "multiplayer"
GAME_TYPE_NO_ENDPOINT = "no_endpoint"
GAME_TYPE_MIXED = "mixed"
GAME_TYPE_MMO = "mmo"
GAME_TYPE_SANDBOX = "sandbox"
GAME_TYPE_BETA_PLAYTEST = "beta_playtest"
GAME_TYPE_SOFTWARE = "software"
GAME_TYPE_EXPANSION = "expansion"
GAME_TYPE_EARLY_ACCESS = "early_access"
GAME_TYPE_UNKNOWN = "unknown"

ALL_GAME_TYPES: tuple = (
    GAME_TYPE_LINEAR,
    GAME_TYPE_MULTIPLAYER,
    GAME_TYPE_NO_ENDPOINT,
    GAME_TYPE_MIXED,
    GAME_TYPE_MMO,
    GAME_TYPE_SANDBOX,
    GAME_TYPE_BETA_PLAYTEST,
    GAME_TYPE_SOFTWARE,
    GAME_TYPE_EXPANSION,
    GAME_TYPE_EARLY_ACCESS,
    GAME_TYPE_UNKNOWN,
)

GAME_TYPE_LABELS = {
    GAME_TYPE_LINEAR: "Linear",
    GAME_TYPE_MULTIPLAYER: "Multiplayer",
    GAME_TYPE_NO_ENDPOINT: "No endpoint",
    GAME_TYPE_MIXED: "Mixed",
    GAME_TYPE_MMO: "MMO",
    GAME_TYPE_SANDBOX: "Sandbox",
    GAME_TYPE_BETA_PLAYTEST: "Beta / Playtest",
    GAME_TYPE_SOFTWARE: "Software",
    GAME_TYPE_EXPANSION: "Expansion",
    GAME_TYPE_EARLY_ACCESS: "Early Access",
    GAME_TYPE_UNKNOWN: "Unknown",
}

GAME_TYPE_TOOLTIPS = {
    GAME_TYPE_LINEAR: "Linear / story-driven — has a defined main path",
    GAME_TYPE_MULTIPLAYER: "Multiplayer focus — no single-player campaign",
    GAME_TYPE_NO_ENDPOINT: "No defined endpoint — sandbox, roguelike, or open-ended",
    GAME_TYPE_MIXED: "Mixed — single-player campaign with multiplayer modes",
    GAME_TYPE_MMO: "MMO — persistent online multiplayer world",
    GAME_TYPE_SANDBOX: "Sandbox — open-ended creative play with HLTB main present",
    GAME_TYPE_BETA_PLAYTEST: "Beta or playtest — data may be unstable",
    GAME_TYPE_SOFTWARE: "Software / utility — not a game",
    GAME_TYPE_EXPANSION: "Expansion / DLC — extends a base game",
    GAME_TYPE_EARLY_ACCESS: "Early Access — still in development, data still settling",
    GAME_TYPE_UNKNOWN: "Type detection failed — signals are best-effort",
}


# ---------------------------------------------------------------------------
# Detection rule constants
# ---------------------------------------------------------------------------

# Title substrings that mark a game as a beta / playtest / test build.
# Case-insensitive substring match against game.name. Highest priority —
# runs before everything else because a "Counter-Strike Beta" should
# classify as beta even though it'd otherwise fit multiplayer.
BETA_KEYWORDS: tuple = (
    "beta",
    "playtest",
    "public test",
    "pts",
    "test server",
    "testing branch",
    "staging branch",
    "unstable",
)

# Curated list of appids that are definitively software/utility apps.
# Purpose: catch software that genre/app_type heuristics miss. Currently
# empty — the initial three candidates (3DMark 223850, Wallpaper Engine
# 431960, Lossless Scaling 993090) were all redundant with the
# "Utilities" genre hint per the checkpoint 1 distribution check, so they
# were removed. Add an entry here only when a software/utility game
# slips past both NON_GAME_APP_TYPES and SOFTWARE_GENRE_HINTS.
SOFTWARE_APPID_LIST: tuple = ()

# Steam genre names that strongly indicate software/utility content rather
# than a game. Case-insensitive lookup against game.genre_list(). Permissive
# — false positives mostly catch genuinely non-game content; user can
# override via the Game Detail Type dropdown.
SOFTWARE_GENRE_HINTS: frozenset = frozenset({
    "utilities",
    "animation & modeling",
    "audio production",
    "design & illustration",
    "education",
    "web publishing",
    "software training",
    "photo editing",
    "video production",
})

# Steam app_type values that count as software (catches non-game apps that
# Steam categorizes precisely). "game" / "dlc" / "demo" never trigger this.
NON_GAME_APP_TYPES: frozenset = frozenset({
    "advertising",
    "music",
    "video",
    "tool",
    "series",
    "episode",
    "hardware",
})

# MMO patterns — checked against BOTH user_tags AND Steam categories
# (case-insensitive substring).
MMO_PATTERNS: tuple = ("mmo", "massively multiplayer")

# SteamSpy user_tag substring used to discriminate "genuinely Mixed"
# (single-player campaign + meaningful multiplayer modes — Halo MCC,
# Borderlands, L4D2, Destiny 2) from "predominantly single-player with
# light multiplayer features" (Souls-likes, etc.).
#
# Steam categories alone can't make this distinction: Souls-likes carry
# the same Multi-player / Co-op / PvP subcategory profile as legitimately-
# mixed games because of FromSoftware's invasion / summoning system.
# But the "Co-op" SteamSpy user_tag is set only when player-perceived
# co-op is a real feature — a 5/5 vs 0/6 split across the verified
# sample (Halo MCC / Borderlands 2+3 / L4D2 / Destiny 2 all have
# "Co-op"; Elden Ring / DS3 / Sekiro / Cyberpunk / Witcher 3 / Skyrim
# all do not, despite Souls-likes carrying every relevant Steam
# subcategory).
#
# Substring match catches "Co-op" and "Online Co-Op" both. Case-
# insensitive — see _has_coop_user_tag helper.
COOP_USER_TAG_SUBSTRING = "co-op"

# User-tag substrings that mark a game as no_endpoint regardless of HLTB
# data (Roguelike / Roguelite / Rogue-like / Rogue-lite / Action Roguelike).
# Substring match is intentional — SteamSpy tags vary in form.
NO_ENDPOINT_TAG_SUBSTRINGS: tuple = ("rogue",)

# Open-world / sandbox tag substrings — used when HLTB main is missing
# (matches the prior is_forever_game logic) and as the disambiguator for
# the sandbox / no_endpoint split.
OPEN_WORLD_TAG_SUBSTRINGS: tuple = ("sandbox", "open world")

# Completionist-to-main ratio above which a game classifies as no_endpoint
# even without an explicit no_endpoint tag (covers Civ VI, MMOs without
# tags, etc.). Sandbox-tagged games at exactly this ratio still classify
# as sandbox via the `<=` check in rule 8 (inclusive).
STRONG_NO_ENDPOINT_RATIO = 7.0


# ---------------------------------------------------------------------------
# Helper predicates
# ---------------------------------------------------------------------------

def _name_has_beta_keyword(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in BETA_KEYWORDS)


def _genre_set(game) -> set:
    return {g.lower() for g in game.genre_list()}


def _category_set(game) -> set:
    """Steam categories live in the `tags` column (a v1 quirk)."""
    return {t.lower() for t in game.tag_list()}


def _user_tag_set(game) -> set:
    return {t.lower() for t in game.user_tags_list()}


def _has_substring(haystacks, needles) -> bool:
    """True if any haystack contains any needle as a substring."""
    for h in haystacks:
        for n in needles:
            if n in h:
                return True
    return False


def _is_software_by_curation(game) -> bool:
    return game.appid in SOFTWARE_APPID_LIST


def _is_software_by_app_type(game) -> bool:
    if not game.app_type:
        return False
    return game.app_type.lower() in NON_GAME_APP_TYPES


def _is_software_by_genre(game) -> bool:
    return bool(_genre_set(game) & SOFTWARE_GENRE_HINTS)


def _is_dlc(game) -> bool:
    return (game.app_type or "").lower() == "dlc"


def _is_early_access_genre(game) -> bool:
    return any("early access" in g for g in _genre_set(game))


def _is_mmo(game) -> bool:
    return _has_substring(_user_tag_set(game), MMO_PATTERNS) or \
           _has_substring(_category_set(game), MMO_PATTERNS)


def _has_coop_user_tag(game) -> bool:
    """True when "Co-op" (or "Online Co-Op", etc.) appears in SteamSpy
    user_tags. Case-insensitive substring match. The discriminator
    between genuinely-Mixed games and Souls-style "single-player with
    co-op invasions" — see COOP_USER_TAG_SUBSTRING for the empirical
    rationale.

    Niche indies with no SteamSpy votes return False (empty tag list),
    falling through to linear by default. Defensive: misclassifying a
    rare co-op indie as linear is less bad than misclassifying every
    Souls-like as mixed.
    """
    for t in game.user_tags_list():
        if COOP_USER_TAG_SUBSTRING in t.lower():
            return True
    return False


def _is_no_endpoint_tag(game) -> bool:
    return _has_substring(_user_tag_set(game), NO_ENDPOINT_TAG_SUBSTRINGS)


def _has_sandbox_tag(game) -> bool:
    return "sandbox" in _user_tag_set(game)


def _has_openworld_or_sandbox_tag(game) -> bool:
    return _has_substring(_user_tag_set(game), OPEN_WORLD_TAG_SUBSTRINGS)


def _completionist_ratio(game) -> Optional[float]:
    h = game.hltb_main_hours
    c = game.hltb_completionist_hours
    if not h or h <= 0 or not c:
        return None
    return c / h


# ---------------------------------------------------------------------------
# Main classifier
# ---------------------------------------------------------------------------

def classify_game(game, coming_soon: bool = False) -> str:
    """Classify `game` into one of the 11 GAME_TYPE_* values.

    `coming_soon`: from the Steam appdetails `release_date.coming_soon`
    field. When True AND the user has any playtime, the game is
    classified as Early Access even without the corresponding genre.
    Default False — most callers (Library Jinja global, Game Detail
    type dropdown rendering) don't have access to this transient signal
    and don't need it; only sync.py provides it from the freshly-fetched
    appdetails response.
    """
    cats = _category_set(game)
    has_mp = "multi-player" in cats
    has_sp = "single-player" in cats

    # Rule 1: beta_playtest — title keyword wins over everything.
    if _name_has_beta_keyword(game.name):
        return GAME_TYPE_BETA_PLAYTEST

    # Rule 2: software — curated list, then app_type, then genre hints.
    if _is_software_by_curation(game):
        return GAME_TYPE_SOFTWARE
    if _is_software_by_app_type(game):
        return GAME_TYPE_SOFTWARE
    if _is_software_by_genre(game):
        return GAME_TYPE_SOFTWARE

    # Rule 3: expansion — Steam app_type='dlc'.
    if _is_dlc(game):
        return GAME_TYPE_EXPANSION

    # Rule 4: early_access — Early Access genre OR coming_soon+playtime.
    if _is_early_access_genre(game):
        return GAME_TYPE_EARLY_ACCESS
    if coming_soon and game.playtime_minutes and game.playtime_minutes > 0:
        return GAME_TYPE_EARLY_ACCESS

    # Rule 5: mmo — user tags or Steam categories.
    if _is_mmo(game):
        return GAME_TYPE_MMO

    # Rule 6: multiplayer — mp category, no sp category.
    if has_mp and not has_sp:
        return GAME_TYPE_MULTIPLAYER

    # Rule 7: mixed — Single-player Steam category AND HLTB main present
    # AND "Co-op" in SteamSpy user_tags. The user_tag check is what
    # discriminates genuinely-Mixed games (Halo MCC, Borderlands, L4D2,
    # Destiny 2 — all carry Co-op as a SteamSpy user_tag) from
    # Souls-style games (Elden Ring, DS3, Sekiro — none carry Co-op
    # despite all carrying every relevant Steam subcategory).
    #
    # Steam category subcategories are NOT used here — they're identical
    # between Souls-likes and pre-cross-plat dedicated-MP games (e.g.
    # Borderlands 2 has the same 'Multi-player + Co-op' profile as DS3),
    # so they can't carry the discrimination. The SteamSpy user_tag
    # reflects player-perceived co-op rather than developer-set Steam
    # categories.
    if has_sp and game.hltb_main_hours and _has_coop_user_tag(game):
        return GAME_TYPE_MIXED

    # Rule 8: sandbox — Sandbox tag AND HLTB main present AND
    # completionist:main <= 7x (inclusive — sandbox-tagged games at the
    # threshold should classify as sandbox, not fall through to linear).
    if _has_sandbox_tag(game) and game.hltb_main_hours:
        ratio = _completionist_ratio(game)
        if ratio is None or ratio <= STRONG_NO_ENDPOINT_RATIO:
            return GAME_TYPE_SANDBOX

    # Rule 9: no_endpoint — Roguelike/Roguelite tag, OR completionist >>
    # main, OR HLTB main missing AND open-world/sandbox-tagged.
    if _is_no_endpoint_tag(game):
        return GAME_TYPE_NO_ENDPOINT
    ratio = _completionist_ratio(game)
    if ratio is not None and ratio > STRONG_NO_ENDPOINT_RATIO:
        return GAME_TYPE_NO_ENDPOINT
    if not game.hltb_main_hours and _has_openworld_or_sandbox_tag(game):
        return GAME_TYPE_NO_ENDPOINT

    # Rule 10: linear — has Single-player AND HLTB main.
    if has_sp and game.hltb_main_hours:
        return GAME_TYPE_LINEAR

    # Rule 11: unknown — none of the above. Rare; e.g., apps with no
    # metadata at all, or Single-player games with no HLTB main and no
    # tag signals. Display layer treats this as "best-effort linear".
    return GAME_TYPE_UNKNOWN


# ---------------------------------------------------------------------------
# Forever-game derivation (replaces app/backlog.is_forever_game)
# ---------------------------------------------------------------------------

# Game types that count as "forever" for Backlog filtering / Shortlist
# eligibility purposes. Multiplayer-focus games and MMOs have no main
# story to track; no_endpoint is grindy/roguelite by definition;
# sandbox is open-ended creative.
_FOREVER_GAME_TYPES: frozenset = frozenset({
    GAME_TYPE_MULTIPLAYER,
    GAME_TYPE_MMO,
    GAME_TYPE_NO_ENDPOINT,
    GAME_TYPE_SANDBOX,
})


def is_forever_game(game) -> bool:
    """True when the game is one of the 'no defined endpoint' types.

    Reads `game.game_type` when populated (the cached classification);
    falls back to running classify_game inline for games whose type
    column is still NULL (the migration window before the first refresh
    after this work lands). Backlog logic depends on this returning the
    right answer for every row, regardless of refresh state.
    """
    gt = game.game_type
    if gt is None:
        gt = classify_game(game)
    return gt in _FOREVER_GAME_TYPES


# ---------------------------------------------------------------------------
# Resolution helper for templates
# ---------------------------------------------------------------------------

def resolve_type(game) -> str:
    """Return the cached game_type when set, else classify on the fly.

    Used by the Library row template and the Game Detail engagement
    section. During the migration window (before the first refresh
    populates game_type for the existing library), this falls back to
    a fresh classification so the UI doesn't show "Unknown" placeholders
    for every row.
    """
    return game.game_type or classify_game(game)


# ---------------------------------------------------------------------------
# Display rules per type
# ---------------------------------------------------------------------------

# Per the spec table — which Phase 1a engagement metrics are surfaced for
# each game type, plus the categorical-badge eligibility flag (Phase 1b)
# and an optional caveat string shown alongside the rendered metrics.
#
# `~` in the spec ("data shown if present, but with caveat") collapses to
# show=True + caveat populated; the template handles both branches the
# same way — show the row, surface the caveat once.
_DISPLAY_RULES = {
    GAME_TYPE_LINEAR: {
        "show_completion_rate": True, "show_cliff_metric": True,
        "show_review_playtime": True, "show_stickiness_ratio": True,
        "show_playtime_ratio": True, "categorical_badge_eligible": True,
        "caveat_text": None,
    },
    GAME_TYPE_MIXED: {
        "show_completion_rate": True, "show_cliff_metric": True,
        "show_review_playtime": True, "show_stickiness_ratio": True,
        "show_playtime_ratio": True, "categorical_badge_eligible": True,
        "caveat_text": None,
    },
    GAME_TYPE_MULTIPLAYER: {
        "show_completion_rate": False, "show_cliff_metric": False,
        "show_review_playtime": True, "show_stickiness_ratio": True,
        "show_playtime_ratio": True, "categorical_badge_eligible": True,
        "caveat_text": None,
    },
    GAME_TYPE_MMO: {
        "show_completion_rate": False, "show_cliff_metric": False,
        "show_review_playtime": True, "show_stickiness_ratio": True,
        "show_playtime_ratio": True, "categorical_badge_eligible": True,
        "caveat_text": None,
    },
    GAME_TYPE_NO_ENDPOINT: {
        "show_completion_rate": False, "show_cliff_metric": False,
        "show_review_playtime": True, "show_stickiness_ratio": True,
        "show_playtime_ratio": True, "categorical_badge_eligible": True,
        "caveat_text": None,
    },
    GAME_TYPE_SANDBOX: {
        "show_completion_rate": False, "show_cliff_metric": False,
        "show_review_playtime": True, "show_stickiness_ratio": True,
        "show_playtime_ratio": True, "categorical_badge_eligible": True,
        "caveat_text": None,
    },
    GAME_TYPE_BETA_PLAYTEST: {
        "show_completion_rate": True, "show_cliff_metric": True,
        "show_review_playtime": True, "show_stickiness_ratio": True,
        "show_playtime_ratio": True, "categorical_badge_eligible": False,
        "caveat_text": "Data may be unstable for beta/playtest titles.",
    },
    GAME_TYPE_EARLY_ACCESS: {
        "show_completion_rate": True, "show_cliff_metric": True,
        "show_review_playtime": True, "show_stickiness_ratio": True,
        "show_playtime_ratio": True, "categorical_badge_eligible": False,
        "caveat_text": "Data still settling for Early Access titles.",
    },
    GAME_TYPE_EXPANSION: {
        "show_completion_rate": True, "show_cliff_metric": True,
        "show_review_playtime": True, "show_stickiness_ratio": True,
        "show_playtime_ratio": True, "categorical_badge_eligible": True,
        "caveat_text": "Treated as a standalone game; data may be limited.",
    },
    GAME_TYPE_SOFTWARE: {
        "show_completion_rate": False, "show_cliff_metric": False,
        "show_review_playtime": False, "show_stickiness_ratio": False,
        "show_playtime_ratio": False, "categorical_badge_eligible": False,
        "caveat_text": "Engagement signals don't apply to software/utilities.",
    },
    GAME_TYPE_UNKNOWN: {
        "show_completion_rate": True, "show_cliff_metric": True,
        "show_review_playtime": True, "show_stickiness_ratio": True,
        "show_playtime_ratio": True, "categorical_badge_eligible": False,
        "caveat_text": "Type detection failed; signals best-effort.",
    },
}


def engagement_display_rules(game_type: Optional[str]) -> dict:
    """Return the display-rule dict for a game_type. Unknown / None types
    fall back to the unknown ruleset (show all, mark best-effort, no
    badge).

    The dict has 7 keys:
      show_completion_rate, show_cliff_metric, show_review_playtime,
      show_stickiness_ratio, show_playtime_ratio (all bool),
      categorical_badge_eligible (bool, used by Phase 1b),
      caveat_text (str or None — surfaced under the section header).
    """
    return _DISPLAY_RULES.get(game_type) or _DISPLAY_RULES[GAME_TYPE_UNKNOWN]
