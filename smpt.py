#!/usr/bin/env python3
"""
smpt - Simple Multi-Package Tool
Discovery prototype: 'smpt search <package>' only.
"""

import sys
import os
import subprocess
import configparser
import tty
import termios
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PackageResult:
    """A single search result from one backend."""
    name: str
    version: str
    description: str
    backend: str        # e.g. "flatpak", "brew", "rpm-ostree"
    app_id: str = ""   # backend-specific ID (e.g. flatpak application ID)
                       # used for name-filter matching when display name differs

    def display_name(self) -> str:
        return f"{self.name} ({self.version})" if self.version else self.name

    def matches_query(self, query: str) -> bool:
        """Return True if query appears in the name or app_id (case-insensitive)."""
        q = query.lower()
        return q in self.name.lower() or (bool(self.app_id) and q in self.app_id.lower())



@dataclass
class MergedResult:
    """A deduplicated result that may come from multiple backends."""
    name: str
    version: str                               # from the highest-priority backend
    description: str                           # from the highest-priority backend
    backends: list[str] = field(default_factory=list)
    app_ids: dict[str, str] = field(default_factory=dict)       # backend -> app_id
    descriptions: dict[str, str] = field(default_factory=dict)  # backend -> description

    def source_tag(self) -> str:
        if len(self.backends) == 1:
            return self.backends[0]
        return "multiple"

    def pkg_id(self, backend: str) -> str:
        """Return the best identifier for this package on a given backend.
        Falls back to the package name if no specific app_id is stored."""
        return self.app_ids.get(backend) or self.name

    def desc_for(self, backend: str) -> str:
        """Return the description from a specific backend, falling back to
        the primary description if that backend didn't provide one."""
        return self.descriptions.get(backend) or self.description


@dataclass
class ResolvedPackage:
    """A single package that has been resolved to a specific backend."""
    query:   str           # the original search term the user typed
    pkg:     MergedResult  # the chosen result
    backend: str           # the chosen backend to install from

    def pkg_id(self) -> str:
        return self.pkg.pkg_id(self.backend)

    def install_id(self) -> str:
        """The identifier to pass to the install command for this backend."""
        return self.pkg.pkg_id(self.backend)



# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

# Default config content written to disk on first run.
# Comments are preserved so the file is self-documenting for the user.

def get_script_dir() -> Path:
    """Return the directory the smpt script lives in."""
    return Path(os.path.abspath(__file__)).parent


def get_system_config_path() -> Path:
    """Return the OS-appropriate user config file path (not the local one)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        # Linux: respect XDG_CONFIG_HOME, fall back to ~/.config
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "smpt" / "config.ini"


def is_ostree_system() -> bool:
    """Return True if this looks like an rpm-ostree / atomic distro."""
    return Path("/run/ostree-booted").exists()


def build_default_config() -> str:
    """
    Generate the default config text, disabling dnf automatically
    if we're on an rpm-ostree system (where dnf is unreliable as a backend).
    """
    on_ostree = is_ostree_system()
    dnf_enabled = "false" if on_ostree else "true"
    dnf_note = (
        "\n# Disabled automatically: dnf is unreliable on rpm-ostree systems.\n"
        "# rpm-ostree uses dnf internally for its own search, so this is safe."
        if on_ostree else ""
    )

    return f"""\
[smpt]
# Enable ANSI colour output. Set to false if your terminal doesn't support it.
color = true

# Whether to filter search results to only packages whose name contains the
# query string. Set to false to see all results (including description matches).
name_filter = true

# ---------------------------------------------------------------------------
# Backend sections
# Each backend has three settings:
#   enabled         = true/false  -- set false to never use this backend
#   priority        = integer     -- lower number = checked/preferred first
#   warn_on_install = true/false  -- show a warning before installing
# ---------------------------------------------------------------------------

[flatpak]
enabled = true
priority = 10
warn_on_install = false

[brew]
enabled = true
priority = 20
warn_on_install = false

[cargo]
enabled = true
priority = 30
warn_on_install = false

[apt]
enabled = true
priority = 40
warn_on_install = false

[dnf]{dnf_note}
enabled = {dnf_enabled}
priority = 40
warn_on_install = false

[pacman]
enabled = true
priority = 40
warn_on_install = false

[rpm-ostree]
enabled = true
priority = 50
# rpm-ostree layers packages onto your immutable image and requires a reboot.
warn_on_install = true

[appman]
enabled = true
priority = 25
warn_on_install = false
"""


def find_config_path() -> tuple[Path, bool]:
    """
    Search for a config file in priority order:
      1. Same directory as this script  (portable / USB use)
      2. OS user config directory

    Returns (path, existed) where existed=False means we just created it.
    """
    local_cfg = get_script_dir() / "config.ini"
    if local_cfg.exists():
        return local_cfg, True

    system_cfg = get_system_config_path()
    if system_cfg.exists():
        return system_cfg, True

    # Neither exists — generate and write the config automatically
    system_cfg.parent.mkdir(parents=True, exist_ok=True)
    config_text = build_default_config()
    system_cfg.write_text(config_text, encoding="utf-8")

    if is_ostree_system():
        print(f"[smpt] Detected rpm-ostree system — dnf disabled in config.")
    print(f"[smpt] Created default config at: {system_cfg}\n")

    return system_cfg, False


def load_config() -> configparser.ConfigParser:
    """Load config from disk (creating it if needed) and return a ConfigParser."""
    cfg_path, _ = find_config_path()

    config = configparser.ConfigParser()
    config.read(cfg_path, encoding="utf-8")
    return config


# Default priority for backends not mentioned in the config file.
# Keeps them usable but ranked below any explicitly configured backends.
_DEFAULT_PRIORITY = {
    "flatpak":    10,
    "appman":     20,
    "brew":       20,
    "cargo":      40,
    "apt":        40,
    "dnf":        40,
    "pacman":     40,
    "rpm-ostree": 50,
}


def get_enabled_backends(config: configparser.ConfigParser) -> list[str]:
    """
    Return backends that are enabled in config AND actually available on
    this machine, sorted by priority (ascending = highest priority first).

    Backends known to the program but missing from the config fall back to
    built-in defaults (enabled, default priority) so a partial or minimal
    config file never silently disables working backends.
    """
    # Start with every backend the program knows about, then overlay config
    all_known   = set(_DEFAULT_PRIORITY.keys())
    in_config   = {s for s in config.sections() if s != "smpt"}
    all_backends = all_known | in_config   # config can also define new ones

    available = []
    for name in all_backends:
        # enabled: config wins; missing section → default True
        enabled = config.getboolean(name, "enabled", fallback=True)
        if not enabled:
            continue
        if not is_backend_available(name):
            continue
        # priority: config wins; missing section → built-in default or 99
        priority = config.getint(name, "priority",
                                 fallback=_DEFAULT_PRIORITY.get(name, 99))
        available.append((priority, name))

    available.sort(key=lambda x: x[0])
    return [name for _, name in available]


# ---------------------------------------------------------------------------
# Backend availability detection
# ---------------------------------------------------------------------------

def command_exists(cmd: str) -> bool:
    """Return True if `cmd` is on PATH."""
    import shutil
    return shutil.which(cmd) is not None


def is_backend_available(backend: str) -> bool:
    """Check whether a backend is actually usable on this system."""
    checks = {
        "flatpak":    lambda: command_exists("flatpak"),
        "brew":       lambda: command_exists("brew"),
        "cargo":      lambda: command_exists("cargo"),
        "apt":        lambda: command_exists("apt-cache"),
        "dnf":        lambda: command_exists("dnf"),
        "pacman":     lambda: command_exists("pacman"),
        "rpm-ostree": lambda: command_exists("rpm-ostree") and Path("/run/ostree-booted").exists(),
        "appman":     lambda: command_exists("appman"),
        # Future backends go here
    }
    check = checks.get(backend)
    return check() if check else False


# ---------------------------------------------------------------------------
# Search backends
# ---------------------------------------------------------------------------

def run(args: list[str], timeout: int = 15) -> tuple[str, str, int]:
    """
    Run a subprocess and return (stdout, stderr, returncode).
    Never raises -- errors surface as non-zero returncodes.
    """
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", f"[smpt] Timeout running: {' '.join(args)}", 1
    except FileNotFoundError:
        return "", f"[smpt] Command not found: {args[0]}", 1


def _parse_flatpak_lines(lines: list[str], query: str = "") -> list[PackageResult]:
    """
    Shared parser for `flatpak search` and `flatpak list` output.

    Uses the last segment of the application ID as the package name
    (e.g. "org.virt_manager.virt-manager" -> "virt-manager") so that
    flatpak results deduplicate correctly against other backends.
    The human-readable display name is prepended to the description so
    it remains visible in the results table.
    """
    results = []
    q = query.lower()
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        display_name = parts[0].strip()
        app_id       = parts[1].strip()
        version      = parts[2].strip() if len(parts) > 2 else ""
        desc         = parts[3].strip() if len(parts) > 3 else ""

        if not app_id or display_name.lower() == "name":
            continue  # skip empty / header rows

        # Use the last segment of the app ID as the canonical package name
        pkg_name = app_id.rsplit(".", 1)[-1]

        # Combine display name + description so neither is lost
        full_desc = f"{display_name}: {desc}" if desc else display_name

        # If a query filter is active, apply it here
        if q and q not in pkg_name.lower() and q not in app_id.lower() and q not in display_name.lower():
            continue

        results.append(PackageResult(pkg_name, version, full_desc, "flatpak", app_id))
    return results


def search_flatpak(query: str) -> list[PackageResult]:
    stdout, _, rc = run(["flatpak", "search", "--columns=name,application,version,description", query])
    if rc != 0 or not stdout.strip():
        return []
    return _parse_flatpak_lines(stdout.strip().splitlines(), query)


def search_brew(query: str) -> list[PackageResult]:
    stdout, _, rc = run(["brew", "search", query])
    if rc != 0 or not stdout.strip():
        return []

    results = []
    current_section = "formula"
    for line in stdout.strip().splitlines():
        line = line.strip()
        if line.startswith("==>"):
            current_section = "cask" if "Cask" in line else "formula"
            continue
        if line:
            # brew search doesn't give versions inline; leave blank for now
            tag = f"brew/{current_section}"
            results.append(PackageResult(line, "", "", tag))
    return results


def search_cargo(query: str) -> list[PackageResult]:
    # `cargo search` returns "name = version  # description" lines,
    # followed by a trailing "... and N crates more (use --)" message.
    stdout, _, rc = run(["cargo", "search", query])
    if rc != 0 or not stdout.strip():
        return []

    results = []
    for line in stdout.strip().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        # Skip cargo's trailing truncation notice — real crate names never
        # contain spaces or start with "..."
        if line.startswith("...") or " " in line.split("=")[0].strip():
            continue
        # Format:  name = "version"  # description
        try:
            name_ver, _, desc = line.partition("#")
            name, _, version  = name_ver.partition("=")
            name    = name.strip()
            version = version.strip().strip('"')
            desc    = desc.strip()
            if name:
                results.append(PackageResult(name, version, desc, "cargo"))
        except ValueError:
            continue
    return results


def search_apt(query: str) -> list[PackageResult]:
    stdout, _, rc = run(["apt-cache", "search", query])
    if rc != 0 or not stdout.strip():
        return []

    results = []
    for line in stdout.strip().splitlines():
        # Format:  name - description  (no version inline)
        name, sep, desc = line.partition(" - ")
        if sep:
            results.append(PackageResult(name.strip(), "", desc.strip(), "apt"))
    return results


def parse_dnf_output(stdout: str, backend: str) -> list[PackageResult]:
    """
    Parse output of `dnf search` / `dnf5 search` into PackageResults.

    dnf4 format:  "name.arch : description"
    dnf5 format:  "name.arch    description"
                  with section headers like "Matched fields: name, summary"
    """
    results = []
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Skip dnf5 section headers and loading messages
        if line.startswith(("Matched fields:", "Updating", "Repositories", "=")):
            continue

        if " : " in line:
            # dnf4 style
            pkg, _, desc = line.partition(" : ")
            name = pkg.strip().rsplit(".", 1)[0]
            results.append(PackageResult(name, "", desc.strip(), backend))
        else:
            # dnf5 style — whitespace-separated, first token is name.arch
            parts = line.split(None, 1)
            if len(parts) == 2 and "." in parts[0]:
                name = parts[0].rsplit(".", 1)[0]
                desc = parts[1].strip()
                results.append(PackageResult(name, "", desc, backend))

    return results


def search_dnf(query: str) -> list[PackageResult]:
    stdout, _, rc = run(["dnf", "search", query])
    if rc != 0 or not stdout.strip():
        return []
    return parse_dnf_output(stdout, "dnf")


def search_pacman(query: str) -> list[PackageResult]:
    stdout, _, rc = run(["pacman", "-Ss", query])
    if rc != 0 or not stdout.strip():
        return []

    results = []
    lines = stdout.strip().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Package lines start with "repo/name version"
        if "/" in line and not line.startswith(" "):
            parts  = line.split()
            repo_name = parts[0] if parts else ""
            version   = parts[1] if len(parts) > 1 else ""
            name = repo_name.split("/")[-1]
            desc = lines[i + 1].strip() if i + 1 < len(lines) else ""
            results.append(PackageResult(name, version, desc, "pacman"))
            i += 2
        else:
            i += 1
    return results


def search_rpm_ostree(query: str) -> list[PackageResult]:
    # rpm-ostree has no search command — delegate to dnf/dnf5.
    stdout, _, rc = run(["dnf", "search", query])
    if rc != 0 or not stdout.strip():
        return []
    # Reuse the same parser but tag results as rpm-ostree so the
    # install step knows to call `rpm-ostree install` not `dnf install`.
    return parse_dnf_output(stdout, "rpm-ostree")


# ---------------------------------------------------------------------------
# Installed package checks
# Each function returns a list of PackageResult for packages whose name or
# app_id matches the query, already installed on the system.
# ---------------------------------------------------------------------------

def installed_flatpak(query: str) -> list[PackageResult]:
    stdout, _, rc = run(["flatpak", "list", "--columns=name,application,version,description"])
    if rc != 0 or not stdout.strip():
        return []
    # Reuse the same parser as search — consistent naming and deduplication
    return _parse_flatpak_lines(stdout.strip().splitlines(), query)


def installed_apt(query: str) -> list[PackageResult]:
    stdout, _, rc = run(["dpkg-query", "-W", "-f=${Package}\t${Version}\t${binary:Summary}\n"])
    if rc != 0 or not stdout.strip():
        return []
    results = []
    q = query.lower()
    for line in stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 1:
            continue
        name    = parts[0].strip()
        version = parts[1].strip() if len(parts) > 1 else ""
        desc    = parts[2].strip() if len(parts) > 2 else ""
        if q in name.lower():
            results.append(PackageResult(name, version, desc, "apt"))
    return results


def installed_dnf(query: str) -> list[PackageResult]:
    stdout, _, rc = run(["rpm", "-qa", "--queryformat", "%{NAME}\t%{VERSION}\t%{SUMMARY}\n"])
    if rc != 0 or not stdout.strip():
        return []
    results = []
    q = query.lower()
    for line in stdout.strip().splitlines():
        parts = line.split("\t")
        name    = parts[0].strip()
        version = parts[1].strip() if len(parts) > 1 else ""
        desc    = parts[2].strip() if len(parts) > 2 else ""
        if q in name.lower():
            results.append(PackageResult(name, version, desc, "dnf"))
    return results


def installed_rpm_ostree(query: str) -> list[PackageResult]:
    # Layered packages show up in rpm -qa just like regular dnf installs
    results = installed_dnf(query)
    for r in results:
        r.backend = "rpm-ostree"
    return results


def installed_pacman(query: str) -> list[PackageResult]:
    stdout, _, rc = run(["pacman", "-Q"])
    if rc != 0 or not stdout.strip():
        return []
    results = []
    q = query.lower()
    for line in stdout.strip().splitlines():
        parts = line.split()
        name    = parts[0].strip() if parts else ""
        version = parts[1].strip() if len(parts) > 1 else ""
        if q in name.lower():
            results.append(PackageResult(name, version, "", "pacman"))
    return results


def installed_brew(query: str) -> list[PackageResult]:
    stdout, _, rc = run(["brew", "list", "--versions"])
    if rc != 0 or not stdout.strip():
        return []
    results = []
    q = query.lower()
    for line in stdout.strip().splitlines():
        parts = line.split()
        name    = parts[0].strip() if parts else ""
        version = parts[1].strip() if len(parts) > 1 else ""
        if q in name.lower():
            results.append(PackageResult(name, version, "", "brew"))
    return results


def installed_cargo(query: str) -> list[PackageResult]:
    stdout, _, rc = run(["cargo", "install", "--list"])
    if rc != 0 or not stdout.strip():
        return []
    results = []
    q = query.lower()
    for line in stdout.strip().splitlines():
        # Lines like: "package v1.2.3:"
        if line.startswith(" ") or not line.strip():
            continue
        parts = line.rstrip(":").split()
        name    = parts[0].strip() if parts else ""
        version = parts[1].strip().lstrip("v") if len(parts) > 1 else ""
        if q in name.lower():
            results.append(PackageResult(name, version, "", "cargo"))
    return results


def search_appman(query: str) -> list[PackageResult]:
    """
    Parse `appman search <query>` output.
    Output format:
      SEARCH RESULTS FOR "QUERY":
      ◆ name : description
      ◆ name : long description that may
        wrap onto the next line
    """
    stdout, _, rc = run(["appman", "search", query])
    if rc != 0 or not stdout.strip():
        return []

    results = []
    current_name = None
    current_desc = []

    def flush():
        if current_name:
            results.append(PackageResult(
                current_name, "", " ".join(current_desc).strip(), "appman"
            ))

    for line in stdout.splitlines():
        if "◆" in line:
            flush()
            current_desc = []
            # Format: "◆ name : description"
            body = line.split("◆", 1)[-1].strip()
            if " : " in body:
                name, _, desc = body.partition(" : ")
                current_name = name.strip()
                current_desc = [desc.strip()]
            else:
                current_name = body.strip()
        elif current_name and line.startswith("  "):
            # Continuation line — appman wraps long descriptions with 2-space indent
            current_desc.append(line.strip())

    flush()
    return results


def _appman_install_dir() -> Path | None:
    """
    Read the user-chosen install directory from appman's config file.
    Returns None if the config doesn't exist or the path isn't set.
    """
    cfg = Path.home() / ".config" / "appman" / "appman-config"
    if not cfg.exists():
        return None
    for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        # Config line is just a bare path, e.g. /home/user/Applications
        if line and not line.startswith("#") and "/" in line:
            p = Path(line)
            if p.is_dir():
                return p
    return None


def installed_appman(query: str) -> list[PackageResult]:
    """
    Detect installed appman apps by scanning the install directory.
    Each app managed by appman lives in its own subdirectory and always
    contains a 'remove' script — that's the reliable indicator.
    Falls back to `appman -f` output if the install dir can't be determined.
    """
    q = query.lower()
    results = []

    install_dir = _appman_install_dir()
    if install_dir:
        try:
            for entry in install_dir.iterdir():
                if not entry.is_dir():
                    continue
                # Presence of a 'remove' script = managed by appman
                if not (entry / "remove").exists():
                    continue
                name = entry.name
                if q in name.lower():
                    results.append(PackageResult(name, "", "", "appman"))
            return results
        except PermissionError:
            pass  # fall through to appman -f

    # Fallback: parse `appman -f` output
    # Columns are space-separated: name  version  size  type
    # Header and separator lines start with non-alphanumeric chars.
    stdout, _, rc = run(["appman", "-f"])
    if rc != 0 or not stdout.strip():
        return results
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Skip headers, separators, and table borders (|, -, =, #)
        if line[0] in ("-", "=", "|", "#", "+"):
            continue
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        # Skip obvious header rows
        if name.lower() in ("name", "application", "app"):
            continue
        version = parts[1] if len(parts) > 1 else ""
        if q in name.lower():
            results.append(PackageResult(name, version, "", "appman"))
    return results


INSTALLED_FUNCTIONS = {
    "flatpak":    installed_flatpak,
    "apt":        installed_apt,
    "dnf":        installed_dnf,
    "rpm-ostree": installed_rpm_ostree,
    "pacman":     installed_pacman,
    "brew":       installed_brew,
    "cargo":      installed_cargo,
    "appman":     installed_appman,
}


def check_installed(query: str, backends: list[str]) -> list[PackageResult]:
    """Return all installed packages matching query across active backends."""
    found = []
    for backend in backends:
        fn = INSTALLED_FUNCTIONS.get(backend)
        if fn:
            found.extend(fn(query))
    return found


# Map backend names to their search functions
SEARCH_FUNCTIONS = {
    "flatpak":    search_flatpak,
    "brew":       search_brew,
    "cargo":      search_cargo,
    "apt":        search_apt,
    "dnf":        search_dnf,
    "pacman":     search_pacman,
    "rpm-ostree": search_rpm_ostree,
    "appman":     search_appman,
}


# ---------------------------------------------------------------------------
# Result merging
# ---------------------------------------------------------------------------

def merge_results(
    all_results: list[PackageResult],
    query: str = "",
    name_filter: bool = True,
) -> list[MergedResult]:
    """
    Deduplicate by package name (case-insensitive).
    The first occurrence (highest-priority backend) provides version/description.
    Subsequent occurrences just add their backend to the list.
    """
    """
    Deduplicate by package name (case-insensitive).
    The first occurrence (highest-priority backend) provides version/description.
    Subsequent occurrences just add their backend to the list.

    If name_filter is True, only packages whose name contains the query
    string are kept. Description-only matches are dropped to keep results focused.
    """
    seen: dict[str, MergedResult] = {}
    query_lower = query.lower()

    for pkg in all_results:
        # Apply name filter before deduplication
        if name_filter and query_lower and not pkg.matches_query(query_lower):
            continue

        key = pkg.name.lower()
        # Always store the original identifier for this backend — use app_id
        # if the backend provided one (e.g. flatpak), otherwise use the raw
        # package name before deduplication may have changed it.
        backend_id = pkg.app_id if pkg.app_id else pkg.name

        if key not in seen:
            seen[key] = MergedResult(
                name         = pkg.name,
                version      = pkg.version,
                description  = pkg.description,
                backends     = [pkg.backend],
                app_ids      = {pkg.backend: backend_id},
                descriptions = {pkg.backend: pkg.description},
            )
        else:
            if pkg.backend not in seen[key].backends:
                seen[key].backends.append(pkg.backend)
            if pkg.backend not in seen[key].app_ids:
                seen[key].app_ids[pkg.backend] = backend_id
            if pkg.backend not in seen[key].descriptions:
                seen[key].descriptions[pkg.backend] = pkg.description

    return list(seen.values())


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

USE_COLOR = True   # overridden by config at startup

def colorize(text: str, code: str) -> str:
    if USE_COLOR:
        return f"\033[{code}m{text}\033[0m"
    return text

def bold(text):  return colorize(text, "1")
def cyan(text):  return colorize(text, "36")
def yellow(text):return colorize(text, "33")
def dim(text):   return colorize(text, "2")


def print_results(merged: list[MergedResult]) -> None:
    if not merged:
        print("No results found.")
        return

    print(f"\n  {'#':<4} {'Package':<35} {'Version':<18} {'Source':<12} Description")
    print("  " + "-" * 100)

    for i, pkg in enumerate(merged):
        index   = bold(f"[{i + 1}]")
        name    = cyan(f"{pkg.name:<35}")
        version = dim(f"{pkg.version:<18}") if pkg.version else f"{'':18}"
        source  = yellow(f"{pkg.source_tag():<12}")
        desc    = pkg.description[:55] + "…" if len(pkg.description) > 55 else pkg.description
        print(f"  {index:<6} {name} {version} {source} {desc}")

    print()


# ---------------------------------------------------------------------------
# Backend info commands
# ---------------------------------------------------------------------------

# Maps each backend to the command that shows detailed package information.
# {pkg} is replaced with the package name at call time.
INFO_COMMANDS: dict[str, list[str]] = {
    "flatpak":    ["flatpak", "remote-info", "flathub", "{id}"],
    "brew":       ["brew",    "info",        "{id}"],
    "cargo":      ["cargo",   "search",      "{id}"],   # cargo has no dedicated info
    "apt":        ["apt-cache", "show",      "{id}"],
    "dnf":        ["dnf",    "info",         "{id}"],
    "pacman":     ["pacman", "-Si",          "{id}"],
    "rpm-ostree": ["dnf",    "info",         "{id}"],
    "appman":     ["appman", "about",          "{id}"],
}

def get_install_command(pkg: MergedResult, backend: str) -> str:
    """Return the shell command string a user would type to install this package."""
    pkg_id = pkg.pkg_id(backend)
    cmds = {
        "flatpak":    f"flatpak install flathub {pkg_id}",
        "brew":       f"brew install {pkg_id}",
        "cargo":      f"cargo install {pkg_id}",
        "apt":        f"sudo apt install {pkg_id}",
        "dnf":        f"sudo dnf install {pkg_id}",
        "pacman":     f"sudo pacman -S {pkg_id}",
        "rpm-ostree": f"rpm-ostree install {pkg_id}",
        "appman":     f"appman install {pkg_id}",
    }
    return cmds.get(backend, f"{backend} install {pkg_id}")


def run_info(pkg: MergedResult, backend: str) -> None:
    """Run the backend's info command and stream output directly to the terminal."""
    template = INFO_COMMANDS.get(backend)
    if not template:
        print(f"  [smpt] No info command defined for {backend}.")
        return
    pkg_id = pkg.pkg_id(backend)
    cmd = [part.replace("{id}", pkg_id) for part in template]
    print(f"\n  {dim('$')} {' '.join(cmd)}\n")
    subprocess.run(cmd)

# Maps each backend to its install command tokens. {id} = package identifier.
INSTALL_COMMANDS: dict[str, list[str]] = {
    "flatpak":    ["flatpak", "install", "flathub",  "{id}"],
    "brew":       ["brew",    "install",             "{id}"],
    "cargo":      ["cargo",   "install",             "{id}"],
    "apt":        ["sudo",    "apt",     "install",  "{id}"],
    "dnf":        ["sudo",    "dnf",     "install",  "{id}"],
    "pacman":     ["sudo",    "pacman",  "-S",       "{id}"],
    "rpm-ostree": ["rpm-ostree", "install",          "{id}"],
    "appman":     ["appman",     "install",          "{id}"],
}

# Flags that suppress interactive prompts for each backend (used with -y)
YES_FLAGS: dict[str, list[str]] = {
    "flatpak":    ["--assumeyes"],
    "brew":       [],
    "cargo":      [],
    "apt":        ["-y"],
    "dnf":        ["-y"],
    "pacman":     ["--noconfirm"],
    "rpm-ostree": ["-y"],
    "appman":     [],   # appman has no non-interactive flag; prompts are minimal
}


def build_install_cmd(backend: str, pkg_ids: list[str],
                      yes: bool = False) -> list[str]:
    """
    Build an install command for one backend and one or more package IDs.
    Multiple IDs are appended after the base command (most package managers
    accept this natively: `apt install pkg1 pkg2 ...`).
    The special case is flatpak which needs `flathub` before the IDs.
    """
    template = INSTALL_COMMANDS.get(backend)
    if not template:
        return []

    # Build base command by replacing {id} with the first id, then append rest
    base = [part.replace("{id}", pkg_ids[0]) for part in template]
    if len(pkg_ids) > 1:
        base = base + pkg_ids[1:]

    if yes:
        flags = YES_FLAGS.get(backend, [])
        # Insert yes-flags right after the subcommand (before package ids)
        # Find where the package ids start (after the last non-id flag)
        # Simple heuristic: flags go before the first pkg_id in the command
        first_id_idx = next(
            (i for i, part in enumerate(base) if part == pkg_ids[0]), len(base) - 1
        )
        base = base[:first_id_idx] + flags + base[first_id_idx:]

    return base


def run_install(pkg: MergedResult, backend: str,
                dry_run: bool = False, yes: bool = False) -> int:
    """
    Run the install command for a single package.
    Returns the subprocess returncode (0 = success).
    """
    pkg_id = pkg.pkg_id(backend)
    cmd = build_install_cmd(backend, [pkg_id], yes=yes)
    if not cmd:
        print(f"  [smpt] No install command defined for {backend}.")
        return 1

    print(f"\n  {dim('$')} {' '.join(cmd)}")

    if dry_run:
        print(f"  {yellow('[dry-run] Command not executed.')}")
        return 0

    print()
    result = subprocess.run(cmd)
    return result.returncode


def run_batch(resolved: list[ResolvedPackage],
              dry_run: bool = False, yes: bool = False) -> None:
    """
    Group resolved packages by backend, build one command per backend,
    print the full plan, confirm with the user, then execute.
    """
    from collections import defaultdict

    # Group by backend, preserving priority order
    groups: dict[str, list[ResolvedPackage]] = defaultdict(list)
    for r in resolved:
        groups[r.backend].append(r)

    # Build commands
    commands: list[tuple[str, list[str]]] = []   # (backend, cmd)
    for backend, items in groups.items():
        pkg_ids = [r.install_id() for r in items]
        cmd = build_install_cmd(backend, pkg_ids, yes=yes)
        if cmd:
            commands.append((backend, cmd))

    if not commands:
        print("[smpt] Nothing to install.")
        return

    # Show the plan
    print(f"\n  {bold('Install plan:')}")
    print()
    for backend, items in groups.items():
        names = ', '.join(cyan(r.pkg.name) for r in items)
        print(f"  {yellow(backend)}: {names}")
    print()
    print(f"  {bold('Commands to run:')}")
    for _, cmd in commands:
        print(f"    {dim('$')} {' '.join(cmd)}")
    print()

    # Confirmation (skipped with -y or dry-run)
    if not yes and not dry_run:
        try:
            ans = input("  Proceed? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return
        if ans and ans not in ("y", "yes"):
            print("Cancelled.")
            return

    if dry_run:
        print(f"  {yellow('[dry-run] Commands not executed.')}")
        return

    # Execute each command
    print()
    for backend, cmd in commands:
        print(f"  {dim('$')} {' '.join(cmd)}\n")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"\n  {yellow(f'Warning: {backend} returned exit code {result.returncode}')}")
    print()


def is_exact_match(pkg: MergedResult, query: str) -> bool:
    """
    Return True if the package name or any app_id last segment exactly matches
    the query (case-insensitive). Used by install to default to the best result.
    """
    q = query.lower()
    if pkg.name.lower() == q:
        return True
    for app_id in pkg.app_ids.values():
        if app_id.rsplit(".", 1)[-1].lower() == q:
            return True
    return False


# ---------------------------------------------------------------------------
# Interactive selection and detail view
# ---------------------------------------------------------------------------

def _read_key() -> str:
    """
    Read a single keypress from stdin in raw mode and return a string token:
      "up", "down", "enter", "quit", or the raw character for anything else.
    Works on Linux and macOS. On Windows (no termios) falls back to input().
    """
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":              # escape sequence — read 2 more bytes
            ch2 = sys.stdin.read(1)
            ch3 = sys.stdin.read(1)
            if ch2 == "[":
                if ch3 == "A": return "up"
                if ch3 == "B": return "down"
                if ch3 == "C": return "right"
                if ch3 == "D": return "left"
        if ch in ("\r", "\n"):       return "enter"
        if ch in ("q", "Q", "\x03"): return "quit"   # q or Ctrl-C
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _term_cols() -> int:
    """Return terminal width, defaulting to 100 if unavailable."""
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 100


def _format_row(pkg: MergedResult, i: int, cursor: int) -> str:
    """
    Build one result row, truncating name and description so the whole
    line fits within the current terminal width. ANSI codes don't count
    toward visible width, so we measure plain text separately.
    """
    cols = _term_cols()

    arrow   = bold("▶") if i == cursor else " "
    index   = f"[{i + 1}]"

    # Fixed-width columns (visible chars): arrow=1, gap=1, index≤5, gap=1
    # version=18, source=12, separating spaces — total fixed overhead ≈ 46
    FIXED = 46
    remaining = max(cols - FIXED, 20)   # always show at least 20 chars

    # Split remaining space: name gets up to 30, description gets the rest
    name_width = min(30, remaining // 2)
    desc_width = max(10, remaining - name_width - 2)

    plain_name = pkg.name[:name_width]
    name       = cyan(f"{plain_name:<{name_width}}")
    version    = dim(f"{pkg.version:<18}") if pkg.version else f"{'':<18}"
    source     = yellow(f"{pkg.source_tag():<12}")
    plain_desc = pkg.description
    desc       = (plain_desc[:desc_width] + "…") if len(plain_desc) > desc_width else plain_desc

    return f"  {arrow} {index:<5} {name} {version} {source} {desc}"


def _term_rows() -> int:
    """Return terminal height, defaulting to 24 if unavailable."""
    try:
        return os.get_terminal_size().lines
    except OSError:
        return 24


# Lines consumed by header (blank+header+separator) and footer (blank+hint).
# _FOOTER_LINES is 3 because print(f"\n  hint") emits: blank line, hint line,
# then print() itself adds a newline — leaving the cursor 3 lines below the
# last item, not 2.
_HEADER_LINES = 3
_FOOTER_LINES = 3


def _visible_window(n: int) -> int:
    """Number of list items that fit on screen given current terminal height."""
    return max(3, _term_rows() - _HEADER_LINES - _FOOTER_LINES - 1)


def _draw_window(merged: list[MergedResult], cursor: int,
                 scroll_top: int, win_size: int,
                 header: bool = False) -> int:
    """
    Draw the visible slice [scroll_top : scroll_top+win_size] of merged.
    Returns the exact number of lines printed.
    """
    lines_printed = 0
    visible = merged[scroll_top: scroll_top + win_size]
    n       = len(merged)

    if header:
        cols = _term_cols()
        print(f"\n  {'':<4} {'Package':<30} {'Version':<18} {'Source':<12} Description")
        print("  " + "-" * min(cols - 2, 120))
        lines_printed += 3

    for rel, pkg in enumerate(visible):
        abs_i = scroll_top + rel
        print(_format_row(pkg, abs_i, cursor))
        lines_printed += 1

    # Scroll indicators in the hint line
    up_ind   = "▲ " if scroll_top > 0          else "  "
    down_ind = " ▼" if scroll_top + win_size < n else "  "
    print(f"\n  {dim(up_ind + '↑↓ · Enter · Q to quit' + down_ind)}")
    lines_printed += 3   # \n blank line + hint line + print()'s trailing newline

    return lines_printed


def _move_cursor_in_window(merged: list[MergedResult],
                           old: int, new: int,
                           scroll_top: int, drawn_lines: int) -> None:
    """
    Repaint only the two changed rows within the current window using
    relative positioning based on lines actually drawn — not the theoretical
    window capacity. This keeps the cursor correct when the list is shorter
    than the terminal height.

    After _draw_window the cursor sits drawn_lines below the first item row.
    The first item is at offset (drawn_lines - _HEADER_LINES) from the cursor,
    so item at abs index i is at offset:
      drawn_lines - _HEADER_LINES - (i - scroll_top) - 1
    plus _FOOTER_LINES to account for the footer below the last item.
    """
    out = sys.stdout

    # Total lines from cursor back up to item i (0-based within visible slice)
    def offset_for(abs_i: int) -> int:
        rel = abs_i - scroll_top          # position within visible slice
        visible_count = drawn_lines - _HEADER_LINES - _FOOTER_LINES
        # Distance from cursor: footer lines + items below this one
        return _FOOTER_LINES + (visible_count - 1 - rel)

    def move_to(abs_i: int) -> None:
        out.write(f"\x1b[{offset_for(abs_i)}A")

    def move_down(abs_i: int) -> None:
        out.write(f"\x1b[{offset_for(abs_i)}B")

    # Deselect old
    move_to(old)
    out.write("\r")
    out.write(_format_row(merged[old], old, -1))
    move_down(old)

    # Select new
    move_to(new)
    out.write("\r")
    out.write(_format_row(merged[new], new, new))
    move_down(new)

    out.flush()


def _erase_lines(n: int) -> None:
    """Move the terminal cursor up n lines and clear from there down."""
    sys.stdout.write(f"\x1b[{n}A\x1b[0J")
    sys.stdout.flush()


def prompt_selection(merged: list[MergedResult],
                     default: int = 0) -> MergedResult | None:
    """
    Scrollable arrow-key driven package selector.
    Shows a window of items that fits the terminal height; scrolls as needed.
    Falls back to plain input() if stdin is not a real terminal.
    """
    if not sys.stdin.isatty():
        try:
            raw = input(f"Select [1-{len(merged)}] (default: {default+1}, q to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if raw.lower() in ("q", "quit", "") or raw == "":
            return merged[default]
        try:
            choice = int(raw)
            if 1 <= choice <= len(merged):
                return merged[choice - 1]
        except ValueError:
            pass
        return None

    n          = len(merged)
    cursor     = default
    win_size   = _visible_window(n)
    # Start the window so the default item is visible, centred if possible
    scroll_top = max(0, min(cursor - win_size // 2, n - win_size))

    drawn_lines = _draw_window(merged, cursor, scroll_top, win_size, header=True)

    while True:
        key = _read_key()

        if key == "quit":
            _erase_lines(drawn_lines)
            print()
            return None

        elif key == "enter":
            _erase_lines(drawn_lines)
            return merged[cursor]

        elif key in ("up", "down"):
            old    = cursor
            cursor = (cursor - 1 if key == "up" else cursor + 1) % n

            # Recalculate window — scroll if cursor left the visible range
            new_scroll = scroll_top
            if cursor < scroll_top:
                new_scroll = cursor
            elif cursor >= scroll_top + win_size:
                new_scroll = cursor - win_size + 1
            # Wrap-around: if cursor jumped to the other end, re-centre
            if abs(cursor - old) > 1:
                new_scroll = max(0, min(cursor - win_size // 2, n - win_size))

            if new_scroll != scroll_top:
                # Window moved — full redraw of the visible slice
                scroll_top  = new_scroll
                win_size    = _visible_window(n)   # recheck in case terminal resized
                _erase_lines(drawn_lines)
                drawn_lines = _draw_window(merged, cursor, scroll_top, win_size, header=True)
            else:
                # Cursor stayed in window — targeted two-row repaint
                _move_cursor_in_window(merged, old, cursor, scroll_top, drawn_lines)

        else:
            # Number shortcut: "3" jumps to item 3
            try:
                idx = int(key) - 1
                if 0 <= idx < n:
                    old        = cursor
                    cursor     = idx
                    new_scroll = max(0, min(cursor - win_size // 2, n - win_size))
                    if new_scroll != scroll_top:
                        scroll_top  = new_scroll
                        _erase_lines(drawn_lines)
                        drawn_lines = _draw_window(
                            merged, cursor, scroll_top, win_size, header=True
                        )
                    else:
                        _move_cursor_in_window(merged, old, cursor, scroll_top, drawn_lines)
            except ValueError:
                pass



def prompt_backend(result: MergedResult, action: str = "use") -> str | None:
    """
    If a result comes from multiple backends, ask the user which one to use.
    `action` is shown in the prompt (e.g. "install", "get info for").
    Returns the chosen backend name, or None if cancelled.
    """
    if len(result.backends) == 1:
        return result.backends[0]

    print(f"\n  '{result.name}' is available from multiple sources.")
    print(f"  Which would you like to {action}?")
    for i, backend in enumerate(result.backends):
        marker = bold(f"[{i + 1}]") if i > 0 else bold("[1]")
        default = dim("  (default)") if i == 0 else ""
        print(f"    {marker}  {backend}{default}")

    while True:
        try:
            raw = input(f"  Choose [1-{len(result.backends)}] (default: 1): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if raw == "":
            return result.backends[0]

        try:
            choice = int(raw)
            if 1 <= choice <= len(result.backends):
                return result.backends[choice - 1]
        except ValueError:
            pass

        print("  Invalid input.")


def wrap(text: str, width: int = 66, indent: str = "  ") -> str:
    """Word-wrap text to width, returning an indented multiline string."""
    lines = []
    while len(text) > width:
        split = text[:width].rfind(" ")
        split = split if split > 0 else width
        lines.append(indent + text[:split])
        text = text[split:].lstrip()
    if text:
        lines.append(indent + text)
    return "\n".join(lines)


def print_detail(pkg: MergedResult, backend: str) -> None:
    """Print the full package detail view, scoped to a single backend."""
    print()
    print("  " + "─" * 68)
    print(f"  {bold(pkg.name)}  {dim(pkg.version)}")
    print(f"  Source: {yellow(backend)}")
    print()
    print(wrap(pkg.desc_for(backend)))
    print()
    print(f"  {dim('Install command:')}")
    print(f"    {cyan(get_install_command(pkg, backend))}")
    print("  " + "─" * 68)
    print()


# Detail menu options: (label shown, hotkey, action id)
_DETAIL_OPTIONS = [
    ("Back",    "b", "back"),
    ("iNfo",    "n", "info"),
    ("Install", "i", "install"),
    ("Quit",    "q", "quit"),
]


def _render_detail_menu(cursor: int) -> str:
    """Return the menu bar string (without newline) for the given cursor."""
    parts = []
    for i, (label, hotkey, _) in enumerate(_DETAIL_OPTIONS):
        hi = label.index(next(c for c in label if c.lower() == hotkey))
        decorated = label[:hi] + bold(label[hi]) + label[hi+1:]
        if i == cursor:
            parts.append(f"\033[7m [{decorated}] \033[0m")
        else:
            parts.append(f"  {decorated}  ")
    return "  " + "  ".join(parts)


def _draw_detail_menu(cursor: int) -> None:
    """Print the full menu bar + hint line (initial draw only)."""
    print(_render_detail_menu(cursor))
    print()
    print(f"  {dim('←→ to move · Enter to select · or press hotkey')}")


def _update_detail_menu(cursor: int) -> None:
    """
    Repaint only the menu bar line in-place — no flicker.
    After _draw_detail_menu, print() has advanced past the hint line,
    so the cursor sits one line below it. Layout from cursor position:
      menu bar  = 3 lines up
      blank     = 2 lines up
      hint      = 1 line up
      [cursor]  = 0 (current)
    """
    out = sys.stdout
    out.write("\x1b[3A")          # move up 3 lines to the menu bar
    out.write("\r\x1b[2K")        # go to start of line, clear it
    out.write(_render_detail_menu(cursor))
    out.write("\x1b[3B\r")        # move back down 3 lines
    out.flush()


def prompt_backend_arrow(result: MergedResult, action: str = "use",
                          default: str | None = None) -> str | None:
    """
    Arrow-key driven backend picker for multi-source packages.
    default: backend name to pre-select (defaults to first in list).
    Falls back to plain input if stdin is not a tty.
    """
    if len(result.backends) == 1:
        return result.backends[0]

    # Resolve default index — fall back to 0 if named backend not in list
    default_idx = result.backends.index(default) if default in result.backends else 0

    options = result.backends
    n = len(options)

    print(f"\n  {bold(result.name)} is available from multiple sources.")
    print(f"  Choose which to {action}:\n")

    if not sys.stdin.isatty():
        for i, b in enumerate(options):
            tag = dim("  (default)") if i == default_idx else ""
            print(f"    [{i+1}]  {b}{tag}")
        try:
            raw = input(f"  Choose [1-{n}] (default: {default_idx+1}): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw == "":
            return options[default_idx]
        try:
            c = int(raw)
            if 1 <= c <= n:
                return options[c - 1]
        except ValueError:
            pass
        return None

    cursor = default_idx

    def draw(cur):
        for i, b in enumerate(options):
            arrow = bold("▶") if i == cur else " "
            tag   = dim("(default)") if i == default_idx else ""
            print(f"  {arrow}  {b}  {tag}")
        print(f"\n  {dim('↑↓ · Enter · Q to cancel')}")

    draw(cursor)

    while True:
        key = _read_key()
        if key in ("up",):
            cursor = (cursor - 1) % n
        elif key in ("down",):
            cursor = (cursor + 1) % n
        elif key == "enter":
            _erase_lines(n + 2)
            return options[cursor]
        elif key == "quit":
            _erase_lines(n + 2)
            return None
        else:
            try:
                idx = int(key) - 1
                if 0 <= idx < n:
                    _erase_lines(n + 2)
                    return options[idx]
            except ValueError:
                pass

        _erase_lines(n + 2)
        draw(cursor)


def detail_menu(pkg: MergedResult, backend: str, config: configparser.ConfigParser,
                default_action: str = "back",
                dry_run: bool = False, yes: bool = False) -> bool:
    """
    Show the package detail view for a specific backend, then handle B/N/I/Q.

    default_action: hotkey of the option to highlight by default ("b" or "i").
    dry_run / yes: passed through to run_install if the user chooses Install.

    Returns True  → caller should reprint results and let user pick again.
    Returns False → program should exit (install ran, or user quit).
    """
    MENU_LINES = 3   # _draw_detail_menu prints: menu bar + hint line + blank

    use_arrow = sys.stdin.isatty()
    # Find cursor start position from default_action hotkey
    cursor = next(
        (i for i, (_, hk, _) in enumerate(_DETAIL_OPTIONS) if hk == default_action),
        0
    )

    # Print the static detail view once — only the menu bar redraws below
    print_detail(pkg, backend)

    if not use_arrow:
        print("  [B]ack  i[N]fo  [I]nstall  [Q]uit\n")
        try:
            key = input("  Choice (default: B): ").strip().lower() or "b"
        except (EOFError, KeyboardInterrupt):
            key = "q"
    else:
        _draw_detail_menu(cursor)
        key = None   # enter the navigation loop below

    while True:

        # ── Arrow-key navigation loop (tty only) ──────────────────────────
        if use_arrow and key is None:
            key = _read_key()

        # Left / Right: move cursor — repaint only the menu bar, no flicker
        if use_arrow and key in ("left", "right"):
            step = -1 if key == "left" else 1
            cursor = (cursor + step) % len(_DETAIL_OPTIONS)
            _update_detail_menu(cursor)
            key = None
            continue

        # Enter: resolve to the hotkey of the highlighted option
        if use_arrow and key == "enter":
            key = _DETAIL_OPTIONS[cursor][1]

        # ── Actions ───────────────────────────────────────────────────────
        if key in ("b", "back", ""):
            return True

        elif key in ("n", "info"):
            # Erase menu bar before backend output scrolls in, then reprint
            _erase_lines(MENU_LINES)
            run_info(pkg, backend)
            print_detail(pkg, backend)
            if use_arrow:
                _draw_detail_menu(cursor)
                key = None
            else:
                print("  [B]ack  i[N]fo  [I]nstall  [Q]uit\n")
                try:
                    key = input("  Choice (default: B): ").strip().lower() or "b"
                except (EOFError, KeyboardInterrupt):
                    return False
            continue

        elif key in ("i", "install"):
            _erase_lines(MENU_LINES)
            if config.getboolean(backend, "warn_on_install", fallback=False):
                print(f"\n  {yellow('Warning:')} Installing via {bold(backend)} has side effects.")
                if backend == "rpm-ostree":
                    print("  Packages are layered onto your immutable image and require a reboot.")
                try:
                    confirm = input("  Continue? [y/N]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    if use_arrow:
                        _draw_detail_menu(cursor)
                        key = None
                    continue
                if confirm not in ("y", "yes"):
                    if use_arrow:
                        _draw_detail_menu(cursor)
                        key = None
                    continue

            rc = run_install(pkg, backend, dry_run=dry_run, yes=yes)
            if rc == 0:
                print(f"\n  {bold(pkg.name)} installed successfully via {yellow(backend)}.\n")
            else:
                print(f"\n  {yellow('Warning:')} install exited with code {rc}.\n")
            return False

        elif key in ("q", "quit", "exit", "\x03"):
            return False

        else:
            # Unknown key — just loop and read the next one
            key = None if use_arrow else "b"


# ---------------------------------------------------------------------------
# Command: search
# ---------------------------------------------------------------------------

def cmd_search(query: str, config: configparser.ConfigParser,
               dry_run: bool = False, yes: bool = False,
               search_desc: bool = False) -> None:
    backends = get_enabled_backends(config)

    if not backends:
        print("[smpt] No package backends are available on this system.")
        sys.exit(1)

    print(f"[smpt] Searching for '{query}' across: {', '.join(backends)}")

    # --description overrides the config name_filter for this run.
    # Queries with spaces are treated as phrase searches — disable name filter.
    name_filter = False if (search_desc or " " in query) else config.getboolean("smpt", "name_filter", fallback=True)

    all_results: list[PackageResult] = []
    for backend in backends:
        fn = SEARCH_FUNCTIONS.get(backend)
        if fn:
            found = fn(query)
            # Apply name filter per-backend so the count shown to the user
            # reflects results that will actually appear, not raw backend output
            if name_filter and query:
                q = query.lower()
                found = [p for p in found if p.matches_query(q)]
            print(f"  {backend}: {len(found)} result(s)")
            all_results.extend(found)

    # name_filter already applied above, pass False to avoid double-filtering
    merged = merge_results(all_results, query=query, name_filter=False)

    if not merged:
        # Remote search returned nothing — check if already installed
        installed = check_installed(query, backends)
        if installed:
            print(f"\n  {yellow('Note:')} No new packages found, but '{query}' is already installed:")
            for pkg in installed:
                print(f"    {cyan(pkg.name):<38} {dim(pkg.version):<18} via {yellow(pkg.backend)}")
            print()
            try:
                ans = input("  Install from another source anyway? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if ans not in ("y", "yes"):
                print("Cancelled.")
                return
            print(f"\n[smpt] Re-searching without name filter...")
            merged = merge_results(all_results, query=query, name_filter=False)
        if not merged:
            print(f"\nNo packages found matching '{query}'.")
            return

    # Note any already-installed matches before showing the table
    else:
        installed = check_installed(query, backends)
        if installed:
            print()
            for pkg in installed:
                print(f"  {yellow("⬤")} Already installed: {cyan(pkg.name)} {dim(pkg.version)} via {yellow(pkg.backend)}")

    # Main results loop — stays here until user installs or quits
    while True:
        # prompt_selection draws the results table itself (arrow-key UI)
        chosen_pkg = prompt_selection(merged)
        if chosen_pkg is None:
            print("Cancelled.")
            return

        # Resolve backend before showing detail — single-source skips the prompt
        chosen_backend = prompt_backend_arrow(chosen_pkg, action="view details for")
        if chosen_backend is None:
            # User cancelled the backend picker — go back to results
            continue

        # Show detail view scoped to the chosen backend
        # Returns True = back to results, False = exit
        go_back = detail_menu(chosen_pkg, chosen_backend, config,
                              dry_run=dry_run, yes=yes)
        if not go_back:
            return


def resolve_one(query: str, backends: list[str], config: configparser.ConfigParser,
                search_desc: bool = False,
                yes: bool = False) -> ResolvedPackage | None:
    """
    Resolve a single query to a ResolvedPackage using the interactive UI.
    Returns None if the user cancels or no match is found and we should skip.
    In -y mode, returns the best exact match without interaction.
    """
    print(f"\n[smpt] Searching for '{query}' across: {', '.join(backends)}")

    name_filter = (
        False if (search_desc or " " in query)
        else config.getboolean("smpt", "name_filter", fallback=True)
    )

    all_results: list[PackageResult] = []
    for backend in backends:
        fn = SEARCH_FUNCTIONS.get(backend)
        if fn:
            found = fn(query)
            if name_filter and query:
                q = query.lower()
                found = [p for p in found if p.matches_query(q)]
            print(f"  {backend}: {len(found)} result(s)")
            all_results.extend(found)

    merged = merge_results(all_results, query=query, name_filter=False)

    if not merged:
        print(f"  {yellow('No packages found matching')} '{query}'. Skipping.")
        return None

    exact_idx = next(
        (i for i, pkg in enumerate(merged) if is_exact_match(pkg, query)), None
    )

    # ── -y mode ───────────────────────────────────────────────────────────
    if yes:
        if exact_idx is None:
            print(f"  {yellow('No exact match for')} '{query}'. Skipping "
                  f"(run without -y to choose interactively).")
            return None
        pkg     = merged[exact_idx]
        backend = next((b for b in backends if b in pkg.backends), pkg.backends[0])
        return ResolvedPackage(query=query, pkg=pkg, backend=backend)

    # ── Interactive mode ───────────────────────────────────────────────────
    installed = check_installed(query, backends)
    if installed:
        print()
        for p in installed:
            print(f"  {yellow('⬤')} Already installed: "
                  f"{cyan(p.name)} {dim(p.version)} via {yellow(p.backend)}")

    while True:
        chosen_pkg = prompt_selection(merged, default=exact_idx or 0)
        if chosen_pkg is None:
            return None

        best_backend = next(
            (b for b in backends if b in chosen_pkg.backends),
            chosen_pkg.backends[0]
        )
        chosen_backend = prompt_backend_arrow(
            chosen_pkg, action="install from", default=best_backend,
        )
        if chosen_backend is None:
            continue

        # Show detail with Install as default action; Back loops back to list
        go_back = detail_menu(
            chosen_pkg, chosen_backend, config,
            default_action="i", dry_run=False, yes=False,
        )
        if not go_back:
            # User confirmed install from detail menu — return resolution
            return ResolvedPackage(
                query=query, pkg=chosen_pkg, backend=chosen_backend
            )
        # go_back=True means they pressed Back — loop and show list again


def cmd_install(query: str, config: configparser.ConfigParser,
                dry_run: bool = False, yes: bool = False,
                search_desc: bool = False) -> None:
    """
    Install one or more packages.

    Multiple packages are separated by commas or can be passed as space-
    separated words when quoted: `smpt install firefox vlc` installs both.
    Each package is resolved individually, then all are batched together
    into the minimum number of commands (one per backend).
    """
    backends = get_enabled_backends(config)
    if not backends:
        print("[smpt] No package backends are available on this system.")
        sys.exit(1)

    # Split query into individual package names.
    # Commas are an explicit separator; otherwise each word is its own package.
    if "," in query:
        queries = [q.strip() for q in query.split(",") if q.strip()]
    else:
        queries = query.split()

    # ── Already-installed check for -y multi-package ──────────────────────
    if yes and len(queries) > 1:
        installed_names: set[str] = set()
        for q in queries:
            found = check_installed(q, backends)
            if any(is_exact_match(
                MergedResult(p.name, p.version, p.description, [p.backend]), q
            ) for p in found):
                print(f"[smpt] '{q}' is already installed. Skipping.")
                installed_names.add(q)
        queries = [q for q in queries if q not in installed_names]
        if not queries:
            return

    # ── Resolve each package ──────────────────────────────────────────────
    # Multi-package installs always auto-select the best exact match (like -y)
    # so the user isn't walked through a separate UI for every package.
    # The confirmation step in run_batch is where they get to review and approve.
    auto = yes or len(queries) > 1

    resolved: list[ResolvedPackage] = []
    for q in queries:
        # Skip if already installed from any source (multi-package only)
        if len(queries) > 1:
            already = check_installed(q, backends)
            exact_installed = [p for p in already if q.lower() in p.name.lower()]
            if exact_installed:
                src = ', '.join(yellow(p.backend) for p in exact_installed)
                print(f"  {cyan(q)} is already installed via {src}. Skipping.")
                continue

        r = resolve_one(q, backends, config,
                        search_desc=search_desc, yes=auto)
        if r:
            resolved.append(r)
        else:
            print(f"  {yellow('Skipping')} '{q}'.")

    if not resolved:
        print("\n[smpt] Nothing to install.")
        return

    # ── Single interactive package: install already ran inside detail_menu
    if len(queries) == 1 and not yes:
        return

    # ── Batch install ─────────────────────────────────────────────────────
    run_batch(resolved, dry_run=dry_run, yes=yes)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def get_deploy_path() -> Path:
    """Return the appropriate user bin path for the current OS."""
    if sys.platform == "win32":
        # Windows: use %USERPROFILE%\bin, creating it if needed
        return Path.home() / "bin"
    else:
        # Linux and macOS: ~/.local/bin is the XDG standard user bin
        return Path(os.environ.get("HOME", str(Path.home()))) / ".local" / "bin"


def cmd_deploy() -> None:
    """
    Copy this script to the user's bin directory as 'smpt' and mark it
    executable, so it can be run from anywhere as just 'smpt'.
    """
    src = Path(os.path.abspath(__file__))
    bin_dir = get_deploy_path()

    if sys.platform == "win32":
        dst = bin_dir / "smpt.py"
    else:
        dst = bin_dir / "smpt"

    # Create the bin directory if it doesn't exist
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[smpt] Could not create {bin_dir}: {e}")
        sys.exit(1)

    # Copy the script
    try:
        import shutil
        shutil.copy2(src, dst)
    except OSError as e:
        print(f"[smpt] Could not copy to {dst}: {e}")
        sys.exit(1)

    # Mark executable on non-Windows
    if sys.platform != "win32":
        try:
            dst.chmod(dst.stat().st_mode | 0o755)
        except OSError as e:
            print(f"[smpt] Copied but could not mark executable: {e}")
            sys.exit(1)

    print(f"[smpt] Deployed to {bold(str(dst))}")

    # Check if the bin directory is on $PATH and warn if not
    if sys.platform != "win32":
        path_dirs = os.environ.get("PATH", "").split(":")
        if str(bin_dir) not in path_dirs:
            print()
            print(f"  {yellow('Note:')} {bin_dir} is not on your $PATH.")
            print(f"  Add this to your shell config (~/.bashrc, ~/.zshrc, etc.):")
            print()
            print(f"    {cyan(f'export PATH="$HOME/.local/bin:$PATH"')}")
            print()
            print(f"  Then restart your shell or run:")
            print(f"    {cyan('source ~/.bashrc')}")
    else:
        path_dirs = os.environ.get("PATH", "").split(";")
        if str(bin_dir) not in path_dirs:
            print()
            print(f"  {yellow('Note:')} {bin_dir} is not on your PATH.")
            print(f"  Add it via System Properties → Environment Variables.")
            print(f"  Also ensure .py files are associated with Python.")



def cmd_deploy() -> None:
    """
    Copy this script to the user's local bin directory and mark it executable,
    so it can be called as just `smpt` from anywhere.

    Also copies a local config.ini if one sits next to the script, placing it
    in the OS config directory so it is picked up on future runs.
    """
    import shutil
    import stat

    src = Path(os.path.abspath(__file__))

    # ── Determine destination directory ───────────────────────────────────
    if sys.platform == "win32":
        # Best-effort on Windows: use a smpt folder in APPDATA and advise the user
        dest_dir = Path(os.environ.get("APPDATA", Path.home())) / "smpt" / "bin"
    else:
        # Linux and macOS: ~/.local/bin is the XDG standard for user binaries
        dest_dir = Path.home() / ".local" / "bin"

    dest = dest_dir / "smpt"

    # ── Check for existing install ─────────────────────────────────────────
    if dest.exists():
        print(f"  {yellow('smpt')} is already installed at {dim(str(dest))}")
        try:
            ans = input("  Overwrite? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return
        if ans not in ("y", "yes"):
            print("Cancelled.")
            return

    # ── Create destination directory if needed ────────────────────────────
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[smpt] Could not create {dest_dir}: {e}")
        sys.exit(1)

    # ── Copy script ───────────────────────────────────────────────────────
    try:
        shutil.copy2(src, dest)
    except OSError as e:
        print(f"[smpt] Could not copy script to {dest}: {e}")
        sys.exit(1)

    # ── Mark executable (no-op on Windows) ───────────────────────────────
    if sys.platform != "win32":
        try:
            current = dest.stat().st_mode
            dest.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError as e:
            print(f"[smpt] Warning: could not mark executable: {e}")

    print(f"  {cyan('smpt')} installed to {bold(str(dest))}")

    # ── Copy local config if one exists next to the source script ─────────
    local_cfg = src.parent / "config.ini"
    if local_cfg.exists():
        sys_cfg = get_system_config_path()
        if not sys_cfg.exists():
            try:
                sys_cfg.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_cfg, sys_cfg)
                print(f"  Config copied to {dim(str(sys_cfg))}")
            except OSError as e:
                print(f"  {yellow('Warning:')} could not copy config: {e}")
        else:
            print(f"  {dim('Existing config at')} {dim(str(sys_cfg))} {dim('left unchanged.')}")

    # ── PATH check ────────────────────────────────────────────────────────
    path_dirs = [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep)]
    if dest_dir not in path_dirs:
        print()
        print(f"  {yellow('Note:')} {dest_dir} is not on your PATH.")
        if sys.platform == "win32":
            print(f"  Add it to your PATH in System Properties → Environment Variables.")
        else:
            shell_rc = _detect_shell_rc()
            print(f"  Add this line to {dim(shell_rc)}:")
            print(f"    {cyan(f'export PATH="$HOME/.local/bin:$PATH"')} ")
            print(f"  Then restart your shell or run:")
            print(f"    {cyan(f'source {shell_rc}')}")
    else:
        print(f"  {dim('PATH is already set up correctly.')}")
        print(f"  You can now run {bold('smpt')} from anywhere.")

    if sys.platform == "win32":
        print(f"\n  {yellow('Windows note:')} you may need to call it as {bold('smpt.py')}")
        print(f"  or associate .py files with Python in your system settings.")


def _detect_shell_rc() -> str:
    """Best-guess the user's shell RC file for PATH export advice."""
    shell = os.environ.get("SHELL", "")
    home  = str(Path.home())
    if "zsh"  in shell: return f"{home}/.zshrc"
    if "fish" in shell: return f"{home}/.config/fish/config.fish"
    if "bash" in shell: return f"{home}/.bashrc"
    return f"{home}/.profile"


def print_help() -> None:
    """Print the smpt help text."""
    cfg_path, _ = find_config_path()
    print(f"""
{bold("smpt")} — Simple Multi-Package Tool

{bold("Usage:")}
  smpt <command> [flags] <package>

{bold("Commands:")}
  {cyan("search")}   <package>   Search for a package across all active backends
  {cyan("install")}  <package> [package2 ...]   Install one or more packages  (alias: {dim("in")})
  {cyan("deploy")}               Copy smpt to ~/.local/bin and mark it executable,
                        so it can be called as just {bold("smpt")} from anywhere.
  {cyan("help")}                 Show this help text  (alias: {dim("-h")}, {dim("--help")})
  {cyan("deploy")}               Copy smpt to ~/.local/bin/smpt and mark executable,
                        so it can be run from anywhere as just {dim("smpt")}

{bold("Flags:")} (work with both search and install)
  {cyan("--dry-run")}            Print the install command without running it
  {cyan("-y")}                   Non-interactive mode: install the best exact match
                        without any prompts. Skips if already installed.
                        Requires {cyan("--run-as-root")} when running as root.
  {cyan("--run-as-root")}        Suppress the root warning and allow -y as root.
                        Use with caution — most backends are user-level tools.
  {cyan("--description")}        Include description matches in search results,
                        not just package name matches  (alias: {dim("--desc")})

{bold("Root / sudo warning:")}
  smpt is designed for user-level package managers (flatpak, appman, brew,
  cargo). Running as root may install packages into the wrong locations or
  corrupt per-user state. A warning is always shown when running as root.
  Use {cyan("--run-as-root")} to acknowledge and suppress it.

{bold("Navigation:")}
  ↑ ↓          Move cursor in package list
  ← →          Move cursor in detail menu
  Enter        Select highlighted option
  1–9          Jump to item by number
  Q            Quit / cancel

{bold("Detail menu:")}
  {cyan("B")}  Back to results
  {cyan("N")}  iNfo — run the backend's info command for more details
  {cyan("I")}  Install the package
  {cyan("Q")}  Quit

{bold("Config file:")}
  {dim(str(cfg_path))}
  Edit to enable/disable backends or change their priority order.

{bold("Supported backends:")}
  flatpak · appman · brew · cargo · apt · dnf · pacman · rpm-ostree
  (only backends detected on your system are used)
""")


def _warn_root(run_as_root: bool) -> None:
    """
    Print a red warning if running as root/sudo.
    The border blinks; the message text is steady red for readability.
    """
    BLINK  = "\033[1;5;31m"   # bold + blink + red
    RED    = "\033[1;31m"     # bold + red (steady)
    RESET  = "\033[0m"
    border = BLINK + "!" * 60 + RESET
    print(border)
    print(RED + "  WARNING: smpt is running as root / sudo!"           + RESET)
    print(RED + "  User-level backends (flatpak, appman, brew, cargo)" + RESET)
    print(RED + "  may install to wrong locations or corrupt user state." + RESET)
    if not run_as_root:
        print(RED + "  Pass --run-as-root to acknowledge this risk."   + RESET)
    print(border)
    print()


def main():
    config = load_config()

    global USE_COLOR
    USE_COLOR = config.getboolean("smpt", "color", fallback=True)
    # Disable color if stdout is not a terminal (e.g. piped to a file)
    if not sys.stdout.isatty():
        USE_COLOR = False

    # ── Help (check before stripping flags so -h works anywhere) ─────────────
    if len(sys.argv) < 2 or sys.argv[1].lower() in ("-h", "--help", "help"):
        print_help()
        return

    command = sys.argv[1].lower()

    # Strip flags from remaining args before joining as query
    remaining    = sys.argv[2:]
    dry_run      = "--dry-run"      in remaining
    yes          = "-y"             in remaining
    run_as_root  = "--run-as-root"  in remaining
    search_desc  = "--description"  in remaining or "--desc" in remaining
    args = [a for a in remaining if a not in (
        "--dry-run", "-y", "-h", "--help",
        "--description", "--desc", "--run-as-root", "deploy",
    )]

    # ── Root check ────────────────────────────────────────────────────────────
    is_root = (os.geteuid() == 0) if hasattr(os, "geteuid") else False
    if is_root:
        _warn_root(run_as_root)
        if yes and not run_as_root:
            print("[smpt] Refusing to run -y as root without --run-as-root.")
            print("       Re-run with --run-as-root to confirm you know what you're doing.")
            sys.exit(1)

    if command == "deploy":
        cmd_deploy()
        return

    if command == "deploy":
        cmd_deploy()
        return

    if command == "search":
        if not args:
            print("Usage: smpt search [--dry-run] [-y] <package name>")
            sys.exit(1)
        query = " ".join(args)
        cmd_search(query, config, dry_run=dry_run, yes=yes, search_desc=search_desc)

    elif command in ("install", "in"):
        if not args:
            print("Usage: smpt install [--dry-run] [-y] <package> [package2 ...]")
            sys.exit(1)
        # Join args with comma so cmd_install splits them as individual packages
        query = ",".join(args)
        cmd_install(query, config, dry_run=dry_run, yes=yes, search_desc=search_desc)

    else:
        print(f"[smpt] Unknown command: '{command}'")
        print(f"       Run 'smpt --help' for usage.")
        sys.exit(1)

if __name__ == "__main__":
    main()
