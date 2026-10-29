"""
Package adapters of ops projects (docs/design/fleet-provisioning.md, chapters 4, 6 and 7, P2 and P2b).

Every manager (apt, dnf, zypper, apk, pacman, brew, npm, bun, pnpm, yarn, pipx, uv) is described by the same few functions and tables:

- argv: the install, remove, simulation and query commands as argument lists (never a shell string; the only shell
  text is the fixed preamble that picks the privilege prefix);
- parse_*: turn the recorded output of a simulation or query into items and installed versions;
- protected: the built-in protected list (matched by name, cannot be lifted);
- check: the validator of one action against the host's facts (manager present, sudo, names, versions, protection).

Items name what a simulation would change: "add:name=version", "upgrade:name=version", "remove:name=version"
(version omitted when unknown) and "dependent:name" (an installed brew formula that needs a package being removed).
ops_actions stores them with the proposal and compares them again right before the run (drift).

Nothing here talks to a host; ops_actions runs the commands. Tests use recorded outputs.
"""

import fnmatch
import json
import os
import re
import shlex
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import ops_facts

INSTALL, REMOVE = "ops:package_install", "ops:package_remove"
TOOLS = (INSTALL, REMOVE)
MANAGERS = ("apt", "dnf", "zypper", "apk", "pacman", "brew", "npm", "bun", "pnpm", "yarn", "pipx", "uv")
SYSTEM_MANAGERS = ("apt", "dnf", "zypper", "apk", "pacman")
# A preview that is only the request and the exact command, not a simulation: the step is one risk level higher.
LISTED_ONLY = ("bun", "pnpm", "yarn", "pipx", "uv")
SCRIPT_MANAGERS = ("npm", "bun", "pnpm", "yarn")
# pipx and uv install one tool per command, so an action names one package.
SINGLE_PACKAGE = ("pipx", "uv")
# Managers whose commands were not run on a real host yet. check() refuses them unless the operator lists them in
# GUARDIAN_PACKAGES_ALLOW_UNVERIFIED (comma separated), so a plan cannot depend on a guessed command.
UNVERIFIED = ("bun", "zypper", "apk", "pacman")
# Exit statuses of a real run that are a success besides 0 (zypper: 102 reboot needed, 103 restart of zypper needed).
OK_EXIT = {"zypper": (0, 102, 103)}
PROGRAMS = {"apt": "apt-get", "dnf": "dnf", "zypper": "zypper", "apk": "apk", "pacman": "pacman", "brew": "brew",
            "npm": "npm", "bun": "bun", "pnpm": "pnpm", "yarn": "yarn", "pipx": "pipx", "uv": "uv"}
MAX_PACKAGES = 20
MANY_REMOVALS = 5  # more removals than this raise the risk to high

NAME_PATTERNS = {
    **ops_facts.NAME_PATTERNS,
    "bun": ops_facts.NAME_PATTERNS["npm"], "pnpm": ops_facts.NAME_PATTERNS["npm"], "yarn": ops_facts.NAME_PATTERNS["npm"],
    "zypper": ops_facts.NAME_PATTERNS["dnf"],
    "apk": re.compile(r"^[a-z0-9][a-z0-9+._-]{0,99}$"),
    # Arch names and groups; no "/" (repository prefix) and no leading "-".
    "pacman": re.compile(r"^[a-z0-9@._+][a-z0-9@._+-]{0,99}$"),
    # PyPI project names (PEP 503): no extras, no URL, no specifier.
    "pipx": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"), "uv": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"),
}
# Exact versions only: no ranges, tags, wildcards or shell characters.
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z][0-9A-Za-z.-]{0,63})?(\+[0-9A-Za-z][0-9A-Za-z.-]{0,63})?$")
DISTRO_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+:~_-]{0,63}$")
APK_VERSION = re.compile(r"^[0-9][0-9A-Za-z._]{0,63}(-r[0-9]{1,6})?$")
PEP440 = re.compile(r"^[0-9]+(\.[0-9]+){0,3}((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?$")
# pacman has no entry: it cannot install a pinned version (partial upgrades are unsupported on Arch).
VERSION_PATTERNS = {"apt": DISTRO_VERSION, "dnf": DISTRO_VERSION, "zypper": DISTRO_VERSION, "apk": APK_VERSION,
                    "npm": SEMVER, "bun": SEMVER, "pnpm": SEMVER, "yarn": SEMVER, "pipx": PEP440, "uv": PEP440}
# Before the manager is known (manager "auto") only this shape is required.
LOOSE_NAME = re.compile(r"^[@A-Za-z0-9][A-Za-z0-9+._@/-]{0,199}$")

# Built-in protected packages per manager (chapter 6.1): reaching and controlling the host, the kernel, libc, the
# package manager and what the Guardian check commands need. A project can only add to them (P4).
_COMMON = ("openssh*", "tailscale*", "headscale*", "server-guardian*", "guardian-*")
_RPM_BASE = ("sudo", "systemd*", "dbus*", "glibc*", "coreutils", "findutils", "bash", "util-linux*", "ca-certificates*",
             "cloud-init*", "python3", "filesystem")
PROTECTED = {
    "apt": _COMMON + ("ssh", "sudo", "sudo-rs", "systemd*", "libsystemd*", "dbus*", "libc6*", "libc-bin", "libc-dev-bin",
                      "linux-*", "grub*", "shim*", "initramfs-tools*", "apt", "apt-*", "libapt*", "dpkg*", "coreutils",
                      "findutils", "procps", "bash", "dash", "util-linux", "login", "passwd", "libpam*", "mount",
                      "netplan*", "network-manager*", "ifupdown*", "cloud-init*", "iproute2", "ca-certificates",
                      "python3", "python3-minimal", "python3-apt", "udev", "init", "base-files", "base-passwd"),
    "dnf": _COMMON + ("sudo", "systemd*", "dbus*", "glibc*", "kernel*", "grub2*", "shim*", "dnf*", "yum*", "rpm*",
                      "libdnf*", "coreutils", "findutils", "procps-ng", "bash", "util-linux", "shadow-utils", "pam",
                      "NetworkManager*", "cloud-init*", "iproute", "ca-certificates", "python3", "python3-dnf",
                      "python3-libs", "python3-rpm", "basesystem", "filesystem", "setup"),
    "zypper": _COMMON + _RPM_BASE + ("kernel-*", "grub2*", "shim*", "zypper*", "libzypp*", "rpm*", "procps*", "shadow",
                                     "pam*", "NetworkManager*", "wicked*", "iproute2", "python3-base", "aaa_base",
                                     "dracut", "udev", "*-release", "openSUSE-release*", "SLES-release*"),
    "apk": _COMMON + ("sudo", "doas", "alpine-*", "apk-tools*", "busybox*", "musl*", "openrc*", "linux-*", "mkinitfs",
                      "grub*", "syslinux*", "ssl_client", "libc-utils", "eudev*", "dbus*", "ifupdown-ng*", "iproute2",
                      "ca-certificates*", "cloud-init*", "bash", "python3"),
    "pacman": _COMMON + ("sudo", "systemd*", "dbus*", "glibc", "linux", "linux-*", "grub", "efibootmgr", "pacman*",
                         "archlinux-keyring", "base", "filesystem", "coreutils", "findutils", "bash", "util-linux*",
                         "shadow", "pam", "networkmanager", "iproute2", "ca-certificates*", "cloud-init", "python",
                         "procps-ng", "openssl", "mkinitcpio*"),
    "brew": _COMMON + ("openssl@*", "ca-certificates", "python@*"),
    "npm": ("npm", "corepack", "server-guardian*"),
    "bun": ("server-guardian*",),
    "pnpm": ("pnpm", "corepack", "server-guardian*"),
    "yarn": ("yarn", "corepack", "server-guardian*"),
    "pipx": ("pipx", "uv", "server-guardian*", "guardian-*"),
    "uv": ("uv", "pipx", "server-guardian*", "guardian-*"),
}
SYSTEM_FAMILY = {"linux": ("apt", "dnf", "zypper", "apk", "pacman"), "macos": ("brew",)}

# Shell text before an adapter's command. $S is the privilege prefix (sudo -n when not root); sudo is only reached
# on a host whose facts said it works without a password (the validator checks that).
SUDO_PREAMBLE = 'S=""; if [ "$(id -u)" != 0 ]; then S="sudo -n"; fi; '
# npm: sudo only when the global prefix is not writable by the remote user.
NPM_PREAMBLE = ('S=""; P=$(npm prefix -g 2>/dev/null); if [ -n "$P" ] && [ ! -w "$P/lib/node_modules" ] && '
                '[ ! -w "$P/lib" ] && [ "$(id -u)" != 0 ] && sudo -n true 2>/dev/null; then S="sudo -n"; fi; '
                'export NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false; ')
BREW_PREAMBLE = "export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_ENV_HINTS=1; "
# pnpm refuses a global install when PNPM_HOME is not set and on the PATH (ERR_PNPM_NO_GLOBAL_BIN_DIR).
PNPM_PREAMBLE = ('if [ -z "$PNPM_HOME" ]; then case "$(uname -s)" in Darwin) PNPM_HOME="$HOME/Library/pnpm";; '
                 '*) PNPM_HOME="$HOME/.local/share/pnpm";; esac; fi; export PNPM_HOME PATH="$PNPM_HOME:$PATH"; ')
# Only yarn classic has `yarn global`; yarn 2+ (berry) removed it.
YARN_PREAMBLE = ('case "$(yarn --version 2>/dev/null)" in 1.*) ;; *) echo \'yarn is not available on this host '
                 '(global installs need yarn 1.x)\'; exit 3;; esac; ')
PREAMBLES = {"npm": NPM_PREAMBLE, "brew": BREW_PREAMBLE, "pnpm": PNPM_PREAMBLE, "yarn": YARN_PREAMBLE}
NOT_AVAILABLE = "is not available on this host"
SECTION = "@@"


def shape(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """The manager-independent argument rules; returns normalized arguments. Raises ValueError."""
    allowed = {"target_host", "manager", "packages", "ignore_scripts"} if tool == INSTALL else \
        {"target_host", "manager", "packages", "purge"}
    extra = sorted(set(args) - allowed)
    if extra:
        raise ValueError(f"unknown arguments {extra}; {tool} takes {sorted(allowed)}")
    manager = str(args.get("manager", "")).strip().lower()
    if manager != "auto" and manager not in MANAGERS:
        raise ValueError(f"manager must be \"auto\" or one of {', '.join(MANAGERS)}")
    packages = args.get("packages")
    if not isinstance(packages, list) or not 1 <= len(packages) <= MAX_PACKAGES:
        raise ValueError(f"packages must list 1 to {MAX_PACKAGES} entries of {{\"name\", \"version\"?}}")
    out: List[Dict[str, str]] = []
    for entry in packages:
        if not isinstance(entry, dict) or set(entry) - {"name", "version"} or not isinstance(entry.get("name"), str):
            raise ValueError("every package is an object {\"name\": ..., \"version\": ...?} (version optional)")
        item = {"name": entry["name"].strip()}
        if not LOOSE_NAME.fullmatch(item["name"]):
            raise ValueError(f"{entry['name']!r} is not a package name (a plain registry or repository name)")
        if "version" in entry and entry["version"] not in (None, ""):
            if tool == REMOVE:
                raise ValueError("a removal names packages only, not versions")
            if not isinstance(entry["version"], str):
                raise ValueError("version must be a string")
            item["version"] = entry["version"].strip()
        out.append(item)
    names = [p["name"] for p in out]
    if len(set(names)) != len(names):
        raise ValueError("a package is listed twice")
    for flag in ("ignore_scripts", "purge"):
        if flag in args and not isinstance(args[flag], bool):
            raise ValueError(f"{flag} must be true or false")
    return {**args, "manager": manager, "packages": out}


def resolve_manager(facts: Dict[str, Any], requested: str) -> str:
    """The package manager for "auto": the host's system manager. Raises ValueError when there is none."""
    if requested != "auto":
        return requested
    managers = facts.get("managers") or {}
    for manager in SYSTEM_FAMILY.get(str(facts.get("os_family", "")), ()):
        if PROGRAMS[manager] in managers:
            return manager
    raise ValueError("manager auto: no system package manager (apt, dnf, zypper, apk, pacman, brew) found on this host; name one")


def extra_for(extra: Any, manager: str) -> Sequence[str]:
    """The additional protected patterns for one manager. extra is a plain list (project additions, every manager), a
    mapping manager -> patterns whose "*" key applies to every manager, or a package_roles.Protection."""
    if hasattr(extra, "for_manager"):  # package_roles.Protection
        return tuple(extra.for_manager(manager))
    if isinstance(extra, Mapping):
        return tuple(extra.get("*", ())) + tuple(extra.get(manager, ()))
    return tuple(extra or ())


def protected_source(extra: Any, manager: str, name: str) -> str:
    """" — role gitea" and the like for the refusal text, when extra knows where its patterns came from."""
    found = extra.source_of(manager, name) if hasattr(extra, "source_of") else None
    return f" — {found[1]}" if found else ""


def protected_match(manager: str, name: str, extra: Any = ()) -> Optional[str]:
    """The protected pattern that covers a package name (an architecture suffix is ignored), else None."""
    base = name.split(":", 1)[0]
    for pattern in tuple(PROTECTED.get(manager, ())) + tuple(extra_for(extra, manager)):
        if fnmatch.fnmatchcase(base, pattern):
            return pattern
    return None


def _unverified_allowed() -> Sequence[str]:
    return [m.strip() for m in os.environ.get("GUARDIAN_PACKAGES_ALLOW_UNVERIFIED", "").split(",") if m.strip()]


def check(tool: str, args: Dict[str, Any], facts: Optional[Dict[str, Any]],
          extra_protected: Any = ()) -> Dict[str, Any]:
    """The validator of one package action against the host's facts. Returns the arguments with the manager resolved;
    raises ValueError with the one-line reason."""
    args = shape(tool, args)
    if not facts:
        raise ValueError("host facts are not available (the host did not answer or the answer was incomplete)")
    manager = resolve_manager(facts, args["manager"])
    if manager in UNVERIFIED and manager not in _unverified_allowed():
        raise ValueError(f"{manager} commands have not been verified on a real host yet; the operator enables them "
                         "with GUARDIAN_PACKAGES_ALLOW_UNVERIFIED")
    if PROGRAMS[manager] not in (facts.get("managers") or {}):
        raise ValueError(f"{manager} is not installed on this host (found: {', '.join(sorted(facts.get('managers') or {})) or 'none'})")
    if manager in SYSTEM_MANAGERS and not (facts.get("root") or facts.get("sudo_nopasswd")):
        raise ValueError("sudo_required: this host has no passwordless sudo and the login is not root")
    if manager == "brew" and facts.get("root"):
        raise ValueError("brew does not run as root; the hub logs in as root on this host")
    if args.get("purge") and manager != "apt":
        raise ValueError("purge applies to apt only")
    if args.get("ignore_scripts") and manager not in SCRIPT_MANAGERS:
        raise ValueError("ignore_scripts applies to npm, bun, pnpm and yarn only")
    if manager in SINGLE_PACKAGE and len(args["packages"]) > 1:
        raise ValueError(f"{manager} installs one package per action; propose one action for each")
    packages = []
    for pkg in args["packages"]:
        if not NAME_PATTERNS[manager].fullmatch(pkg["name"]):
            raise ValueError(f"{pkg['name']!r} is not a valid {manager} package name (no version, URL, path, tap or option)")
        version = pkg.get("version")
        if version:
            pattern = VERSION_PATTERNS.get(manager)
            if pattern is None:
                raise ValueError(f"{manager} cannot install a pinned version" +
                                 ("; name the formula (name@x) instead" if manager == "brew" else ""))
            if not pattern.fullmatch(version):
                raise ValueError(f"version {version!r} is not an exact {manager} version (no ranges, tags or wildcards)")
        if tool == REMOVE:
            hit = protected_match(manager, pkg["name"], extra_protected)
            if hit:
                raise ValueError(f"{pkg['name']} is protected ({hit}{protected_source(extra_protected, manager, pkg['name'])}) "
                                 "and cannot be removed by an action")
        packages.append(pkg)
    return {**args, "manager": manager, "packages": packages}


def risk(tool: str, args: Dict[str, Any], preview: Optional[Dict[str, Any]] = None) -> str:
    """medium; high for a listed-only manager, a removal list longer than five, an install whose simulation removes
    something, or a removal on a host whose derived protection has blind spots (preview["blind"], package_roles)."""
    if args.get("manager") in LISTED_ONLY:
        return "high"
    if tool == REMOVE and (preview or {}).get("blind"):
        return "high"
    removals = [i for i in ((preview or {}).get("items") or []) if i.startswith("remove:")]
    if tool == REMOVE and (len(args.get("packages") or []) > MANY_REMOVALS or len(removals) > MANY_REMOVALS):
        return "high"
    if tool == INSTALL and removals:
        return "high"
    return "medium"


def _spec(pkg: Dict[str, str], manager: str) -> str:
    version = pkg.get("version")
    if not version:
        return pkg["name"]
    joined = {"apt": "=", "zypper": "=", "apk": "=", "dnf": "-", "pipx": "==", "uv": "=="}.get(manager, "@")
    return f"{pkg['name']}{joined}{version}"


def argv(tool: str, args: Dict[str, Any], simulate: bool = False) -> List[str]:
    """The install or remove command (or its simulation) without the privilege prefix. args come from check()."""
    manager, packages = args["manager"], args["packages"]
    specs = [_spec(p, manager) for p in packages]
    names = [p["name"] for p in packages]
    install = tool == INSTALL
    if manager == "apt":
        base = ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-s" if simulate else "-y",
                "-o", "Dpkg::Options::=--force-confold", "-o", "Debug::NoLocking=1"]
        return base + (["install"] + specs if install else ["purge" if args.get("purge") else "remove"] + names)
    if manager == "dnf":
        return ["dnf", "--assumeno" if simulate else "-y", "install" if install else "remove"] + (specs if install else names)
    if manager == "zypper":
        return ["zypper", "--non-interactive", install and "install" or "remove"] + (["--dry-run"] if simulate else []) + \
            (specs if install else names)
    if manager == "apk":
        return ["apk", "add" if install else "del"] + (["--simulate"] if simulate else []) + (specs if install else names)
    if manager == "pacman":
        # No dry run: --print lists what the transaction would fetch or remove. No -y: a database refresh without a
        # full upgrade is the partial upgrade Arch warns about. --needed keeps an up-to-date package untouched.
        if simulate:
            return ["pacman", "-S" if install else "-R", "--print", "--print-format", "%n %v", "--noconfirm"] + \
                (["--needed"] if install else []) + names
        return ["pacman", "-S" if install else "-R", "--noconfirm"] + (["--needed"] if install else []) + names
    if manager == "brew":
        if install:
            return ["brew", "install", "--formula"] + (["--dry-run"] if simulate else []) + specs
        # brew has no dry run for uninstall: the simulation lists the installed formulae that need the packages.
        return ["brew", "uses", "--installed", "--formula"] + names if simulate else ["brew", "uninstall", "--formula"] + names
    if manager == "npm":
        flags = ["--ignore-scripts"] if args.get("ignore_scripts") else []
        if install:
            return ["npm", "install", "-g", "--no-audit", "--no-fund"] + flags + (["--dry-run"] if simulate else []) + specs
        return ["npm", "uninstall", "-g"] + (["--dry-run"] if simulate else []) + names
    flags = ["--ignore-scripts"] if args.get("ignore_scripts") else []
    if manager == "bun":
        return ["bun", "add", "-g"] + flags + specs if install else ["bun", "remove", "-g"] + names
    if manager == "pnpm":
        return ["pnpm", "add", "-g"] + flags + specs if install else ["pnpm", "remove", "-g"] + names
    if manager == "yarn":
        return ["yarn", "global", "add", "--non-interactive"] + flags + specs if install else \
            ["yarn", "global", "remove", "--non-interactive"] + names
    if manager == "pipx":
        return ["pipx", "install"] + specs if install else ["pipx", "uninstall"] + names
    if manager == "uv":
        return ["uv", "tool", "install"] + specs if install else ["uv", "tool", "uninstall"] + names
    raise ValueError(f"unknown manager {manager}")


def query_argv(manager: str, names: Sequence[str]) -> List[str]:
    """Read-only: which of the names are installed, and at which version."""
    if manager == "apt":
        return ["dpkg-query", "-W", "-f=${Package} ${Version} ${db:Status-Abbrev}\\n"] + list(names)
    if manager in ("dnf", "zypper"):
        return ["rpm", "-q", "--qf", "%{NAME} %{VERSION}-%{RELEASE}\\n"] + list(names)
    if manager == "apk":
        return ["apk", "list", "--installed"] + list(names)
    if manager == "pacman":
        return ["pacman", "-Q"] + list(names)
    if manager == "brew":
        return ["brew", "list", "--versions", "--formula"] + list(names)
    if manager == "npm":
        return ["npm", "ls", "-g", "--depth=0", "--json"] + list(names)
    if manager == "bun":
        return ["bun", "pm", "ls", "-g"]
    if manager == "pnpm":
        return ["pnpm", "list", "-g", "--depth=0", "--json"]
    if manager == "yarn":
        # `yarn global list` shows only packages that have binaries; the global package.json tree has them all.
        script = ('d=$(yarn global dir 2>/dev/null) || exit 0; for n in "$@"; do v=$(sed -n '
                  '\'s/^ *"version": *"\\([^"]*\\)".*/\\1/p\' "$d/node_modules/$n/package.json" 2>/dev/null | head -1); '
                  '[ -n "$v" ] && echo "$n $v"; done; true')
        return ["sh", "-c", script, "sh"] + list(names)
    if manager == "pipx":
        return ["pipx", "list", "--short"]
    if manager == "uv":
        return ["uv", "tool", "list"]
    raise ValueError(f"unknown manager {manager}")


def _line(parts: Sequence[str]) -> str:
    return " ".join(shlex.quote(p) for p in parts)


def _preamble(manager: str) -> str:
    program = PROGRAMS[manager]
    guard = f"command -v {program} >/dev/null 2>&1 || {{ echo '{manager} {NOT_AVAILABLE}'; exit 3; }}; "
    pre = PREAMBLES.get(manager, SUDO_PREAMBLE if manager in SYSTEM_MANAGERS else "")
    return ops_facts.PATH_PREFIX + guard + pre


def _prefixed(manager: str, parts: Sequence[str], root: bool = True) -> str:
    """The command text with the privilege prefix: system managers and (when the prefix needs it) npm run under $S."""
    if manager == "npm":
        return f'$S env "PATH=$PATH" ' + _line(parts)
    if manager in SYSTEM_MANAGERS and root:
        return "$S " + _line(parts)
    return _line(parts)


def _resolved(args: Dict[str, Any]) -> str:
    if args.get("manager") not in MANAGERS:
        raise ValueError("manager is not resolved; check the action against the host's facts first")
    return args["manager"]


def run_command(tool: str, args: Dict[str, Any]) -> str:
    manager = _resolved(args)
    return _preamble(manager) + _prefixed(manager, argv(tool, args))


def measure_command(tool: str, args: Dict[str, Any]) -> str:
    """Read-only: the installed versions, the simulation (with its exit status) and the running services, in
    sections that parse_sections() separates."""
    manager = _resolved(args)
    names = [p["name"] for p in args["packages"]]
    simulate = argv(tool, args, simulate=True)
    listed_only = manager in LISTED_ONLY
    parts = [_preamble(manager), f"echo '{SECTION}installed'; ", _line(query_argv(manager, names)) + " 2>/dev/null; "]
    if listed_only:
        parts.append(f"echo '{SECTION}simulation'; echo '{SECTION}exit 0'; ")
    else:
        # dnf, zypper, apk and pacman need root even for a simulation; apt -s does not.
        parts.append(f"echo '{SECTION}simulation'; ( {_prefixed(manager, simulate, root=manager != 'apt')} ) 2>&1; "
                     f"echo \"{SECTION}exit $?\"; ")
    parts.append(f"echo '{SECTION}services'; command -v systemctl >/dev/null 2>&1 && systemctl list-units "
                 "--type=service --state=running --no-legend --plain --no-pager 2>/dev/null | awk '{print $1}' | head -200; true")
    return "".join(parts)


def parse_sections(text: str) -> Dict[str, Any]:
    """{"installed", "simulation", "services": text, "exit": int|None} from measure_command's output."""
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    code: Optional[int] = None
    for line in (text or "").splitlines():
        if line.startswith(SECTION):
            word, _, rest = line[len(SECTION):].partition(" ")
            if word == "exit":
                code = int(rest) if rest.strip().lstrip("-").isdigit() else None
            else:
                current = word
                sections.setdefault(word, [])
        elif current:
            sections[current].append(line)
    return {"installed": "\n".join(sections.get("installed", [])), "simulation": "\n".join(sections.get("simulation", [])),
            "services": "\n".join(sections.get("services", [])), "exit": code, "seen": sorted(sections)}


def key(manager: str, name: str) -> str:
    """The name as the installed list spells it: PyPI names are compared in their normalized form (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower() if manager in ("pipx", "uv") else name


_APK_LIST = re.compile(r"^(?P<name>\S+?)-(?P<ver>[0-9][0-9A-Za-z._]*-r[0-9]+)\s")
_UV_LIST = re.compile(r"^(\S+) v(\S+)$")
_BUN_LIST = re.compile(r"^[├└|`+\\-]+\s*(@?[^@\s]+(?:/[^@\s]+)?)@(\S+)")


def parse_installed(manager: str, text: str) -> Optional[Dict[str, str]]:
    """{name: version} of the installed ones among the queried names; None when the output cannot be read."""
    found: Dict[str, str] = {}
    if manager in ("npm", "pnpm"):
        try:
            data = json.loads(text or "{}")
        except ValueError:
            return None
        # npm prints one object; pnpm a list of them (one per workspace, one for the global directory).
        for entry in data if isinstance(data, list) else [data]:
            for name, info in ((entry or {}).get("dependencies") or {}).items():
                found[name] = str((info or {}).get("version", ""))
        return found
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("package ", "Error", "Warning")):
            continue
        if manager == "apt":
            parts = line.split()
            if len(parts) >= 3 and parts[-1].startswith("ii"):
                found[parts[0].split(":")[0]] = parts[1]
        elif manager in ("dnf", "zypper", "pacman", "yarn", "pipx"):
            parts = line.split()
            if len(parts) == 2:
                found[key(manager, parts[0])] = parts[1]
        elif manager == "apk":
            match = _APK_LIST.match(line) if line.endswith("[installed]") else None
            if match:
                found[match.group("name")] = match.group("ver")
        elif manager == "uv":
            match = _UV_LIST.match(line)
            if match:
                found[key(manager, match.group(1))] = match.group(2)
        elif manager == "brew":
            parts = line.split()
            if len(parts) >= 2:
                found[parts[0]] = parts[-1]
        elif manager == "bun":
            match = _BUN_LIST.match(line)
            if match:
                found[match.group(1)] = match.group(2)
    return found


_APT_INST = re.compile(r"^Inst (\S+)(?: \[([^\]]+)\])? \((\S+)")
_APT_REMV = re.compile(r"^Remv (\S+)(?: \[([^\]]+)\])?")
_NPM_LINE = re.compile(r"^(add|remove|change) (\S+) (\S+)\s*$")
_DNF_SECTIONS = {"installing": "add", "installing dependencies": "add", "installing weak dependencies": "add",
                 "installing group/module packages": "add", "upgrading": "upgrade", "removing": "remove",
                 "removing dependent packages": "remove", "removing unused dependencies": "remove",
                 "downgrading": "change", "reinstalling": "change"}
_ARCHES = {"x86_64", "aarch64", "noarch", "i686", "i386", "armv7hl", "ppc64le", "s390x", "src"}
_ZYPPER_HEADER = re.compile(r"^The following (?:\d+ )?(?:NEW )?packages? (?:is|are) going to be "
                            r"(installed|upgraded|REMOVED|reinstalled|downgraded)\b")
_ZYPPER_KINDS = {"installed": "add", "upgraded": "upgrade", "REMOVED": "remove", "reinstalled": "change",
                 "downgraded": "change"}
_APK_STEP = re.compile(r"^\(\d+/\d+\) (Installing|Upgrading|Purging|Reinstalling|Downgrading) (\S+) \(([^)]*)\)")
_APK_KINDS = {"Installing": "add", "Upgrading": "upgrade", "Purging": "remove", "Reinstalling": "change",
              "Downgrading": "change"}
_PACMAN_LINE = re.compile(r"^(\S+) ((?:[0-9]+:)?[0-9][^\s]*)$")
_BREW_BLOCK = re.compile(r"^==> Would (install|upgrade) \d+ (?:formula|formulae|dependency|dependencies|casks?)\b")


def item(kind: str, name: str, version: str = "") -> str:
    return f"{kind}:{name}" + (f"={version}" if version else "")


def _dnf_items(text: str) -> List[str]:
    items, kind, pending = [], None, ""
    for raw in text.splitlines():
        line = raw.strip()
        key = line.rstrip(":").strip().lower()
        if line.endswith(":") and key in _DNF_SECTIONS:
            kind, pending = _DNF_SECTIONS[key], ""
            continue
        if line.lower().startswith("transaction summary") or line.startswith("====") or not line:
            if line.lower().startswith("transaction summary"):
                kind = None
            continue
        if kind is None or line.startswith(("replacing", "Package ", "Repository")):
            continue
        tokens = line.split()
        if len(tokens) == 1:
            pending = tokens[0]  # dnf wraps a long name onto its own line
        elif len(tokens) >= 3 and tokens[1] in _ARCHES:
            items.append(item(kind, pending or tokens[0], tokens[2]))
            pending = ""
        elif len(tokens) >= 3 and pending and tokens[0] in _ARCHES:
            items.append(item(kind, pending, tokens[1]))
            pending = ""
    return items


def _brew_items(text: str) -> List[str]:
    items, kind = [], None
    for raw in text.splitlines():
        line = raw.strip()
        header = _BREW_BLOCK.match(line)
        if header:
            kind = "add" if header.group(1) == "install" else "upgrade"
            continue
        if line.startswith("==>") or not line or line.startswith(("Warning", "Error")):
            kind = None
            continue
        if kind:
            tokens = line.split()
            if len(tokens) == 4 and tokens[2] == "->":  # "ffmpeg 9.0.2 -> 9.0.2_1" (an upgrade)
                items.append(item(kind, tokens[0], tokens[3]))
            elif len(tokens) == 2 and re.match(r"^[0-9]", tokens[1]):
                items.append(item(kind, tokens[0], tokens[1]))
            else:
                items.extend(item(kind, token) for token in tokens)
    return items


def _zypper_items(text: str) -> List[str]:
    """Package names under "The following N NEW packages are going to be installed:" and the like (indented, several
    to a line, no versions). Other "The following ..." blocks (recommended, suggested, support) are informational."""
    items, kind = [], None
    for raw in text.splitlines():
        header = _ZYPPER_HEADER.match(raw)
        if header:
            kind = _ZYPPER_KINDS[header.group(1)]
        elif raw.startswith("The following") or not raw.strip():
            kind = None
        elif kind and raw.startswith(" "):
            items.extend(item(kind, token) for token in raw.split())
        else:
            kind = None
    return items


def _apk_items(text: str) -> List[str]:
    items = []
    for raw in text.splitlines():
        match = _APK_STEP.match(raw.strip())
        if match:
            version = match.group(3).split("->")[-1].strip()
            items.append(item(_APK_KINDS[match.group(1)], match.group(2), version))
    return items


def _pacman_items(tool: str, text: str) -> List[str]:
    """`pacman --print --print-format "%n %v"` prints one "name version" per target; the kind is the action's."""
    kind = "add" if tool == INSTALL else "remove"
    items = []
    for raw in text.splitlines():
        match = _PACMAN_LINE.match(raw.strip())
        if match and not raw.startswith(("warning:", "error:", "::")):
            items.append(item(kind, match.group(1), match.group(2)))
    return items


def parse_simulation(manager: str, tool: str, args: Dict[str, Any], sections: Dict[str, Any]) -> Tuple[List[str], Optional[str]]:
    """(items, error): what the simulation would change, or why it could not be made."""
    text = sections["simulation"]
    if manager in LISTED_ONLY:
        return [item("add" if tool == INSTALL else "remove", p["name"], p.get("version", "")) for p in args["packages"]], None
    errors = [line.strip() for line in text.splitlines()
              if re.match(r"^(E: |Error|npm error|npm ERR!|Failed|No match for argument|error: |ERROR: |Problem: |"
                          r"Package '[^']*' not found|No provider of)", line.strip())]
    if errors:
        return [], "; ".join(errors[:3])[:400]
    code = sections["exit"]
    if manager == "dnf":
        # --assumeno ends with "Operation aborted" (exit 1) after a good transaction table.
        if code not in (0, 1) or (code == 1 and "Operation aborted" not in text and "Nothing to do" not in text):
            return [], f"simulation failed (exit {code}): {text.strip()[-200:]}"
    elif code not in (0, None):
        return [], f"simulation failed (exit {code}): {text.strip()[-200:]}"
    if sections.get("exit") is None:
        return [], "simulation did not finish"
    if manager == "apt":
        items = []
        for line in text.splitlines():
            inst, remv = _APT_INST.match(line), _APT_REMV.match(line)
            if inst:
                items.append(item("upgrade" if inst.group(2) else "add", inst.group(1), inst.group(3)))
            elif remv:
                items.append(item("remove", remv.group(1), remv.group(2) or ""))
        return items, None
    if manager == "dnf":
        return _dnf_items(text), None
    if manager == "zypper":
        return _zypper_items(text), None
    if manager == "apk":
        return _apk_items(text), None
    if manager == "pacman":
        return _pacman_items(tool, text), None
    if manager == "brew":
        if tool == INSTALL:
            return _brew_items(text), None
        wanted = {p["name"] for p in args["packages"]}
        dependents = [t for t in text.split() if t not in wanted and not t.startswith("=")]
        return [item("remove", p["name"]) for p in args["packages"]] + [item("dependent", d) for d in dependents], None
    items = []
    for line in text.splitlines():
        match = _NPM_LINE.match(line)
        if match:
            items.append(item({"add": "add", "remove": "remove", "change": "upgrade"}[match.group(1)], match.group(2), match.group(3)))
    return items, None


def assess(tool: str, args: Dict[str, Any], out: str, extra_protected: Any = ()) -> Dict[str, Any]:
    """What a measurement of a package action adds to {"bytes", "items", "text", "error"}: the installed versions
    ("installed"), the running services, and "refusal" when the simulation breaks a rule (a protected package would
    go, an installed brew formula needs a package being removed). error is set when nothing could be measured."""
    manager = args["manager"]
    sections = parse_sections(out)
    if NOT_AVAILABLE in out and "installed" not in sections["seen"]:
        return {"items": None, "installed": None, "services": [], "error": f"{manager} {NOT_AVAILABLE}"}
    installed = parse_installed(manager, sections["installed"])
    items, error = parse_simulation(manager, tool, args, sections)
    services = sorted({line.strip() for line in sections["services"].splitlines() if line.strip().endswith(".service")})
    result: Dict[str, Any] = {"items": None if error else items, "installed": installed, "services": services,
                              "error": error}
    if error:
        return result
    names = [p["name"] for p in args["packages"]]
    if tool == REMOVE and installed is not None and not any(key(manager, n) in installed for n in names):
        result["error"] = f"none of {', '.join(names)} is installed on this host"
        return result
    protected = [(i, protected_match(manager, i.split(":", 1)[1].split("=", 1)[0], extra_protected))
                 for i in items if i.startswith("remove:")]
    hits = [f"{i.split(':', 1)[1]} ({p}{protected_source(extra_protected, manager, i.split(':', 1)[1].split('=', 1)[0])})"
            for i, p in protected if p]
    if hits:
        result["refusal"] = "the simulation would remove protected packages: " + ", ".join(hits)
    dependents = [i.split(":", 1)[1] for i in items if i.startswith("dependent:")]
    if dependents:
        result["refusal"] = ("installed formulae depend on it: " + ", ".join(dependents[:8])
                             + " — remove those first or name them in the same action")
    return result


def verify(tool: str, args: Dict[str, Any], before: Dict[str, Any], after: Dict[str, Any], code: int) -> Dict[str, Any]:
    """The verification of a run: each package present at the version (install) or absent (remove), and no service
    that was running before has stopped. ok is False when the installed list cannot be read."""
    now = after.get("installed")
    verify_: Dict[str, Any] = {"installed": now}
    if now is None:
        verify_.update(ok=False, problem="the installed packages could not be read after the run")
        return verify_
    wrong = []
    for pkg in args["packages"]:
        have = now.get(key(args["manager"], pkg["name"]))
        if tool == REMOVE:
            if have is not None:
                wrong.append(f"{pkg['name']} is still installed")
        elif have is None:
            wrong.append(f"{pkg['name']} is not installed")
        elif pkg.get("version") and not have.startswith(pkg["version"].split(":")[-1]) and pkg["version"] != have:
            wrong.append(f"{pkg['name']} is {have}, not {pkg['version']}")
    stopped = sorted(set(before.get("services") or []) - set(after.get("services") or []))
    ok_exit = OK_EXIT.get(args["manager"], (0,))
    verify_.update(wrong=wrong, services_stopped=stopped, ok=code in ok_exit and not wrong and not stopped)
    return verify_
