# Prometheus Alerting Rules for SLO-Based Monitoring

**Type:** pattern
**Topic:** prometheus
**Tags:** prometheus, promql, alerting, observability, metrics, slo, kube-prometheus-stack

## Problem

The team had dozens of static threshold alerts (CPU > 80%, memory > 90%) that generated noise but missed actual user-facing outages. A 15-minute elevated error rate went undetected because no alert tracked the ratio of failed to total requests over a meaningful window.

## Solution

Replace static resource alerts with SLO-derived burn-rate alerts. Define an error budget (e.g., 99.9% success over 30 days), then create multi-window burn-rate rules: a fast window (5m) catches sudden spikes and a slow window (1h) catches sustained degradation. Use `rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m])` as the base signal and alert when the burn rate exceeds 14x (page) or 6x (ticket).

## Why

Burn-rate alerting ties alerting directly to user impact rather than infrastructure symptoms. It naturally suppresses transient blips while catching sustained issues early enough to act before the error budget is exhausted for the period.
