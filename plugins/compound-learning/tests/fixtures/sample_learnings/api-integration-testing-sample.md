# API Integration Testing with Contract Validation

**Type:** pattern
**Topic:** testing
**Tags:** testing, integration-tests, api-testing, verification, fixtures

## Problem

Unit tests mocked all HTTP calls and passed consistently, but production integrations broke after an upstream provider changed their response schema. The mock fixtures were months stale and no longer matched the real API. The team only discovered the breakage when customers reported errors.

## Solution

Add a contract testing layer alongside unit tests. Record real API responses as fixtures using a VCR-style library (e.g., `vcrpy` for Python, `nock` for Node) during an initial recording session. Run these recorded tests in CI for speed. On a weekly schedule, replay the tests against the live sandbox API in "record" mode to refresh fixtures. If the refreshed fixtures differ from the committed ones, the CI job fails and opens a PR with the updated fixtures for review.

## Why

Mocks and fixtures drift from reality unless actively maintained. Contract tests bridge the gap between fast unit tests and slow live integration tests by verifying that the assumptions encoded in your mocks still hold. The weekly refresh cadence catches schema changes before they reach production without requiring live API calls on every PR.
