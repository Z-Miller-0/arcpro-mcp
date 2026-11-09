"""`arcpro-mcp install`, the version check between server and bridge, and packaging."""

import ast
import json
from pathlib import Path

import pytest

from test_end_to_end import call_tool, payload

HERE = Path(__file__).parent
ROOT = HERE.parent


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point every user folder the installer looks at into tmp_path."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.delenv("ARCPRO_MCP_IGNORE_VERSION", raising=False)
    from arcpro_mcp import install
    monkeypatch.setattr(install, "_find_exe", lambda name: r"C:\Tools\%s.exe" % name)
    return tmp_path


# -- versions ------------------------------------------------------------------------------

def test_one_version_everywhere():
    from arcpro_mcp import __version__
    from arcpro_mcp.install import bridge_version_of
    assert bridge_version_of(ROOT / "bridge" / "arcpro_bridge.py") == __version__
    assert "## %s" % __version__ in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_version_problem(monkeypatch):
    from arcpro_mcp import __version__, client
    monkeypatch.delenv("ARCPRO_MCP_IGNORE_VERSION", raising=False)
    major, minor = (int(x) for x in __version__.split(".")[:2])
    assert client.version_problem(__version__) is None
    assert client.version_problem("%d.%d.99" % (major, minor)) is None  # patch releases mix
    older = client.version_problem("0.1.0")
    assert "must match" in older and "Start Bridge" in older and "exec(open(" in older
    newer = client.version_problem("%d.%d.0" % (major, minor + 1))
    assert "reopen Claude" in newer
    assert "unknown" in client.version_problem(None)
    monkeypatch.setenv("ARCPRO_MCP_IGNORE_VERSION", "1")
    assert client.version_problem("0.1.0") is None


def test_old_bridge_is_reported_not_called(env, monkeypatch):
    monkeypatch.delenv("ARCPRO_MCP_IGNORE_VERSION", raising=False)
    state = env["state"]
    original = state.read_text()
    try:
        st = json.loads(original)
        st["bridge_version"] = "0.1.2"
        state.write_text(json.dumps(st))
        r = call_tool(env["mcp"], "list_layers")
        assert r.isError and "0.1.2" in r.content[0].text and "must match" in r.content[0].text
        # bridge_status still answers, so Claude can see what is wrong
        status = payload(call_tool(env["mcp"], "bridge_status"))
        assert status["server_version"] and status["bridge_version"] != "0.1.2"  # the live bridge
    finally:
        state.write_text(original)
    assert not call_tool(env["mcp"], "list_layers").isError


def test_bridge_status_reports_versions(env):
    from arcpro_mcp import __version__
    st = payload(call_tool(env["mcp"], "bridge_status"))
    assert st["server_version"] == st["bridge_version"] == __version__
    assert "version_problem" not in st


# -- where the bridge is ---------------------------------------------------------------------

def test_startup_line_is_one_python_line(home):
    from arcpro_mcp.install import bridge_script, startup_line
    script = bridge_script()
    assert script is not None and script.is_file()
    line = startup_line()
    assert "\n" not in line and str(script) in line
    ast.parse(line)


def test_installed_copy_is_preferred_only_when_it_matches(home):
    from arcpro_mcp import install
    dest = install.default_install_dir()
    install.copy_bridge(install.bundled_bridge_dir(), dest)
    assert install.bridge_script() == dest / "arcpro_bridge.py"
    stale = (dest / "arcpro_bridge.py").read_text(encoding="utf-8").replace(
        'BRIDGE_VERSION = "', 'BRIDGE_VERSION = "0.0.0-', 1)
    (dest / "arcpro_bridge.py").write_text(stale, encoding="utf-8")
    assert install.bridge_script() == install.bundled_bridge_dir() / "arcpro_bridge.py"


# -- the bridge files ------------------------------------------------------------------------

def test_copy_bridge(home):
    from arcpro_mcp.install import bundled_bridge_dir, copy_bridge
    src, dest = bundled_bridge_dir(), home / "b"
    assert copy_bridge(src, dest, dry_run=True) == "would be copied" and not dest.exists()
    assert copy_bridge(src, dest) == "copied"
    assert (dest / "ArcProMCP.pyt").read_bytes() == (src / "ArcProMCP.pyt").read_bytes()
    assert copy_bridge(src, dest) == "already up to date"
    (dest / "ArcProMCP.pyt").write_text("old")
    assert copy_bridge(src, dest) == "updated"
    assert copy_bridge(src, src) == "used in place"


# -- Claude Desktop's config -----------------------------------------------------------------

ENTRY = {"command": r"C:\Tools\uvx.exe", "args": ["arcpro-mcp@9.9.9"]}


def test_merge_into_new_config(home):
    from arcpro_mcp.install import merge_config
    path = home / "c" / "claude_desktop_config.json"
    what, backup, old = merge_config(path, ENTRY)
    assert what == "added" and backup is None and old is None
    assert json.loads(path.read_text()) == {"mcpServers": {"arcgis-pro": ENTRY}}


def test_merge_keeps_everything_else_and_backs_up(home):
    from arcpro_mcp.install import merge_config
    path = home / "claude_desktop_config.json"
    before = {"mcpServers": {"zotero": {"command": "z"},
                             "arcgis-pro": {"command": "old", "args": []}},
              "preferences": {"x": 1}}
    path.write_text("\ufeff" + json.dumps(before), encoding="utf-8")  # Notepad adds a BOM
    what, backup, old = merge_config(path, ENTRY)
    assert what == "updated" and old == {"command": "old", "args": []}
    after = json.loads(path.read_text())
    assert after["preferences"] == {"x": 1} and after["mcpServers"]["zotero"] == {"command": "z"}
    assert after["mcpServers"]["arcgis-pro"] == ENTRY
    assert json.loads(backup.read_text(encoding="utf-8-sig")) == before
    assert merge_config(path, ENTRY) == ("already set", None, None)


def test_merge_dry_run_changes_nothing(home):
    from arcpro_mcp.install import merge_config
    path = home / "claude_desktop_config.json"
    path.write_text("{}")
    assert merge_config(path, ENTRY, dry_run=True)[0] == "would be added"
    assert path.read_text() == "{}" and list(home.iterdir()) == [path]


@pytest.mark.parametrize("text", ["{not json", "[]", '{"mcpServers": []}'])
def test_merge_leaves_a_bad_config_alone(home, text):
    from arcpro_mcp.install import InstallError, merge_config
    path = home / "claude_desktop_config.json"
    path.write_text(text)
    with pytest.raises(InstallError, match="left alone"):
        merge_config(path, ENTRY)
    assert path.read_text() == text and list(home.iterdir()) == [path]


def test_config_paths(home):
    from arcpro_mcp.install import claude_config_paths
    assert claude_config_paths() == []  # Claude Desktop not installed
    (home / "roaming" / "Claude").mkdir(parents=True)
    assert claude_config_paths() == [home / "roaming" / "Claude" / "claude_desktop_config.json"]
    store = home / "local" / "Packages" / "Claude_abc" / "LocalCache" / "Roaming" / "Claude"
    store.mkdir(parents=True)
    (store / "claude_desktop_config.json").write_text("{}")
    assert claude_config_paths() == [store / "claude_desktop_config.json"]


def test_server_entry(home):
    from arcpro_mcp import __version__
    from arcpro_mcp.install import server_entry
    assert server_entry("pypi") == {"command": r"C:\Tools\uvx.exe",
                                    "args": ["arcpro-mcp@%s" % __version__]}
    repo = server_entry("repo")
    assert repo["command"] == r"C:\Tools\uv.exe"
    assert repo["args"] == ["--directory", str(ROOT), "run", "--frozen", "arcpro-mcp"]


def test_no_uv_is_explained(home, monkeypatch):
    from arcpro_mcp import install
    monkeypatch.setattr(install, "_find_exe", lambda name: None)
    with pytest.raises(install.InstallError, match="astral.sh/uv"):
        install.server_entry("pypi")


# -- the whole command -----------------------------------------------------------------------

def test_install_pypi(home, capsys):
    from arcpro_mcp import __version__
    from arcpro_mcp.server import main
    (home / "roaming" / "Claude").mkdir(parents=True)
    with pytest.raises(SystemExit) as done:
        main(["install", "--source", "pypi"])
    assert done.value.code == 0
    out = capsys.readouterr().out
    dest = home / "local" / "arcpro-mcp" / "bridge"
    assert (dest / "arcpro_bridge.py").is_file() and (dest / "ArcProMCP.pyt").is_file()
    config = json.loads((home / "roaming" / "Claude" / "claude_desktop_config.json").read_text())
    assert config["mcpServers"]["arcgis-pro"]["args"] == ["arcpro-mcp@%s" % __version__]
    assert str(dest / "ArcProMCP.pyt") in out and str(dest / "arcpro_bridge.py") in out
    assert "claude mcp add --scope user arcgis-pro" in out and "@latest install" in out

    with pytest.raises(SystemExit):
        main(["install", "--source", "pypi"])
    again = capsys.readouterr().out
    assert "already up to date" in again and "already set" in again


def test_install_from_checkout_uses_it_in_place(home, capsys):
    from arcpro_mcp.server import main
    config = home / "cfg.json"
    with pytest.raises(SystemExit) as done:
        main(["install", "--config", str(config)])
    assert done.value.code == 0
    out = capsys.readouterr().out
    assert "used in place" in out and str(ROOT / "bridge" / "ArcProMCP.pyt") in out
    assert json.loads(config.read_text())["mcpServers"]["arcgis-pro"]["args"][1] == str(ROOT)
    assert not (home / "local").exists()


def test_install_dry_run_and_errors(home, capsys):
    from arcpro_mcp.server import main
    config = home / "cfg.json"
    config.write_text("{oops")
    with pytest.raises(SystemExit) as done:
        main(["install", "--source", "pypi", "--dry-run", "--no-claude-desktop"])
    assert done.value.code == 0 and "would be copied" in capsys.readouterr().out
    assert not (home / "local").exists()
    with pytest.raises(SystemExit) as done:
        main(["install", "--source", "pypi", "--config", str(config)])
    assert done.value.code == 1 and "not valid JSON" in capsys.readouterr().err
    assert config.read_text() == "{oops"


def test_cli_version_and_usage(capsys):
    from arcpro_mcp import __version__
    from arcpro_mcp.server import main
    with pytest.raises(SystemExit) as done:
        main(["--version"])
    assert done.value.code == 0 and __version__ in capsys.readouterr().out
    with pytest.raises(SystemExit) as done:
        main(["bogus"])
    assert done.value.code == 2 and "install" in capsys.readouterr().err
