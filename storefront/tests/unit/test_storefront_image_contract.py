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
        f"FROM --platform=linux/amd64 {BASE_IMAGE} AS runtime",
    ]
