# CLI Printing Press Setup

Complete guide to installing and using CLI Printing Press for agent-native API access.

## Why CLIs Over APIs and MCPs

| | APIs | MCPs | CLIs |
|---|---|---|---|
| **Token cost** | ~3000/call | ~2500/call | ~85/call |
| **Discovery** | Manual | Auto (50+ tools) | Lazy (on demand) |
| **Local cache** | No | No | SQLite |
| **Rate limits** | Per-API | Per-API | Local mirror |
| **Auth overhead** | Every call | Server config | One-time token |
| **Agent-native** | No | Partial | Yes |

CLIs are 35x more token-efficient than MCP servers for the same operations.

## Prerequisites

- **Go 1.26+** — Required for the factory (generating new CLIs from specs)
- **Claude Code** — Skills are designed for Claude Code integration

Note: Pre-built CLIs from the catalog don't require Go to *run* — they're standalone binaries.

## Installation

### Already Installed in This Project

```
Go 1.26.3        → /usr/local/go/bin/go
Printing Press   → ~/go/bin/printing-press (v4.5.0)
Repo             → ~/repos/cli-printing-press/
Skills (6)       → ~/.claude/skills/printing-press*
Builder Agent    → .claude/agents/printing-press-builder.md
```

### Fresh Install

```bash
# 1. Install Go
curl -LO https://go.dev/dl/go1.26.3.linux-amd64.tar.gz
sudo tar -C /usr/local -xzf go1.26.3.linux-amd64.tar.gz
echo 'export PATH=$PATH:/usr/local/go/bin:$HOME/go/bin' >> ~/.bashrc
source ~/.bashrc

# 2. Install Printing Press (pre-built binary — recommended)
curl -sL "https://github.com/mvanhorn/cli-printing-press/releases/latest/download/cli-printing-press_linux_amd64.tar.gz" \
  -o /tmp/pp.tar.gz
mkdir -p ~/go/bin /tmp/pp-extract && tar -xzf /tmp/pp.tar.gz -C /tmp/pp-extract
cp /tmp/pp-extract/printing-press ~/go/bin/ && chmod +x ~/go/bin/printing-press

# Alternative: compile from source (needs ~2GB disk for Go build cache)
# go install github.com/mvanhorn/cli-printing-press/v4/cmd/printing-press@latest

# 3. Clone repo for skills
git clone https://github.com/mvanhorn/cli-printing-press.git ~/repos/cli-printing-press

# 4. Link skills
for skill in printing-press printing-press-catalog printing-press-import \
             printing-press-polish printing-press-reprint printing-press-score; do
  ln -sf ~/repos/cli-printing-press/skills/$skill ~/.claude/skills/$skill
done

# 5. Verify
printing-press --version
printing-press catalog list
```

## Usage

### Browse the Catalog

```bash
# In Claude Code
/printing-press-catalog

# Or via CLI
printing-press catalog list
```

### Generate a CLI

```bash
# From the embedded catalog
/printing-press stripe

# From an OpenAPI spec file
/printing-press --spec ./api/github.yml

# From a website (reverse-engineers the API)
/printing-press https://example.com

# From captured traffic
/printing-press --har ./capture.har --name MyAPI
```

### Install a Pre-Built CLI

```bash
printing-press library install stripe
printing-press library install github sentry hubspot
```

### Use a Generated CLI

```bash
# Direct CLI usage
stripe-pp-cli customers list --limit 5 --compact
github-pp-cli repos list --user mvanhorn --json

# Via Claude Code skill (natural language)
"What are my recent Stripe charges?"
"List open GitHub issues in my repo"
```

## NeurogenServer Integration

### Architecture

```
Magnus Pipeline Request
    → register_integration_tools()
        → Try CLI (PrintingPressToolkit)  ← 85 tokens
        → Fallback: OpenAPI toolkit       ← 3000 tokens
```

### Files

| File | Purpose |
|------|---------|
| `modules/services/printing_press_bridge.py` | Auth bridge — decrypts Fernet keys, passes to CLI via env vars |
| `modules/services/toolkits/printing_press_toolkit.py` | AG2 toolkit wrapping CLI binaries |
| `modules/services/toolkits/__init__.py` | CLI-first registration in `register_integration_tools()` |

### Auth Bridge

NeurogenServer stores API keys encrypted with Fernet in PostgreSQL. The bridge:

1. Calls `resolve_integration_auth()` to decrypt the user's key
2. Sets `<CLI_NAME>_API_KEY` environment variable
3. Runs the CLI binary as a subprocess
4. Key exists only in process memory, never on disk

### Supported Integrations

The bridge maps 16 NeurogenServer integrations to CLI binaries:

| Integration Key | CLI Binary | Env Var |
|----------------|------------|---------|
| `stripe_api` | `stripe-pp-cli` | `STRIPE_API_KEY` |
| `github_api` | `github-pp-cli` | `GITHUB_TOKEN` |
| `linear_api` | `linear-pp-cli` | `LINEAR_API_KEY` |
| `slack_api` | `slack-pp-cli` | `SLACK_TOKEN` |
| `notion_api` | `notion-pp-cli` | `NOTION_API_KEY` |
| `hubspot_api` | `hubspot-pp-cli` | `HUBSPOT_API_KEY` |
| `jira_api` | `jira-pp-cli` | `JIRA_API_TOKEN` |
| `sendgrid_api` | `sendgrid-pp-cli` | `SENDGRID_API_KEY` |
| `shopify_api` | `shopify-pp-cli` | `SHOPIFY_ACCESS_TOKEN` |
| `mailchimp_api` | `mailchimp-pp-cli` | `MAILCHIMP_API_KEY` |
| `zendesk_api` | `zendesk-pp-cli` | `ZENDESK_API_TOKEN` |
| `intercom_api` | `intercom-pp-cli` | `INTERCOM_ACCESS_TOKEN` |
| `freshdesk_api` | `freshdesk-pp-cli` | `FRESHDESK_API_KEY` |
| `trello_api` | `trello-pp-cli` | `TRELLO_API_KEY` |
| `asana_api` | `asana-pp-cli` | `ASANA_ACCESS_TOKEN` |
| `clickup_api` | `clickup-pp-cli` | `CLICKUP_API_KEY` |

### Enable/Disable

```bash
# Disable CLI-first (fall back to OpenAPI only)
export PRINTING_PRESS_ENABLED=false

# Re-enable (default)
export PRINTING_PRESS_ENABLED=true
```

## Batch Generation

Use the `printing-press-builder` agent to generate CLIs for multiple NeurogenServer APIs:

```
Use the printing-press-builder agent to generate CLIs for stripe, github, and hubspot
from the OpenAPI specs in /opt/neurogenserver/api/
```

## Quality Scoring

Every generated CLI is scored on a 100-point two-tier scorecard:

- **Tier 1 (50 pts)**: Infrastructure — output modes, auth, error handling, agent-native flags
- **Tier 2 (50 pts)**: Domain correctness — path validity, auth matching, data pipeline, type fidelity

Grade A requires 85+ points. Use `/printing-press-score` to check.
