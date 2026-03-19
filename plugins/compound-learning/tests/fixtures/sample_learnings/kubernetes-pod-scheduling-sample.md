# Kubernetes Pod Scheduling with Node Affinity and Taints

**Type:** pattern
**Topic:** kubernetes-infrastructure
**Tags:** kubernetes, k8s, pod-scheduling, node-management, karpenter, spot-instances

## Problem

Workloads were landing on inappropriate node types because the cluster relied solely on Karpenter provisioner defaults. Batch jobs consumed on-demand capacity meant for latency-sensitive services, and GPU pods occasionally scheduled onto CPU-only nodes when labels drifted after a cluster upgrade.

## Solution

Define explicit node affinity rules and taints per workload class. Use `requiredDuringSchedulingIgnoredDuringExecution` for hard constraints (GPU workloads must land on GPU nodes) and `preferredDuringSchedulingIgnoredDuringExecution` for soft hints (prefer spot for batch). Pair with Karpenter NodePool `taints` so new nodes come pre-tainted and only pods with matching tolerations can schedule there.

## Why

Relying on bin-packing alone leads to noisy-neighbor problems and cost overruns. Explicit scheduling constraints make capacity intent declarative and auditable, and they prevent silent regressions when node pools change.
