"""Static supply-chain invariants for the storefront container image."""

from pathlib import Path

STOREFRONT_ROOT = Path(__file__).resolve().parents[2]
BASE_IMAGE = (
    "ghcr.io/astral-sh/uv:python3.12-bookworm-slim@"
    "sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58"
)


def test_storefront_base_image_is_immutable_and_architecture_pinned():
    dockerfile = (STOREFRONT_ROOT / "Dockerfile").read_text()
    from_lines = [
        line.strip() for line in dockerfile.splitlines() if line.startswith("FROM ")
    ]

    assert from_lines == [
        f"FROM --platform=linux/amd64 {BASE_IMAGE} AS builder",
        f"FROM --platform=linux/amd64 {BASE_IMAGE} AS runtime-base",
        "FROM runtime-base AS inert-runtime",
        "FROM runtime-base AS active-runtime",
        "FROM active-runtime AS runtime",
    ]


def test_inert_target_excludes_zerotier_and_privilege_layer():
    dockerfile = (STOREFRONT_ROOT / "Dockerfile").read_text()
    inert_target = dockerfile.split("FROM runtime-base AS inert-runtime", 1)[1].split(
        "FROM runtime-base AS active-runtime", 1
    )[0]
    active_target = dockerfile.split("FROM runtime-base AS active-runtime", 1)[1]

    assert "ENV STOREFRONT_ACTIVATION_MODE=inert" in inert_target
    assert "curl -s https://install.zerotier.com" not in inert_target
    assert "NOPASSWD:" not in inert_target
    assert "9993" not in inert_target

    # Backward-compatible default builds still resolve to the active target.
    assert "zerotier-one" in active_target
    assert "FROM active-runtime AS runtime" in active_target


def test_hosted_ci_builds_and_inspects_the_inert_artifact():
    workflow = (STOREFRONT_ROOT.parent / ".github/workflows/storefront-ci.yml").read_text()

    assert "--target inert-runtime" in workflow
    assert "--network none" in workflow
    assert "--read-only" in workflow
    assert "--cap-drop ALL" in workflow
    assert "--security-opt no-new-privileges" in workflow
    assert "! command -v sudo" in workflow
    assert "! command -v zerotier-one" in workflow
    assert "STOREFRONT_ACTIVATION_MODE" in workflow
