"""The manual bootstrap path (PRD 11)."""
from __future__ import annotations

import pytest

from asm.seeding import SeedError, parse_seed_file

GOOD = "5yb3D1KBy13czATSYGLUbZrYJvRvFQiH9XYkAeG2nDzF"
GOOD2 = "8psNvWTrdNTiVRNzAgsou9kETXNJm2SXZyaKuJraVRtf"


def write(tmp_path, body: str):
    p = tmp_path / "wallets.txt"
    p.write_text(body)
    return p


def test_parses_addresses_labels_and_notes(tmp_path):
    p = write(tmp_path, f"# comment\n\n{GOOD},gmgn-7d,high winrate\n{GOOD2}\n")
    entries = parse_seed_file(p)
    assert entries == [(GOOD, "gmgn-7d", "high winrate"), (GOOD2, None, None)]


def test_comments_and_blank_lines_ignored(tmp_path):
    p = write(tmp_path, f"\n\n# nothing here\n   \n{GOOD}\n")
    assert len(parse_seed_file(p)) == 1


def test_duplicates_collapsed(tmp_path):
    p = write(tmp_path, f"{GOOD}\n{GOOD}\n{GOOD2}\n")
    assert len(parse_seed_file(p)) == 2


def test_malformed_address_raises_with_line_number(tmp_path):
    """A typo must fail loudly - a silently skipped wallet is one we never watch."""
    p = write(tmp_path, f"{GOOD}\nnot-a-real-address\n")
    with pytest.raises(SeedError, match=r"line|:2:"):
        parse_seed_file(p)


def test_ambiguous_base58_characters_rejected(tmp_path):
    """Base58 excludes 0, O, I and l precisely to stop transcription errors."""
    p = write(tmp_path, "0OIl" + GOOD[4:] + "\n")
    with pytest.raises(SeedError):
        parse_seed_file(p)


def test_missing_file_raises(tmp_path):
    with pytest.raises(SeedError, match="not found"):
        parse_seed_file(tmp_path / "nope.txt")


def test_example_seed_file_is_parseable():
    """The shipped template must not itself be a syntax error."""
    assert parse_seed_file("seeds/wallets.example.txt") == []
