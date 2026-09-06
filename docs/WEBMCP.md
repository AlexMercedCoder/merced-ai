# WebMCP routing

Merced AI brokers WebMCP through harnesses that implement the browser boundary natively. Current
native routes are MagAgent and Loro; Merced AI itself never reads browser storage or proxies site
credentials.

Harness inventory reports the `webmcp` capability in the CLI and Web UI. A bot binding may require
it:

```bash
merced-ai bot create researcher --profile webmcp-researcher --harness magagent \
  --fallback loro --requires-webmcp
```

When required, routing skips installed fallbacks that cannot provide native WebMCP and reports the
capability mismatch. When optional, another harness may receive a degraded OAP projection as usual.

Configure the selected harness before running the bot:

```bash
magent webmcp status
# or
loro setup webmcp
```

Discovery, exact-origin policy, browser isolation, approvals, revision binding, and audit records
remain the responsibility of the selected harness. Merced AI's UI continues to relay AAIS runtime
approval requests for MagAgent and Loro, including WebMCP mutations.
