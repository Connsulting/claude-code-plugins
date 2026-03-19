# Docker Base Image Pinning and Security Scanning

**Type:** pattern
**Topic:** docker, security
**Tags:** docker, base-image, security, security-scanning, container, dockerfile

## Problem

Production images were built from `node:20-slim` without a digest pin. A upstream rebuild introduced a patched OpenSSL version that changed TLS handshake behavior, causing intermittent 502 errors in staging. The team spent hours debugging the application before tracing it to the base image change.

## Solution

Pin base images by SHA256 digest (`FROM node:20-slim@sha256:abc123...`) and run a nightly CI job that checks for newer digests with known CVE fixes. When the job detects a safer digest, it opens a PR to bump the pin. Combine with a multi-stage build so the final image contains only the runtime, reducing the attack surface.

## Why

Tag-based references are mutable. Even "immutable-looking" tags like `20.11.1-slim` can be overwritten by the registry maintainer. Digest pinning guarantees reproducibility, and the nightly scan ensures you still pick up security patches promptly rather than drifting behind.
