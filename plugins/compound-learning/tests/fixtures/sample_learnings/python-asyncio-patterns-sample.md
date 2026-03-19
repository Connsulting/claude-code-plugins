# Python Asyncio Task Group Error Handling

**Type:** pattern
**Topic:** python
**Tags:** python, asyncio, concurrency, threading, context-manager

## Problem

A data pipeline used `asyncio.gather()` with `return_exceptions=True` to run multiple API fetches concurrently. When one fetch raised an unexpected `ConnectionResetError`, the exception was silently captured in the results list. Downstream code assumed all results were valid dataframes and crashed with an opaque `AttributeError` far from the actual failure point.

## Solution

Replace `asyncio.gather(return_exceptions=True)` with a `TaskGroup` (Python 3.11+). `TaskGroup` propagates the first exception immediately and cancels remaining tasks, giving a clear traceback at the failure point. For cases where partial results are acceptable, wrap individual tasks in try/except within the task group and collect results into a shared list, logging failures explicitly.

## Why

`return_exceptions=True` trades immediate failure visibility for convenience, but it pushes error handling to the caller who must inspect each result. `TaskGroup` enforces structured concurrency where the lifetime and error semantics of child tasks are scoped to a block, making failures impossible to silently ignore.
