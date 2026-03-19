# Grafana Dashboard Design for Service Health

**Type:** pattern
**Topic:** observability
**Tags:** grafana, dashboards, monitoring, observability, metrics, grafana-explorer

## Problem

The existing Grafana dashboards had grown organically to 40+ panels per page, mixing infrastructure metrics (CPU, memory) with business metrics (request count, conversion rate). On-call engineers could not quickly assess service health because the relevant panels were buried among irrelevant ones, and page load time exceeded 10 seconds due to the number of queries.

## Solution

Adopt a three-tier dashboard hierarchy: (1) a top-level service overview with four golden signals (latency, traffic, errors, saturation) as stat panels, (2) a drill-down dashboard per service with time series for each signal, and (3) a debug dashboard with infrastructure metrics linked via data links from the drill-down. Use template variables for service and environment so a single dashboard definition serves all services. Set default time range to 6 hours and refresh to 30 seconds.

## Why

Dashboards should answer a specific question at each level. The overview answers "is anything broken?", the drill-down answers "what is broken?", and the debug dashboard answers "why is it broken?". This layered approach keeps each dashboard fast to load and focused enough that on-call engineers can triage without scrolling.
