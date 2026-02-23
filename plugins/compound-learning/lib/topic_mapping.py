"""
Topic inference from learning file tags.

Maps topic names to sets of indicator tags, and provides a function to infer
the best matching topic from a list of tags.
"""

from __future__ import annotations

TOPIC_TAG_MAP: dict[str, set[str]] = {
    "kubernetes-infrastructure": {
        "kubernetes", "k8s", "helm", "karpenter", "eks", "kubectl",
        "kube-state-metrics", "pod-scheduling", "node-management",
        "self-hosted-runners", "arc", "infrastructure", "cluster",
        "pvc", "hpa", "ingress", "namespace", "rbac", "cfs",
        "cpu-throttling", "qos", "spot-instances", "nodepool",
    },
    "aws": {
        "aws", "ec2", "s3", "ecr", "ebs", "elb", "nlb", "alb", "acm",
        "cloudformation", "cdk", "iam", "eks", "vpc", "nat-gateway",
        "vpc-endpoints", "cur", "cost-explorer", "aws-billing",
        "aws-secrets-manager", "crr", "aws-cli", "ebs-volume",
        "disaster-recovery", "rollback", "aws-ecr",
    },
    "ai-agents": {
        "claude-code", "claude", "anthropic", "agents", "hooks", "llm",
        "agent-teams", "prompt-caching", "claude-cli", "multi-agent",
        "agent-orchestration", "litellm", "claude-code-plugins",
        "recursion", "subprocess", "background-jobs", "plan-mode",
        "token-efficiency", "context-window", "transcript",
        "silent-failure", "claude-code-config", "agent-design",
        "hook-transcript", "hook-recursion", "hook-permissions",
    },
    "cicd": {
        "cicd", "ci-cd", "ci", "github-actions", "bitbucket",
        "pipeline", "workflow", "devops", "migration", "bitbucket-ci",
        "git-ci", "poetry-ci", "arc", "self-hosted-runners",
        "github-actions-db",
    },
    "security": {
        "security", "cve", "vulnerability", "vulnerabilities",
        "security-scanning", "npm-audit", "pnpm", "poetry-constraints",
        "openssl", "base-images", "transitive-dependencies",
        "npm-overrides", "false-positives", "api-keys", "secrets",
        "dmarc", "email-security", "domain-authentication",
        "docker-security", "debian-security", "security-audit",
        "aikido", "debian", "babel-traverse",
    },
    "observability": {
        "observability", "monitoring", "grafana", "otel", "tracing",
        "metrics", "latency", "slo", "span-filtering", "logs",
        "prometheus", "thresholds", "dashboards", "alerting",
        "grafana-explorer",
    },
    "prometheus": {
        "prometheus", "promql", "tsdb", "admin-api", "kube-prometheus-stack",
        "max_over_time", "time-series", "compaction", "deletion",
        "kube-state-metrics",
    },
    "debugging": {
        "debugging", "troubleshooting", "incident-response",
        "root-cause-analysis", "comparative-analysis", "investigation",
        "methodology", "ipc", "file-based-communication",
        "cli", "bash", "stdin", "shell-scripting",
    },
    "dependency-management": {
        "dependency-management", "dependencies", "package-analysis",
        "lock-files", "lock-file", "npm-overrides", "pnpm-overrides",
        "poetry", "package-lock", "npm", "pip", "pypi",
    },
    "python": {
        "python", "pathlib", "context-manager", "asyncio",
        "with-statement", "resource-management", "symlinks",
        "file-discovery", "sqlite", "threading", "concurrency",
        "pandas", "csv-parsing",
    },
    "nodejs": {
        "nodejs", "esm", "modules", "path-resolution", "import-meta",
        "async-await", "node", "npm-package",
    },
    "database": {
        "database", "postgresql", "cdc", "replication", "wal2json",
        "debezium", "vector-database", "embeddings", "sqlite-vec",
        "vector-search",
    },
    "deployment": {
        "deployment", "release", "rollout", "rollback", "canary",
        "blue-green", "staging", "production",
    },
    "git-workflow": {
        "git", "rebase", "conflicts", "branch-management",
        "workflow", "merge", "cherry-pick", "gitops", "pre-commit",
        "git-hooks", "git-dependencies", "regenerated", "lock-files",
    },
    "authentication": {
        "authentication", "oauth", "sso", "cognito", "argocd",
        "grafana-oauth", "jwt", "tokens", "rbac", "oidc",
    },
    "docker": {
        "docker", "dockerfile", "base-image", "image-tags",
        "docker-registry", "container", "docker-compose",
        "ubuntu-dockerfile", "layer-deduplication",
    },
    "api-integration": {
        "api-testing", "feasibility", "curl", "api-migration",
        "api-provider-swap", "thresholds", "data-providers",
        "protocol", "rest", "graphql", "openapi",
    },
    "automation": {
        "automation", "pre-commit", "git-hooks", "safety",
        "script-design", "reusability", "cli-args", "best-practices",
        "workflow-automation",
    },
    "documentation": {
        "documentation", "templates", "single-source-of-truth",
        "architecture", "dry-principle", "architecture-patterns",
        "docs",
    },
    "performance": {
        "performance", "parallelization", "async", "spawn",
        "child_process", "optimization", "cost-optimization",
        "memory-optimization", "capacity-planning",
    },
    "testing": {
        "testing", "e2e", "playwright", "fixtures", "test-audit",
        "integration-tests", "unit-tests", "tdd", "quality-assessment",
        "pytest", "verification",
    },
    "project-management": {
        "jira", "estimation", "corrections", "audit-trail",
        "project-management", "planning", "roadmap",
    },
}


def infer_topic_from_tags(tags: list[str]) -> str:
    """Infer the best matching topic from a list of tags.

    Scores each topic by counting how many of the provided tags appear in
    that topic's indicator set. Returns the topic with the highest score,
    or 'other' if no tags match any topic.
    """
    if not tags:
        return "other"

    normalised = [t.strip().lower() for t in tags]
    scores: dict[str, int] = {}

    for topic, indicators in TOPIC_TAG_MAP.items():
        score = sum(1 for tag in normalised if tag in indicators)
        if score > 0:
            scores[topic] = score

    if not scores:
        return "other"

    return max(scores, key=lambda t: scores[t])
