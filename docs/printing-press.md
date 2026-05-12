# CLI Printing Press Integration

Printing Press generates agent-native CLIs from APIs. CLIs use 35x fewer tokens than MCP servers for the same operations.

## Installation

Pre-installed in this project:
- **Go 1.26.3** at `/usr/local/go/bin/go`
- **Printing Press v4.5.0** at `~/go/bin/printing-press`
- **Repo** cloned at `~/repos/cli-printing-press/`

## Skills

6 Printing Press skills are available:

| Skill | Purpose |
|-------|---------|
| `/printing-press <api>` | Generate a new CLI from an API name, URL, or spec |
| `/printing-press-catalog` | Browse the embedded catalog of available CLIs |
| `/printing-press-import` | Import an existing CLI into the library |
| `/printing-press-polish` | Second-pass improvements on an existing CLI |
| `/printing-press-reprint` | Regenerate a CLI under the latest factory |
| `/printing-press-score` | Score a CLI against the quality scorecard |

## Catalog

22 pre-built APIs available via `printing-press catalog list`:

**Payments**: Stripe, Mercury, Plaid
**Developer**: GitHub, LaunchDarkly, Sentry, Postman
**CRM/Sales**: HubSpot, Pipedrive
**Communication**: Discord, Front, Telegram, Twilio
**Cloud**: DigitalOcean, Google Cloud Run
**Other**: Asana, ProductHunt, Google Flights, Stytch

## Generating a CLI

```bash
# From catalog
/printing-press stripe

# From an OpenAPI spec
/printing-press --spec /opt/neurogenserver/api/github.yml

# From a website (reverse-engineers the API)
/printing-press https://example.com
```

## NeurogenServer Integration

The Printing Press toolkit integrates with NeurogenServer's AG2 pipeline:

- **Auth Bridge** (`printing_press_bridge.py`): Decrypts Fernet-encrypted API keys at runtime, passes via env vars to CLI subprocess. Keys never touch disk.
- **AG2 Toolkit** (`printing_press_toolkit.py`): Wraps CLI binaries as AG2 tool functions.
- **CLI-first fallback**: `register_integration_tools()` tries CLI first, falls back to OpenAPI toolkit automatically.
- **Control**: Set `PRINTING_PRESS_ENABLED=false` to disable CLI-first behavior.

## Builder Agent

The `printing-press-builder` agent (`.claude/agents/printing-press-builder.md`) generates CLIs in batch from NeurogenServer's 72 OpenAPI specs.
