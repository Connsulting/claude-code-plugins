# Big Plan

Big Plan serves repository Markdown plans in the same commentable, phone-friendly UI used by the original service. The renderer, server, feedback dispatcher, JavaScript, and CSS are copied unchanged.

## Install

Installing the Claude or Codex plugin only makes the skill available. It does not start services or change Tailscale.

To stage the stable runtime and install its user unit without starting it:

```sh
./install.sh
```

To install and start it explicitly:

```sh
./install.sh --enable
```

The service serves `$HOME/git` on port `8765` and shows promoted plans under repository `.projects/` directories. When a bounded Tailscale health probe reports `BackendState: Running`, a nonempty MagicDNS name, and an IPv4 address, it binds to `0.0.0.0` so localhost callbacks, direct tailnet access, and an existing Tailscale Serve proxy all work. Otherwise it binds to `127.0.0.1`. Binding `0.0.0.0` listens on every interface and is suitable only on a trusted or firewalled host; the launcher selects it only after that healthy Tailscale probe. Override these defaults with `BIG_PLAN_ROOT`, `BIG_PLAN_PORT`, `BIG_PLAN_HOST`, or `BIG_PLAN_FILTER` in the user-unit environment.

The direct remote URL is `http://<MagicDNS-name>:8765/<path>`. The installer does not configure `tailscale serve`; HTTPS is available only when an operator separately configures Tailscale Serve to proxy to port `8765`.

`./uninstall.sh` stops the user unit and removes only the staged runtime, launcher, and unit. Plans, comment/session/snapshot sidecars, configuration, and state are preserved.
