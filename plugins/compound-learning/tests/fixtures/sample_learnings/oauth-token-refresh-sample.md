# OAuth Token Refresh Race Condition with Cognito

**Type:** gotcha
**Topic:** authentication
**Tags:** authentication, oauth, cognito, jwt, tokens, sso

## Problem

Users were intermittently logged out during long sessions. The SPA fired multiple API requests simultaneously, each detecting an expired access token and triggering a token refresh call to Cognito. Cognito invalidated the refresh token after first use, so subsequent concurrent refresh attempts returned "invalid_grant" and forced a full re-login.

## Solution

Implement a token refresh mutex in the HTTP interceptor. The first request that detects an expired token acquires a lock and performs the refresh. All other concurrent requests await the same promise rather than issuing their own refresh calls. On success the new tokens propagate to all waiting requests; on failure they all redirect to login once.

## Why

OAuth refresh tokens are typically single-use by spec. Any client that can fire concurrent requests needs a synchronization mechanism around token refresh, otherwise the race condition surfaces under real-world network timing even if it never appears in serial testing.
