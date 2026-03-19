# GitHub Actions CI Pipeline Caching Strategy

**Type:** pattern
**Topic:** cicd
**Tags:** cicd, github-actions, pipeline, ci-cd, workflow, ci

## Problem

CI builds took 12 minutes because every run installed all Python and Node dependencies from scratch. The `actions/cache` step was configured but had a low hit rate because the cache key included the full OS version string, which changed on every GitHub runner image update (roughly weekly).

## Solution

Key caches on the lock file hash only (`hashFiles('**/poetry.lock', '**/package-lock.json')`) and use a restore-keys fallback that matches the partial prefix so a stale cache still provides most packages. Split caches by ecosystem (one for pip/poetry, one for npm) so a change in Python deps does not invalidate the Node cache. Add a monthly scheduled workflow that warms the cache on main to prevent cold starts on Monday mornings.

## Why

Dependency installation dominates CI wall-clock time for most projects. A well-keyed cache brings the install step from minutes to seconds. Splitting caches by ecosystem prevents cross-contamination invalidation, and the warm-up job ensures the first PR of the week does not pay the full cold-start penalty.
