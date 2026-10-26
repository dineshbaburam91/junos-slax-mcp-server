# junos-slax-mcp-server

An MCP server that answers **Junos XML RPC** questions and **generates SLAX
scripts** for a described scenario, grounded in official reference sources.

## In plain words

Think of this as an **AI assistant for Junos automation**. You chat in plain
English and it:

- **Answers questions** about Junos XML RPC and SLAX scripting, using the
  official Juniper books and the SLAX manual (so answers are grounded, not made
  up).
- **Writes SLAX scripts** for what you describe (e.g. "warn on commit if an
  interface has no description").
- **Checks and runs** a script on a real Junos device for you (optional).

SLAX is Juniper's scripting language for on-box automation; XML RPC is how Junos
exposes its data/commands. You don't need to know the internals — just ask.

### What you can ask it

Just type requests like these to your AI client; it picks the right tool for you:

- "Write an op script that lists interfaces that are down."
- "Generate a commit script that blocks a commit if any interface lacks a description."
- "How do I call the get-interface-information RPC in SLAX?"
- "Validate this script on device lab1, then install it."

You normally **don't call the tools below by hand** — the assistant runs them
automatically based on your request. The list is a reference for what it can do.

## Sources

The server downloads and indexes these once into a local cache:

| id | kind | source |
|----|------|--------|
| `xml-hierarchy` | pdf | Day One: Navigating the Junos XML Hierarchy |
| `automation-reference` | pdf | This Week: Junos Automation Reference |
| `slax-manual` | html | [libslax SLAX Language Manual](https://juniper.github.io/libslax/slax-manual.html) |
| `libslax-wiki` | wiki | [libslax wiki](https://github.com/Juniper/libslax/wiki) |

## Tools

The assistant calls these for you; you rarely invoke them directly.

- `generate_slax_script(scenario, script_type?, max_references?)` — **primary tool.**
  Returns a ready-to-complete SLAX scaffold (op / commit / event / snmp) with the
  correct boilerplate, install notes, and grounding passages from the docs. The
  script type is inferred from the scenario if not given.
- `search_docs(query, doc_id?, max_results?)` — ranked passage search across sources.
- `answer_context(question, max_results?)` — supporting passages + citations for a question.
- `read_pages(doc_id, start_page, end_page?)` — raw text of specific sections.
- `list_documents()` — list sources, section counts, and any load errors.

### Device tools (via junos-mcp-server)

These call [junos-mcp-server](https://github.com/Juniper/junos-mcp-server) as an
MCP client over stdio — they do not re-implement device connectivity.

- `validate_slax_on_device(script, router, script_name?)` — writes the script to
  `/var/tmp` and runs `/usr/libexec/ui/slaxproc --check` on the device.
- `deploy_slax_script(script, router, script_type, script_name?, run?, op_args?, commit_comment?)`
  — validates, installs into the per-type scripts directory, commits the matching
  `set system scripts ... file` (or event-options) statement, and optionally runs
  an op script.
- `check_device_connectivity()` — confirms junos-mcp-server launches and lists its tools.

`router` must be a device name defined in the junos-mcp-server `devices.json`.

## Connect to a Junos device and validate / run a script

Device access is delegated to [junos-mcp-server](https://github.com/Juniper/junos-mcp-server)
(PyEZ/NETCONF under the hood). This server launches it over stdio and calls its
tools, so you only configure the device inventory in **one** place: the
junos-mcp-server `devices.json`.

### 1. Point at junos-mcp-server and its inventory

| env var | default | purpose |
|---------|---------|---------|
| `JUNOS_MCP_DIR` | _(unset)_ | run jmcp from a local dev checkout instead of the installed package |
| `JUNOS_MCP_DEVICES` | `devices.json` | inventory file (absolute, or resolved against the installed jmcp package) |
| `JUNOS_MCP_CMD` | _(unset)_ | full launch command override (shell-split) |

By default the installed `junos-mcp-server` console script is launched over
stdio, so no path needs to be hardcoded.

> **Security — keep credentials out of the chat/model.** Put passwords and SSH
> keys only in `devices.json` (or a key file on disk). Never paste a password or
> a private key into an assistant prompt. To run against a device, share only
> the **device name** (the JSON key) and confirm it is a lab/non-production box.

Create the inventory where the tools can find it and point at it with an
absolute path:

```sh
export JUNOS_MCP_DEVICES=/absolute/path/to/devices.json
```

The entries are keyed by device name. **Password auth:**

```json
{
  "lab1": {
    "ip": "10.1.1.1",
    "port": 22,
    "username": "lab",
    "auth": { "type": "password", "password": "<your-password>" }
  }
}
```

**SSH-key auth** — the key stays as a file on disk (`private_key_path`), never
in the JSON value or the chat:

```json
{
  "lab1": {
    "ip": "10.1.1.1",
    "port": 22,
    "username": "lab",
    "ssh_config": "~/.ssh/config_jumphost",
    "auth": { "type": "ssh_key", "private_key_path": "/home/lab/.ssh/lab_key.pem" }
  }
}
```

- `port` defaults to `22`; `ssh_config` is optional (use it to reach the device
  via a jump host).
- If the private key has a passphrase, load it into `ssh-agent` first so
  connections don't prompt.
- The device name you pass to the tools below (e.g. `lab1`) must match a key here.


### 2. Confirm the device backend is wired up

```python
check_device_connectivity()
# -> { "ok": true, "jmcp_tools": ["execute_junos_command", "load_and_commit_config", ...] }
```

### 3. Validate a SLAX script with slaxproc --check

`validate_slax_on_device` writes the script to `/var/tmp` on the device and runs
`/usr/libexec/ui/slaxproc --check`. `slaxproc --check` is silent on success, so
`valid` is `true` when the output has no error text.

```python
validate_slax_on_device(script="<SLAX text>", router="lab1", script_name="set-arp-q-maxlen")
```

```json
{
  "router": "lab1",
  "remote_path": "/var/tmp/set-arp-q-maxlen.slax",
  "write_output": "",
  "slaxproc_output": "",
  "valid": true
}
```

On a syntax error, `valid` is `false` and `slaxproc_output` contains the error,
e.g. `set-arp-q-maxlen.slax:24: error: unexpected token ...`.

### 4. Deploy, commit, and (for op scripts) run — see the output

`deploy_slax_script` re-validates, installs into the per-type scripts directory,
commits the matching `set system scripts ...` / `event-options` statement, and —
for an op script with `run=True` — runs it and returns the **script output**.

```python
deploy_slax_script(
    script="<SLAX text>",
    router="lab1",
    script_type="op",
    script_name="show-arp-qlen",
    run=True,
    op_args="interface ge-0/0/0",
)
```

```json
{
  "router": "lab1",
  "script_type": "op",
  "deployed": true,
  "validation": { "valid": true, "remote_path": "/var/tmp/show-arp-qlen.slax" },
  "remote_path": "/var/db/scripts/op/show-arp-qlen.slax",
  "config": "set system scripts op file show-arp-qlen.slax",
  "install_output": "",
  "commit_output": "commit complete",
  "run_command": "op show-arp-qlen interface ge-0/0/0",
  "run_output": "Interface ge-0/0/0 is up"
}
```

- `run_output` is the text the SLAX script emitted (its `<output>` / results).
- If validation fails, `deployed` is `false`, nothing is committed, and the
  failing `validation` block is returned.
- `commit` / `event` / `snmp` scripts are installed and committed but not run
  (commit scripts run on the next commit; event scripts run on their trigger).


## Getting started

### 1. Install

```sh
cd /path/to/junos-slax-mcp-server
uv sync                      # creates the venv and installs dependencies
```

### 2. Start the server (manual smoke test)

The server speaks MCP over **stdio**, so running it directly just waits for a
client to connect — that's expected. Use this only to confirm it starts without
errors (Ctrl-C to stop):

```sh
uv run python main.py
```

In normal use you don't start it by hand — your MCP client launches it for you
once it's registered (next step).

### 3. Add it to your `mcp.json`

Register the server so your AI client (VS Code, Claude Desktop, etc.) can start
it automatically. Use the **absolute path** to this folder for `cwd`.

**VS Code** — create `.vscode/mcp.json` in your workspace (VS Code uses the
`servers` key):

```json
{
  "servers": {
    "junos-slax": {
      "command": "uv",
      "args": ["run", "python", "main.py"],
      "cwd": "/path/to/junos-slax-mcp-server",
      "env": {
        "JUNOS_MCP_DEVICES": "/absolute/path/to/devices.json"
      }
    }
  }
}
```

**Claude Desktop / other clients** — add to the client's config (these use the
`mcpServers` key):

```json
{
  "mcpServers": {
    "junos-slax": {
      "command": "uv",
      "args": ["run", "python", "main.py"],
      "cwd": "/path/to/junos-slax-mcp-server",
      "env": {
        "JUNOS_MCP_DEVICES": "/absolute/path/to/devices.json"
      }
    }
  }
}
```

- `cwd` must be this project folder so `uv` finds `pyproject.toml`.
- The optional `env` block is only needed for the device tools — point
  `JUNOS_MCP_DEVICES` at your inventory (see the device section above). The
  doc/question/script-generation tools need no device config.
- After editing `mcp.json`, reload/restart your client so it picks up the server.

Once registered, just chat (see [What you can ask it](#what-you-can-ask-it)).

## Configuration

- `JUNOS_SLAX_CACHE_DIR` — override the download cache (default
  `~/.cache/junos-slax-mcp`). Sources are fetched on first use and reused after.
- `JUNOS_MCP_DIR` / `JUNOS_MCP_DEVICES` / `JUNOS_MCP_CMD` — optionally override
  how junos-mcp-server is launched for the device tools (see the device section
  above). By default the installed console script is used.

## Notes

- TLS verification uses the OS trust store (`truststore`) so sites that send a
  partial certificate chain still verify correctly.
- If a single source fails to download, the server stays up and reports the
  error via `list_documents()`.

## Testing

Integration tests live in `tests/`. Install the dev dependencies and run them
with pytest via `uv`:

```sh
uv sync            # installs pytest (dev group)
uv run pytest      # run the full suite
```

The suite has two groups, split by the `network` marker:

| group | marker | needs network? | needs a device? |
|-------|--------|----------------|-----------------|
| Device tools | _(unmarked)_ | no | no — uses a sandboxed fake junos-mcp-server |
| Reference pipeline | `network` | yes (downloads the docs) | no |

Run them selectively:

```sh
# Fast, fully offline: validate/deploy/run over a real stdio round-trip
# against tests/fake_jmcp_server.py (no router touched).
uv run pytest -m "not network"

# Downloads the Day One PDFs, SLAX manual, and libslax wiki, then exercises
# search + script generation. Auto-skips if the sources are unreachable.
uv run pytest -m network

# A single test, with verbose output:
uv run pytest tests/test_integration.py::test_deploy_op_script_installs_commits_and_runs -v
```

Notes:
- The device-tool tests set `JUNOS_MCP_CMD` to launch the fake server, so they
  never connect to a real device or to junos-mcp-server.
- PDF parsing prints harmless `fontTools` warnings; they don't affect results.
  Silence them with `uv run pytest -m network -p no:cacheprovider 2> >(grep -v fontTools)`
  or just ignore them.

