import json
from pathlib import Path

import pytest

from bridge.artifact_store import FilePathResolver, SessionArtifactStore
from bridge.freyja_bridge import _new_tracing_registry
from bridge.tools.base import ToolCall, ToolRegistry
from bridge.tools.file_tools import WriteFileTool


def test_file_path_resolver_routes_new_outputs_to_project(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    workspace.mkdir()
    project.mkdir()
    (workspace / "src.py").write_text("print('workspace')\n", encoding="utf-8")
    (project / "notes.md").write_text("artifact notes\n", encoding="utf-8")

    resolver = FilePathResolver(workspace=workspace, project_dir=project)

    write_args = resolver.normalize_tool_arguments(
        "write_file",
        {"path": "out/research/r1.md", "content": "report"},
    )
    assert write_args["path"] == str((project / "out/research/r1.md").resolve())
    assert write_args["resolvedPath"] == write_args["path"]

    read_workspace = resolver.normalize_tool_arguments("read_file", {"path": "src.py"})
    assert read_workspace["path"] == str((workspace / "src.py").resolve())

    read_project = resolver.normalize_tool_arguments("read_file", {"path": "notes.md"})
    assert read_project["path"] == str((project / "notes.md").resolve())

    explicit_workspace = resolver.normalize_tool_arguments(
        "write_file",
        {"path": "docs/output.md", "base": "workspace", "content": "docs"},
    )
    assert explicit_workspace["path"] == str((workspace / "docs/output.md").resolve())


def test_session_artifact_store_records_files_and_change_sets(tmp_path: Path) -> None:
    project = tmp_path / "session-project"
    store = SessionArtifactStore(session_id="session-test", project_dir=project)
    store.ensure()

    artifact = project / "out" / "report.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# Report\n\nbody\n", encoding="utf-8")

    direct = store.record_file(
        artifact,
        creator_id="sub-1",
        creator_label="Research agent",
        operation="write",
        source="tool",
        tool_call_id="call-1",
    )
    assert direct["exists"] is True
    assert direct["lines"] == 3
    assert direct["creatorId"] == "sub-1"

    change_set = {
        "id": "fcs-call-2",
        "toolCallId": "call-2",
        "toolName": "write_file",
        "source": "tool",
        "files": [
            {
                "path": str(artifact),
                "operation": "update",
                "additions": 2,
                "deletions": 1,
                "binary": False,
                "diffTruncated": False,
            }
        ],
    }
    records = store.record_change_set(
        change_set,
        creator_id="sub-1",
        creator_label="Research agent",
    )
    assert len(records) == 1
    assert records[0]["changeSetId"] == "fcs-call-2"

    latest = store.latest_by_path(creator_id="sub-1")
    assert len(latest) == 1
    assert latest[0]["path"] == str(artifact.resolve())
    assert store.paths_for_creator("sub-1") == [str(artifact.resolve())]
    assert store.resolve_ref("report.md") == artifact.resolve()

    manifest_rows = [
        json.loads(line)
        for line in store.manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(manifest_rows) == 2


@pytest.mark.asyncio
async def test_tracing_registry_normalizes_write_file_and_records_artifact(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    workspace.mkdir()
    project.mkdir()
    resolver = FilePathResolver(workspace=workspace, project_dir=project)
    store = SessionArtifactStore(session_id="session-test", project_dir=project)
    store.ensure()

    registry = ToolRegistry()
    registry.register(WriteFileTool())
    traced = _new_tracing_registry(
        registry,
        "sub-1",
        path_resolver=resolver,
        artifact_store=store,
        label_for_session=lambda _session_id: "Research agent",
    )
    call = ToolCall(
        id="call-write",
        name="write_file",
        arguments={"path": "out/research/r1.md", "content": "deep report\n"},
    )

    result = await traced.execute(call)

    expected = project / "out" / "research" / "r1.md"
    assert not result.is_error
    assert expected.read_text(encoding="utf-8") == "deep report\n"
    assert call.arguments["path"] == str(expected.resolve())

    rows = store.latest_by_path(creator_id="sub-1")
    assert len(rows) == 1
    assert rows[0]["path"] == str(expected.resolve())
