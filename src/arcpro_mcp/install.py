"""`arcpro-mcp install`: set up Claude Desktop and the ArcGIS Pro bridge in one step.

It does three things and prints what it did:

1. Copies the bridge (arcpro_bridge.py and the ArcProMCP.pyt toolbox) to a fixed folder,
   %LOCALAPPDATA%\\arcpro-mcp\\bridge, so the toolbox added in Pro keeps working across updates.
   When run from a source checkout, the checkout's own bridge folder is used instead.
2. Adds or updates the "arcgis-pro" entry in Claude Desktop's config, after a backup. Nothing
   else in the file changes, and a file it cannot parse is left alone.
3. Prints the next steps: restart Claude Desktop, add the toolbox in Pro (or paste one line).

Also used by the client to tell the user exactly where the bridge is.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sys
from pathlib import Path

from . import __version__

SERVER_NAME = "arcgis-pro"
BRIDGE_FILES = ("arcpro_bridge.py", "ArcProMCP.pyt")
PACKAGE_DIR = Path(__file__).resolve().parent


class InstallError(RuntimeError):
    pass


# -- where things are ----------------------------------------------------------------------

def repo_root() -> Path | None:
    """The source checkout this package runs from, if any (src layout: <repo>/src/arcpro_mcp)."""
    root = PACKAGE_DIR.parent.parent
    try:
        meta = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    if re.search(r'^name\s*=\s*"arcpro-mcp"', meta, re.M) and (root / "bridge" / BRIDGE_FILES[0]).is_file():
        return root
    return None


def bundled_bridge_dir() -> Path | None:
    """The bridge that ships with this server: inside the wheel, or the checkout's bridge folder."""
    inside = PACKAGE_DIR / "bridge"
    if (inside / BRIDGE_FILES[0]).is_file():
        return inside
    root = repo_root()
    return root / "bridge" if root else None


def default_install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "arcpro-mcp" / "bridge"


def bridge_version_of(script: Path) -> str | None:
    """Read BRIDGE_VERSION from a bridge script without running it."""
    try:
        text = script.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'^BRIDGE_VERSION\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else None


def bridge_script() -> Path | None:
    """The bridge script the user should start: the installed copy when it matches this server,
    otherwise the one that ships with it."""
    installed = default_install_dir() / BRIDGE_FILES[0]
    if bridge_version_of(installed) == __version__:
        return installed
    bundled = bundled_bridge_dir()
    if bundled:
        return bundled / BRIDGE_FILES[0]
    return installed if installed.is_file() else None


def startup_line(script: Path | None = None) -> str:
    """The one line to paste into Pro's Python window (it mangles multi-line pastes)."""
    script = script or bridge_script()
    path = str(script) if script else r"<path to arcpro-mcp>\bridge\arcpro_bridge.py"
    return 'exec(open(r"%s").read()); start(allow_exec=False)' % path


# -- the server command Claude runs --------------------------------------------------------

def _find_exe(name: str) -> str | None:
    """uv or uvx: from uv itself (it sets UV when it runs us), PATH, or its default install folder."""
    exe = name + (".exe" if os.name == "nt" else "")
    uv = os.environ.get("UV")
    if uv and Path(uv).name.lower().startswith("uv"):
        sibling = Path(uv).with_name(exe)
        if sibling.is_file():
            return str(sibling)
    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())
    for folder in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin"):
        if (folder / exe).is_file():
            return str(folder / exe)
    return None


def server_entry(source: str, exe: str | None = None) -> dict:
    """The mcpServers entry. Paths are absolute: Claude Desktop does not use the user's PATH."""
    if source == "repo":
        root = repo_root()
        if root is None:
            raise InstallError("--source repo needs this command to run from an arcpro-mcp checkout.")
        uv = exe or _find_exe("uv")
        if not uv:
            raise InstallError(_NO_UV)
        return {"command": uv, "args": ["--directory", str(root), "run", "--frozen", "arcpro-mcp"]}
    uvx = exe or _find_exe("uvx")
    if not uvx:
        raise InstallError(_NO_UV)
    # Pinned, so the server only changes when the user runs install again, which also updates
    # the bridge files: the two stay the same version.
    return {"command": uvx, "args": ["arcpro-mcp@%s" % __version__]}


_NO_UV = ("Could not find uv. Install it in PowerShell with:\n"
          '    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"\n'
          "then open a new terminal and run this again.")


# -- Claude Desktop's config ---------------------------------------------------------------

def claude_config_paths() -> list[Path]:
    """Config files Claude Desktop may read. The Microsoft Store build keeps its own copy."""
    paths = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            paths += sorted(Path(local, "Packages").glob(
                "Claude_*/LocalCache/Roaming/Claude/claude_desktop_config.json"))
    else:
        paths.append(Path.home() / "Library" / "Application Support" / "Claude"
                     / "claude_desktop_config.json")
    existing = [p for p in paths if p.is_file()]
    if existing:
        return existing
    return [p for p in paths[:1] if p.parent.is_dir()]  # Claude installed, never configured


def merge_config(path: Path, entry: dict, dry_run: bool = False) -> tuple[str, Path | None, dict | None]:
    """Put entry under mcpServers[SERVER_NAME]. Returns (what happened, backup, previous entry)."""
    try:
        text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    except OSError as e:
        raise InstallError("Could not read %s: %s" % (path, e))
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as e:
        raise InstallError("%s is not valid JSON (%s), so it was left alone. Fix it, or add the "
                           "server by hand." % (path, e))
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise InstallError("%s does not look like a Claude Desktop config, so it was left alone." % path)
    servers = data.setdefault("mcpServers", {})
    old = servers.get(SERVER_NAME)
    if old == entry:
        return "already set", None, None
    action = "updated" if old is not None else "added"
    if dry_run:
        return "would be " + action, None, old
    servers[SERVER_NAME] = entry
    backup = None
    if path.exists():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name("%s.bak-%s" % (path.name, stamp))
        shutil.copy2(path, backup)
    _replace(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
    return action, backup, old


def _replace(path: Path, data: bytes) -> None:
    """Write a file in one step, so nothing ever reads half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise InstallError("Could not write %s: %s. If ArcGIS Pro or Claude Desktop has it open, "
                           "close it and run this again." % (path, e))


# -- the bridge files ----------------------------------------------------------------------

def copy_bridge(src: Path, dest: Path, dry_run: bool = False) -> str:
    if src.resolve() == dest.resolve():
        return "used in place"
    changed = [n for n in BRIDGE_FILES
               if not (dest / n).is_file() or (dest / n).read_bytes() != (src / n).read_bytes()]
    if not changed:
        return "already up to date"
    fresh = not (dest / BRIDGE_FILES[0]).exists()
    if dry_run:
        return "would be " + ("copied" if fresh else "updated")
    for n in changed:
        _replace(dest / n, (src / n).read_bytes())
    return "copied" if fresh else "updated"


# -- the command ---------------------------------------------------------------------------

def install(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="arcpro-mcp install",
                                description="Set up Claude Desktop and the ArcGIS Pro bridge.")
    p.add_argument("--source", choices=("auto", "pypi", "repo"), default="auto",
                   help="How Claude runs the server: the published package (pypi) or this "
                        "checkout (repo). auto picks repo when run from a checkout.")
    p.add_argument("--bridge-dir", type=Path, default=None,
                   help="Where to put the bridge files (default %%LOCALAPPDATA%%\\arcpro-mcp\\bridge).")
    p.add_argument("--config", type=Path, action="append", default=None,
                   help="Claude Desktop config file to update (default: found automatically).")
    p.add_argument("--no-claude-desktop", action="store_true",
                   help="Leave Claude Desktop's config alone (for Claude Code only).")
    p.add_argument("--dry-run", action="store_true", help="Show what would change; change nothing.")
    a = p.parse_args(argv)

    source = a.source
    if source == "auto":
        source = "repo" if repo_root() else "pypi"
    src = bundled_bridge_dir()
    if src is None:
        raise InstallError("This copy of arcpro-mcp has no bridge files. Reinstall it.")
    if a.bridge_dir:
        dest = a.bridge_dir
    elif source == "repo":
        dest = src
    else:
        dest = default_install_dir()

    out = []
    say = out.append
    say("arcpro-mcp %s setup%s" % (__version__, " (dry run: nothing changed)" if a.dry_run else ""))
    say("")
    say("1. Bridge files: %s" % copy_bridge(src, dest, a.dry_run))
    say("   %s" % dest)

    entry = server_entry(source)
    command = " ".join(_quote(x) for x in [entry["command"]] + entry["args"])
    say("")
    if a.no_claude_desktop:
        say("2. Claude Desktop: skipped")
    else:
        configs = a.config or claude_config_paths()
        if not configs:
            say("2. Claude Desktop: not found, skipped. Install it and run this again, or use "
                "Claude Code (below).")
        for path in configs:
            what, backup, old = merge_config(path, entry, a.dry_run)
            say("2. Claude Desktop: \"%s\" server %s" % (SERVER_NAME, what))
            say("   %s" % path)
            if old is not None:
                say("   it was: %s" % json.dumps(old))
            if backup:
                say("   backup: %s" % backup)
    say("   Server command: %s" % command)
    say("   For Claude Code, run instead:")
    say("     claude mcp add --scope user %s -- %s" % (SERVER_NAME, command))

    toolbox = dest / BRIDGE_FILES[1]
    say("")
    say("3. Next steps")
    if not a.no_claude_desktop:
        say("   a. Fully quit Claude Desktop (tray icon > Quit) and open it again.")
    say("   b. In ArcGIS Pro, once: Catalog pane > Toolboxes > right-click > Add Toolbox >")
    say("        %s" % toolbox)
    say("      then right-click it > Add To Favorites, so every project has it.")
    say("      Each time you open Pro, run its Start Bridge tool. Or paste this one line into")
    say("      View > Python window:")
    say("        %s" % startup_line(dest / BRIDGE_FILES[0]))
    say("   c. Ask Claude: \"Check the ArcGIS Pro connection and tell me what's in my project.\"")
    if source == "pypi":
        say("")
        say("To update later: uvx arcpro-mcp@latest install, then restart Claude and the bridge.")
    print("\n".join(out))
    return 0


def _quote(arg: str) -> str:
    return '"%s"' % arg if (" " in arg or not arg) else arg


USAGE = """usage: arcpro-mcp [install | --version]

With no arguments, runs the MCP server (this is what Claude starts).
  install     set up Claude Desktop and the ArcGIS Pro bridge (see install --help)
  --version   print the version"""


def cli(argv: list[str]) -> int:
    cmd = argv[0]
    if cmd in ("--version", "-V", "version"):
        print("arcpro-mcp %s" % __version__)
        return 0
    if cmd == "install":
        try:
            return install(argv[1:])
        except InstallError as e:
            print("arcpro-mcp install: %s" % e, file=sys.stderr)
            return 1
    print(USAGE, file=sys.stderr if cmd not in ("-h", "--help", "help") else sys.stdout)
    return 0 if cmd in ("-h", "--help", "help") else 2
