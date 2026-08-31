import pytest

from durable_continue.durations import parse_duration


def test_parse_duration_units() -> None:
    assert parse_duration("30s") == 30
    assert parse_duration("10m") == 600
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400
    assert parse_duration("1.5h") == 5400


@pytest.mark.parametrize("value", ["", "0s", "-1m", "ten minutes", "4w"])
def test_parse_duration_rejects_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(value)
