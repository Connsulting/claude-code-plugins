# Original viewer follow-up project

The plugin intentionally ships the original `jobs-viewer` server, embedded HTML, CSS, and
JavaScript so the first live cutover preserves the UI and its controls exactly. The only
runtime edits in that copied server select the installed skill root and the copied viewer
collectors. The following work is deliberately deferred instead of being mixed into the
port:

- derive provider lanes, icons, buttons, account cards, and drain-order labels from the JSON
  provider/plan/account graph;
- hide provider controls when no matching dispatch adapter and account are configured;
- replace the original Claude/Codex/Grok collector scripts with normalized cache reads from
  configured usage adapters while retaining background-only collection;
- replace the legacy shell gate-display compatibility path with `bonus-drain gates --json`
  and `bonus-drain plan --json`;
- make the original force controls consume the viewer security policy explicitly, including
  a config-driven mutation switch, exact Host/Origin checks, and authenticated sessions if
  exposure ever expands beyond a loopback listener behind Tailscale Serve;
- retain pixel-for-pixel HTML/CSS regression fixtures while testing arbitrary provider sets,
  absent providers, multiple accounts, and no provider calls during page requests.

Until that project is completed, the plugin core scheduler remains generic and JSON-driven,
but this compatibility viewer intentionally retains the original named provider presentation.
The listener template remains fixed to `127.0.0.1`; do not bind it to `0.0.0.0`.
