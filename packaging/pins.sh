# shellcheck shell=bash disable=SC2034  # sourced by the scripts that use these
# The uv release a host install uses: pinned, with the sha256 from astral-sh/uv's release
# data (gh api repos/astral-sh/uv/releases/tags/0.12.23, 2026-10-07). Sourced by install.sh.
# Windows pins the same version in packaging/windows/pins.ps1 (a test keeps them equal).
UV_VERSION="0.12.23"
UV_LINUX_X86_64_SHA256="9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6"
UV_LINUX_X86_64_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz"
# The same release for Apple Silicon (the same call, 2026-10-10; the downloaded file hashes
# to it). Used by packaging/macos/install.sh.
UV_MACOS_ARM64_SHA256="50487ae565ccd96e499056b4674d438f4c53170202617b4c759defe0c6a1b544"
UV_MACOS_ARM64_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-aarch64-apple-darwin.tar.gz"
# The Python base image of both Dockerfiles (packaging/docker, and the sandbox's in
# src/lectern/modules/sandbox/image), pinned by the digest of its multi-arch index:
# python:3.12-slim = 3.12.15-slim-trixie (docker buildx imagetools inspect, 2026-10-09).
# A Dockerfile can't read this file, so tests/test_packaging.py checks both FROM lines match it.
PYTHON_IMAGE="python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f"
