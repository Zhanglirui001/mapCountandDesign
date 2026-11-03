from unittest.mock import Mock, patch

from count_and_design.application import updater


def _release(tag: str) -> dict:
    return {
        "tag_name": tag,
        "name": tag,
        "description": "",
        "assets": {
            "links": [
                {
                    "name": f"Count-and-Design-{tag}.exe",
                    "url": f"https://gitlab.example/{tag}.exe",
                }
            ]
        },
    }


def _response(releases: list[dict]) -> Mock:
    response = Mock(status_code=200)
    response.json.return_value = releases
    return response


def test_stable_channel_only_accepts_exact_semantic_version_tags() -> None:
    releases = [
        _release("v20260831.102428-SIT"),
        _release("v20260831.102428"),
        _release("v1.5.2-SIT"),
        _release("release-1.5.3"),
        _release("v1.5"),
        _release("v1.5.1"),
    ]

    with (
        patch.object(updater.requests, "get", return_value=_response(releases)),
        patch.object(updater, "_resolve_sizes", side_effect=lambda items, _token: items),
    ):
        result = updater.fetch_all_releases(
            "group/project", token="token", channel="stable"
        )

    assert [release.tag_name for release in result] == ["v1.5.1"]


def test_preview_channel_only_accepts_exact_timestamp_tags() -> None:
    releases = [
        _release("v20260831.102428-SIT"),
        _release("v1.5.1"),
        _release("v20260831.102428"),
    ]

    with (
        patch.object(updater.requests, "get", return_value=_response(releases)),
        patch.object(updater, "_resolve_sizes", side_effect=lambda items, _token: items),
    ):
        result = updater.fetch_all_releases(
            "group/project", token="token", channel="preview"
        )

    assert [release.tag_name for release in result] == ["v20260831.102428"]


def test_latest_stable_release_skips_branch_specific_tags() -> None:
    releases = [
        _release("v20260831.102428-SIT"),
        _release("v1.5.1-SIT"),
        _release("v1.5.1"),
    ]

    with (
        patch.object(updater.requests, "get", return_value=_response(releases)),
        patch.object(updater, "_resolve_sizes", side_effect=lambda items, _token: items),
    ):
        result = updater.fetch_latest_release(
            "group/project", token="token", channel="stable"
        )

    assert result is not None
    assert result.tag_name == "v1.5.1"
