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

import asyncio
import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

import docs
import jmcp_client
import server
from templates import SCRIPT_TYPES, deploy_target, infer_script_type, scaffold_for

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


def test_junoscriptorium_indexes_only_slax_files(monkeypatch):
    archive_data = io.BytesIO()
    with tarfile.open(fileobj=archive_data, mode="w:gz") as archive:
        for path, content in (
            ("repo/library/event/interface.slax", b"version 1.0;\nmatch / {}"),
            ("repo/README.md", b"not a script"),
        ):
            member = tarfile.TarInfo(path)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        link = tarfile.TarInfo("repo/library/event/link.slax")
        link.type = tarfile.SYMTYPE
        link.linkname = "interface.slax"
        archive.addfile(link)

    monkeypatch.setattr(docs.DocStore, "_fetch", lambda self, url: archive_data.getvalue())
    store = docs.DocStore()
    doc = store.get("junoscriptorium")
    assert doc.error is None
    assert [page.label for page in doc.pages] == ["library/event/interface.slax"]
    assert store.search("interface", doc_id="junoscriptorium")[0]["section"] == doc.pages[0].label


def test_local_docs_folder_pdfs_are_discovered(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "translation-scripts.pdf").write_bytes(b"pdf placeholder")
    (nested / "notes.md").write_text("not a PDF")

    catalog = docs._discover_local_pdfs(tmp_path)

    assert list(catalog) == ["local-nested-translation-scripts"]
    assert catalog["local-nested-translation-scripts"]["kind"] == "local-pdf"
    assert catalog["local-nested-translation-scripts"]["path"].endswith(
        "nested/translation-scripts.pdf"
    )


def test_checked_in_docs_pdf_is_indexed_and_searchable():
    store = docs.DocStore()
    document = store.get("local-translation-scripts")

    assert document.kind == "local-pdf"
    assert document.page_count > 0
    assert document.error is None
    assert store.search("foreign schema translation", doc_id=document.doc_id)


def test_local_samples_are_searchable_and_used_by_generator(monkeypatch, tmp_path):
    samples = tmp_path / "samples"
    (samples / "commit").mkdir(parents=True)
    sample = samples / "commit" / "license-check.slax"
    sample.write_text(
        'version 1.0;\nmatch configuration { <output> "license_alarm_marker"; }\n',
        encoding="utf-8",
    )
    (samples / "commit" / "notes.txt").write_text("not a SLAX sample")
    (samples / "event").mkdir()
    (samples / "event" / "license-check.slax").write_text(
        'version 1.0;\nmatch / { <output> "event"; }\n', encoding="utf-8"
    )

    catalog = docs._discover_local_samples(samples)
    monkeypatch.setattr(docs, "DOCUMENTS", catalog)
    monkeypatch.setattr(server, "DOCUMENTS", catalog)
    monkeypatch.setattr(server, "store", docs.DocStore())
    monkeypatch.setattr(server, "GENERATED_DIR", tmp_path)

    assert set(catalog) == {
        "sample-commit-license-check",
        "sample-event-license-check",
    }
    rows = server.list_documents()
    commit = next(row for row in rows if row["doc_id"] == "sample-commit-license-check")
    assert commit["error"] is None
    assert commit["sections"] == 1
    result = server.search_docs("license_alarm_marker", doc_id=commit["doc_id"])
    assert result[0]["section"] == "commit/license-check.slax"
    assert "license_alarm_marker" in server.read_pages(commit["doc_id"], 1)
    generated = server.generate_slax_script("license_alarm_marker", "commit")
    assert any(r["doc_id"] == commit["doc_id"] for r in generated["references"])


def test_generated_scaffolds_are_saved_without_overwriting(monkeypatch, tmp_path):
    class EmptyStore:
        def search(self, *args, **kwargs):
            return []

    monkeypatch.setattr(server, "store", EmptyStore())
    generated_dir = tmp_path / "Documents" / "junos-slax-generated"
    monkeypatch.setattr(server, "GENERATED_DIR", generated_dir)

    first = server.generate_slax_script("show interfaces", "op")
    second = server.generate_slax_script("show interfaces", "op")
    first_path = Path(first["scaffold_path"])
    second_path = Path(second["scaffold_path"])

    assert first_path.parent == generated_dir
    assert first_path.suffix == ".slax"
    assert first_path != second_path
    assert first_path.read_text(encoding="utf-8") == first["scaffold"]
    assert second_path.read_text(encoding="utf-8") == second["scaffold"]
    assert "save_slax_script" in first["guidance"]


def test_save_completed_script_and_reject_invalid_input(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "GENERATED_DIR", tmp_path)
    script = 'version 1.1;\nmatch / { <output> "up"; }\n'
    saved = server.save_slax_script(script, "interface-status.slax")
    path = Path(saved["path"])

    assert path.parent == tmp_path
    assert path.name.startswith("interface-status-")
    assert path.read_text(encoding="utf-8") == script
    for name in ("../escape", "/tmp/script", "invalid name"):
        with pytest.raises(ValueError):
            server.save_slax_script(script, name)
    with pytest.raises(ValueError):
        server.save_slax_script("  ")


def test_save_script_reports_storage_errors(monkeypatch, tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("existing file")
    monkeypatch.setattr(server, "GENERATED_DIR", blocked / "generated")
    with pytest.raises(NotADirectoryError):
        server.save_slax_script("version 1.1;")


def test_default_save_directory_is_shared_across_server_processes(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    code = (
        'import server; '
        'print(server.save_slax_script("version 1.1;", "example")["path"])'
    )
    paths = [
        Path(subprocess.run(
            [sys.executable, "-c", code], check=True, capture_output=True, text=True
        ).stdout.strip())
        for _ in range(2)
    ]

    assert paths[0].parent == tmp_path / "Documents" / "junos-slax-generated"
    assert paths[1].parent == paths[0].parent
    assert paths[0] != paths[1]
    assert all(path.read_text(encoding="utf-8") == "version 1.1;" for path in paths)


def test_save_script_is_available_through_mcp(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "GENERATED_DIR", tmp_path)
    _, result = asyncio.run(
        server.mcp.call_tool(
            "save_slax_script",
            {"script": "version 1.1;\n", "script_name": "example"},
        )
    )
    assert Path(result["path"]).read_text(encoding="utf-8") == "version 1.1;\n"


def test_local_sample_load_errors_are_reported(monkeypatch, tmp_path):
    sample = tmp_path / "invalid.slax"
    sample.write_bytes(b"\xff")
    catalog = docs._discover_local_samples(tmp_path)
    monkeypatch.setattr(docs, "DOCUMENTS", catalog)

    doc = docs.DocStore().get("sample-invalid")
    assert doc.error and "UnicodeDecodeError" in doc.error


def test_translation_script_type_has_scaffold_and_deploy_target():
    assert "translation" in SCRIPT_TYPES
    assert infer_script_type("translate OpenConfig BGP configuration") == "translation"
    assert infer_script_type("translation script for configuration") == "translation"
    assert "match configuration" in scaffold_for("translation")
    assert deploy_target("translation", "bgp.slax") == (
        "/var/db/scripts/translation",
        "set system scripts translation-scripts file bgp.slax",
    )


def test_translation_script_deploys(fake_device):
    rep = server.deploy_slax_script(
        scaffold_for("translation"), "r1", "translation", "bgp"
    )
    assert rep["deployed"] is True
    assert rep["remote_path"] == "/var/db/scripts/translation/bgp.slax"
    assert rep["config"] == "set system scripts translation-scripts file bgp.slax"
    assert (fake_device / "var/db/scripts/translation/bgp.slax").exists()


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
