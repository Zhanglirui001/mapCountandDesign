"""Release tag routing tests for the automatic updater."""

import pytest

from count_and_design.application import updater


@pytest.mark.parametrize(
    ("tag_name", "expected_channel"),
    [
        ("v1.7.6", updater._CHANNEL_STABLE),
        ("v20260827.123456", updater._CHANNEL_PREVIEW),
        ("v20260827.123456-SIT", None),
    ],
)
def test_release_channel_keeps_sit_out_of_user_channels(
    tag_name: str,
    expected_channel: str | None,
) -> None:
    assert updater._release_channel(tag_name) == expected_channel

