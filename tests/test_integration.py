"""Integration tests for junos-slax-mcp-server.

Two groups:
  * Device-tool integration (no network, no real device) — drives the real
    server tools through the real jmcp_client stdio MCP round-trip against a
    sandboxed fake junos-mcp-server (tests/fake_jmcp_server.py).
  * Reference-pipeline integration (network) — exercises the real DocStore
    (download + extract + search) and the script generator. Skipped when the
    sources are unreachable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import jmcp_client
import server
from templates import SCRIPT_TYPES, scaffold_for

FAKE_SERVER = Path(__file__).parent / "fake_jmcp_server.py"


# --------------------------------------------------------------------------
# Device-tool integration (fake jmcp over real stdio MCP)
# --------------------------------------------------------------------------


@pytest.fixture()
def fake_device(monkeypatch, tmp_path):
    """Point jmcp_client at the sandboxed fake junos-mcp-server."""
    sandbox = tmp_path / "device"
    sandbox.mkdir()
    monkeypatch.setenv("JUNOS_MCP_CMD", f"{sys.executable} {FAKE_SERVER}")
    monkeypatch.setenv("FAKE_JMCP_SANDBOX", str(sandbox))
    return sandbox


def test_jmcp_client_round_trip_lists_tools(fake_device):
    tools = jmcp_client.list_tools()
    assert "execute_junos_command" in tools
    assert "load_and_commit_config" in tools


def test_check_device_connectivity_ok(fake_device):
    res = server.check_device_connectivity()
    assert res["ok"] is True
    assert "execute_junos_command" in res["jmcp_tools"]


def test_validate_valid_script(fake_device):
    res = server.validate_slax_on_device(scaffold_for("op"), "r1", "demo")
    assert res["router"] == "r1"
    assert res["remote_path"] == "/var/tmp/junos-slax/check/demo.slax"
    assert res["valid"] is True
    # The script was actually written into the sandbox via the base64 flow.
    assert (fake_device / "var/tmp/junos-slax/check/demo.slax").exists()


def test_validate_invalid_script(fake_device):
    res = server.validate_slax_on_device("this is not slax INVALID", "r1", "bad")
    assert res["valid"] is False
    assert "error" in res["slaxproc_output"].lower()


def test_validate_long_script_is_written_in_chunks(fake_device):
    script = scaffold_for("op") + "/* " + "x" * 3000 + " */\n"
    cmds = server._write_remote_cmds(script, "/var/tmp/long.slax")
    assert len(cmds) > 2
    assert all(len(c) < 1000 for c in cmds)
    res = server.validate_slax_on_device(script, "r1", "long")
    assert res["valid"] is True
    assert (fake_device / "var/tmp/junos-slax/check/long.slax").read_text() == script
    assert not (fake_device / "var/tmp/junos-slax/check/long.slax.b64").exists()


def test_deploy_op_script_installs_commits_and_runs(fake_device):
    rep = server.deploy_slax_script(
        scaffold_for("op"), "r1", "op", "show-x", run=True, op_args="interface ge-0/0/0"
    )
    assert rep["deployed"] is True
    assert rep["validation"]["valid"] is True
    assert rep["remote_path"] == "/var/db/scripts/op/show-x.slax"
    assert rep["config"] == "set system scripts op file show-x.slax"
    assert "commit complete" in rep["commit_output"]
    assert rep["run_command"] == "op show-x interface ge-0/0/0"
    assert "OP-OUTPUT" in rep["run_output"]
    assert (fake_device / "var/db/scripts/op/show-x.slax").exists()


def test_deploy_event_script_uses_event_options_and_does_not_run(fake_device):
    rep = server.deploy_slax_script(scaffold_for("event"), "r1", "event", "arpqlen")
    assert rep["deployed"] is True
    assert rep["remote_path"] == "/var/db/scripts/event/arpqlen.slax"
    assert rep["config"] == "set event-options event-script file arpqlen.slax"
    assert "run_output" not in rep  # event scripts run on their trigger, not inline


def test_deploy_aborts_when_validation_fails(fake_device):
    rep = server.deploy_slax_script("not slax INVALID", "r1", "op", "bad")
    assert rep["deployed"] is False
    assert rep["validation"]["valid"] is False
    # Nothing should have been installed into the scripts directory.
    assert not (fake_device / "var/db/scripts/op/bad.slax").exists()


def test_deploy_rejects_unknown_script_type(fake_device):
    rep = server.deploy_slax_script(scaffold_for("op"), "r1", "bogus", "x")
    assert "error" in rep


# --------------------------------------------------------------------------
# Reference-pipeline integration (network)
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def docstore():
    docs = server.store.all_docs()
    if all(d.error for d in docs):
        pytest.skip("reference sources unreachable (offline)")
    return server.store


@pytest.mark.network
def test_list_documents_reports_sources(docstore):
    rows = server.list_documents()
    ids = {r["doc_id"] for r in rows}
    assert {"xml-hierarchy", "automation-reference", "slax-manual", "libslax-wiki"} <= ids


@pytest.mark.network
def test_search_returns_grounded_snippets(docstore):
    results = docstore.search("SLAX for-each loop syntax", max_results=5)
    assert results
    assert all(r["title"] and "snippet" in r for r in results)


@pytest.mark.network
def test_generate_slax_script_grounds_scaffold(docstore):
    r = server.generate_slax_script("op script to show interface status")
    assert r["script_type"] in SCRIPT_TYPES
    assert "version 1.1;" in r["scaffold"]
    assert 'import "../import/junos.xsl";' in r["scaffold"]
    assert isinstance(r["references"], list)


@pytest.mark.network
def test_read_pages_round_trip(docstore):
    doc = next(d for d in docstore.all_docs() if not d.error and d.page_count)
    text = server.read_pages(doc.doc_id, 1, 1)
    assert doc.title in text
