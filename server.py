"""MCP server for Junos XML RPC questions and SLAX script generation.

Tools let an MCP client search and read the reference sources (the Juniper
Day One XML/automation books, the libslax SLAX manual, and the libslax wiki)
and, most importantly, generate a SLAX script scaffold for a described
scenario grounded in those sources.
"""

from __future__ import annotations

import base64

from mcp.server.fastmcp import FastMCP

import jmcp_client
from docs import DOCUMENTS, DocStore
from templates import (
    INSTALL_NOTES,
    SCRIPT_TYPES,
    deploy_target,
    infer_script_type,
    scaffold_for,
)

mcp = FastMCP("junos-slax")
store = DocStore()

# Device-side SLAX validator shipped with Junos/EVO.
SLAXPROC = "/usr/libexec/ui/slaxproc"

# "start shell command" breaks on lines over ~1000 chars, so send base64 in chunks.
B64_CHUNK = 512

# Scripts import "../import/junos.xsl", so validate one level below a dir that has import/.
VALIDATE_ROOT = "/var/tmp/junos-slax"
VALIDATE_DIR = f"{VALIDATE_ROOT}/check"
VALIDATE_SETUP = (
    f'start shell command "mkdir -p {VALIDATE_DIR}; '
    f'ln -sfn /var/db/scripts/import {VALIDATE_ROOT}/import"'
)


@mcp.tool()
def list_documents() -> list[dict[str, object]]:
    """List the reference sources this server can answer questions from.

    Returns each source's id, title, kind (pdf/html/wiki), topic, source URL,
    and the number of extracted sections. Use the returned doc_id to scope
    other tools. If a source failed to download, its "error" field is set.
    """
    results: list[dict[str, object]] = []
    for doc_id, meta in DOCUMENTS.items():
        doc = store.get(doc_id)
        results.append(
            {
                "doc_id": doc_id,
                "title": meta["title"],
                "kind": meta["kind"],
                "topic": meta["topic"],
                "url": meta["url"],
                "sections": doc.page_count,
                "error": doc.error,
            }
        )
    return results


@mcp.tool()
def search_docs(
    query: str, doc_id: str | None = None, max_results: int = 6
) -> list[dict[str, object]]:
    """Search the reference sources for passages relevant to a question.

    Args:
        query: The question or keywords, e.g. "get-interface-information RPC"
            or "SLAX for-each loop syntax".
        doc_id: Optional source id to restrict the search ("xml-hierarchy",
            "automation-reference", "slax-manual", "libslax-wiki", "slax-1-1",
            "junoscriptorium", or "translation-scripts"). Omit to
            search all sources.
        max_results: Maximum number of matching passages to return.

    Returns a ranked list of snippets with their document, section label, and
    page number so you can cite them or read more with read_pages.
    """
    return store.search(query, doc_id=doc_id, max_results=max_results)


@mcp.tool()
def read_pages(doc_id: str, start_page: int, end_page: int | None = None) -> str:
    """Read the raw extracted text of specific sections/pages of a source.

    Args:
        doc_id: The source id (see list_documents).
        start_page: 1-based first section/page to read.
        end_page: 1-based last section/page (inclusive). Defaults to start_page.

    Returns the concatenated section text with labelled markers.
    """
    doc = store.get(doc_id)
    if doc.error:
        return f"Source '{doc_id}' failed to load: {doc.error}"
    if end_page is None:
        end_page = start_page
    start = max(1, start_page)
    end = min(doc.page_count, end_page)
    if start > end:
        return (
            f"No sections in range {start_page}-{end_page} "
            f"(document has {doc.page_count} sections)."
        )
    chunks: list[str] = []
    for page in doc.pages[start - 1 : end]:
        marker = f"--- {doc.title} | {page.label or ('section ' + str(page.number))} ---"
        chunks.append(f"{marker}\n{page.text.strip()}")
    return "\n\n".join(chunks)


@mcp.tool()
def answer_context(question: str, max_results: int = 8) -> dict[str, object]:
    """Gather supporting passages to answer an XML RPC or SLAX question.

    Searches all sources and returns the most relevant passages with their
    citations. Use the returned context to compose an answer and cite the
    document title + section.

    Args:
        question: A natural-language question about Junos XML RPC or SLAX.
        max_results: Maximum number of supporting passages to include.
    """
    passages = store.search(question, doc_id=None, max_results=max_results)
    return {
        "question": question,
        "passages": passages,
        "guidance": (
            "Base the answer only on the passages above. Cite the document "
            "title and section. If the passages do not cover the question, "
            "say so instead of guessing."
        ),
    }


@mcp.tool()
def generate_slax_script(
    scenario: str, script_type: str | None = None, max_references: int = 6
) -> dict[str, object]:
    """Generate a SLAX script scaffold for a described scenario.

    This is the primary tool: given a plain-language scenario (e.g. "warn on
    commit if any interface has no description" or "op script to show BGP
    neighbors in a given VRF"), it returns a ready-to-complete SLAX scaffold
    with the correct boilerplate for the inferred (or requested) script type,
    plus grounding passages from the SLAX manual and automation reference.

    Args:
        scenario: What the SLAX script should do.
        script_type: Optional explicit type: "op", "commit", "event", or
            "snmp". If omitted, the type is inferred from the scenario.
        max_references: How many supporting reference passages to include.

    Returns the chosen script_type, a SLAX scaffold, install instructions,
    grounding passages (with citations), and guidance for completing the body.
    """
    chosen = (script_type or "").strip().lower()
    if chosen not in SCRIPT_TYPES:
        chosen = infer_script_type(scenario)

    scaffold = scaffold_for(chosen)

    # Pull scenario-relevant syntax from the SLAX manual and automation book.
    passages = store.search(
        f"{chosen} script {scenario}", doc_id=None, max_results=max_references
    )

    return {
        "scenario": scenario,
        "script_type": chosen,
        "scaffold": scaffold,
        "install": INSTALL_NOTES.get(chosen, ""),
        "references": passages,
        "guidance": (
            "Produce a complete SLAX script by adapting the scaffold to the "
            "scenario. Keep the version line, namespace declarations, and the "
            "junos.xsl import. Build RPCs as XML elements and invoke them with "
            "jcs:invoke(). Use the reference passages for exact element and "
            "statement syntax, and cite them if you explain the script. Only "
            "use SLAX/Junos constructs supported by the references."
        ),
    }


def _slax_filename(script_name: str) -> str:
    name = script_name.strip() or "genscript"
    return name if name.endswith(".slax") else f"{name}.slax"


def _write_remote_cmds(script: str, remote_path: str) -> list[str]:
    """Build 'start shell command's that write the script via chunked base64."""
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    tmp = f"{remote_path}.b64"
    chunks = [b64[i : i + B64_CHUNK] for i in range(0, len(b64), B64_CHUNK)] or [""]
    cmds: list[str] = []
    for i, chunk in enumerate(chunks):
        redirect = ">" if i == 0 else ">>"
        # FreeBSD base64 -d drops input lacking a trailing newline, hence '%s\n'.
        cmds.append(f"start shell command \"printf '%s\\n' '{chunk}' {redirect} {tmp}\"")
    cmds.append(f'start shell command "base64 -d < {tmp} > {remote_path}; rm -f {tmp}"')
    return cmds


def _slax_is_valid(output: str) -> bool:
    """slaxproc --check prints no error text on success (or "script check succeeds")."""
    low = output.lower()
    return not any(w in low for w in ("error", "invalid", "fail", "no such file"))


@mcp.tool()
def validate_slax_on_device(
    script: str, router: str, script_name: str = "genscript"
) -> dict[str, object]:
    """Validate a SLAX script on a Junos device using slaxproc --check.

    Writes the script under /var/tmp/junos-slax/check on the device (via
    junos-mcp-server's execute_junos_command tool) and runs
    "/usr/libexec/ui/slaxproc --check <file>" to check syntax and content.

    Args:
        script: The full SLAX script text to validate.
        router: Device name as defined in the junos-mcp-server devices.json.
        script_name: Base name for the temporary file (".slax" appended).

    Returns the remote path, raw slaxproc output, and a "valid" flag.
    """
    fname = _slax_filename(script_name)
    remote_path = f"{VALIDATE_DIR}/{fname}"

    cmds = [VALIDATE_SETUP, *_write_remote_cmds(script, remote_path)]
    cmds.append(f'start shell command "{SLAXPROC} --check {remote_path}"')
    outputs = jmcp_client.run_commands(router, cmds)
    write_out = "\n".join(o for o in outputs[:-1] if o)
    check_out = outputs[-1]
    return {
        "router": router,
        "remote_path": remote_path,
        "write_output": write_out,
        "slaxproc_output": check_out,
        "valid": not write_out and _slax_is_valid(check_out),
    }


@mcp.tool()
def deploy_slax_script(
    script: str,
    router: str,
    script_type: str,
    script_name: str = "genscript",
    run: bool = False,
    op_args: str = "",
    commit_comment: str = "SLAX script deploy via junos-slax-mcp",
) -> dict[str, object]:
    """Validate, install, and commit a SLAX script on a Junos device.

    Steps, all performed through junos-mcp-server tools:
      1. Validate the script with slaxproc --check; abort if invalid.
      2. Write the script into the per-type scripts directory on the device.
      3. Commit the matching config statement (e.g. "set system scripts op
         file <name>.slax", or the event-options stanza for event scripts).
      4. For op scripts with run=True, execute "op <name> <op_args>".

    Args:
        script: The full SLAX script text.
        router: Device name from the junos-mcp-server devices.json.
        script_type: "op", "commit", "event", or "snmp".
        script_name: Base name for the file (".slax" appended).
        run: If True and script_type is "op", run the script after commit.
        op_args: Extra arguments passed when running an op script.
        commit_comment: Commit log comment.

    Returns a step-by-step report including validation, install, commit, and
    (optionally) run output. Deployment is skipped if validation fails.
    """
    stype = script_type.strip().lower()
    if stype not in SCRIPT_TYPES:
        return {"error": f"script_type must be one of {', '.join(SCRIPT_TYPES)}"}

    fname = _slax_filename(script_name)

    validation = validate_slax_on_device(script, router, script_name)
    if not validation["valid"]:
        return {
            "router": router,
            "script_type": stype,
            "deployed": False,
            "validation": validation,
            "message": "slaxproc --check failed; script not deployed.",
        }

    remote_dir, config_line = deploy_target(stype, fname)
    remote_path = f"{remote_dir}/{fname}"

    install_cmds = _write_remote_cmds(script, remote_path)
    calls = [jmcp_client.command_call(router, c) for c in install_cmds]
    calls.append(
        jmcp_client.commit_call(
            router, config_line, config_format="set", commit_comment=commit_comment
        )
    )
    run_cmd = f"op {fname[:-5]} {op_args}".strip()
    if run and stype == "op":
        calls.append(jmcp_client.command_call(router, run_cmd))
    outputs = jmcp_client.call_tools(calls)
    n = len(install_cmds)
    install_out = "\n".join(o for o in outputs[:n] if o)
    commit_out = outputs[n]

    report: dict[str, object] = {
        "router": router,
        "script_type": stype,
        "deployed": True,
        "validation": validation,
        "remote_path": remote_path,
        "config": config_line,
        "install_output": install_out,
        "commit_output": commit_out,
    }

    if run and stype == "op":
        report["run_command"] = run_cmd
        report["run_output"] = outputs[n + 1]

    return report


@mcp.tool()
def check_device_connectivity() -> dict[str, object]:
    """Verify junos-mcp-server is reachable and list its available tools.

    Launches junos-mcp-server over stdio and returns the tool names it
    advertises. Use this to confirm the device backend is wired up before
    calling validate_slax_on_device or deploy_slax_script.
    """
    try:
        tools = jmcp_client.list_tools()
        return {"ok": True, "jmcp_tools": tools}
    except Exception as exc:  # noqa: BLE001 - surface the failure to the client
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    mcp.run()
