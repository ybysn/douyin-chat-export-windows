"""Safety contracts for GitHub Actions publishing."""

from pathlib import Path


def test_ghcr_publish_is_limited_to_the_canonical_repository():
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    assert "github.event_name == 'push'" in workflow
    assert "github.repository == 'TeamBreakerr/douyin-chat-export'" in workflow
    assert "images: ghcr.io/teambreakerr/douyin-chat-export" in workflow
