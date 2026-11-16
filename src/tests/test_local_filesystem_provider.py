from unittest.mock import patch

import pytest
from agents.utils.mcp_providers import (
    LocalFilesystemMcpProvider,
    McpProviderNotConfiguredError,
    McpProviderRegistry,
    create_local_filesystem_toolset,
)
from database import models


def test_local_filesystem_toolset_tools(tmp_path):
    # Setup test directory with files
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text(
        "def hello():\n    return 'world'\n", encoding="utf-8"
    )
    (src_dir / "secret.py").write_text("SECRET = 42\n", encoding="utf-8")

    toolset = create_local_filesystem_toolset(
        tmp_path,
        allowed_tools=[
            "get_file_contents",
            "list_directory",
            "search_code",
            "create_or_update_file",
        ],
    )
    tools_dict = (
        toolset.tools
        if isinstance(toolset.tools, dict)
        else {t.name: t for t in toolset.tools}
    )
    assert "get_file_contents" in tools_dict
    assert "list_directory" in tools_dict
    assert "search_code" in tools_dict
    assert "create_or_update_file" in tools_dict

    # 1. Test get_file_contents
    content = tools_dict["get_file_contents"].function("src/app.py")
    assert "def hello():" in content

    # 2. Test list_directory
    listed = tools_dict["list_directory"].function("src")
    assert "app.py" in listed
    assert "secret.py" in listed

    # 3. Test search_code
    search_res = tools_dict["search_code"].function("world")
    assert "src/app.py:2" in search_res or "app.py" in search_res

    # 4. Test create_or_update_file
    write_res = tools_dict["create_or_update_file"].function("src/new.py", "x = 10\n")
    assert "Successfully wrote" in write_res
    assert (src_dir / "new.py").read_text() == "x = 10\n"

    # 5. Test Path Traversal security
    traversal_res = tools_dict["get_file_contents"].function("../../etc/passwd")
    assert "Error" in traversal_res or "escapes" in traversal_res


def test_mcp_provider_registry():
    provider = McpProviderRegistry.get("local_filesystem", "scm")
    assert isinstance(provider, LocalFilesystemMcpProvider)


def test_local_provider_disabled_without_docker(db_session):
    org = models.Organization(name="TestOrg")
    db_session.add(org)
    db_session.commit()

    proj = models.Project(name="TestProj", organization_id=org.id)
    db_session.add(proj)
    db_session.commit()

    provider = LocalFilesystemMcpProvider()
    with patch("utils.environment.is_local_workspace_enabled", return_value=False):
        with pytest.raises(McpProviderNotConfiguredError):
            provider.build_toolset(db_session, org, proj.id, "ask_git")
