from __future__ import annotations

import pytest

from app.config import parse_size_bytes


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("104857600", 100 * 1024**2),
        ("100M", 100 * 1024**2),
        ("1.5G", int(1.5 * 1024**3)),
        ("512MiB", 512 * 1024**2),
        ("2T", 2 * 1024**4),
        (" 10 k ", 10 * 1024),
    ],
)
def test_parse_size_bytes(value: str, expected: int):
    assert parse_size_bytes(value) == expected


@pytest.mark.parametrize("value", ["", "10X", "-1G", "0B", "one-megabyte"])
def test_parse_size_bytes_rejects_invalid_values(value: str):
    with pytest.raises(ValueError):
        parse_size_bytes(value)
