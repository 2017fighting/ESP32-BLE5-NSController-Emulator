#!/usr/bin/env python3
"""Bring the container up in one command on the bench host.

A *wrapper* over §10.4's compose files, not a second deployment path: it runs
`docker compose -f container/compose.yaml` from `container/`'s project name (plus
`-f container/compose.macos.yaml` on macOS), so it manages the same `container-controller-1` the
documented line does. Four things it adds, each because a documented step is easy to skip or easy
to get quietly wrong:

1. **The corpus preflight, before `up`.** Compose interpolates `${REFERENCE_ROOT:-$HOME/clone}` at
   parse time and does not check the path, and Docker does not fail on a missing bind source
   either — it creates a **directory** there, inside the container and on the host alike. That is
   #43's failure mode: the container reported `KEY_ABSENT` for a key that *was* mounted, and the
   directory landed inside the pinned reference clone. The sources are read out of
   `container/compose.yaml` itself (not restated here), so a mount added later is preflighted the
   day it is added, and "found no volumes" is an error rather than a clean bill of health.
2. **The device node.** macOS has no `/dev/ttyACM0`; `scripts/find_serial_port.py` resolves the
   device by USB serial number and the committed macOS override (§10.7) does the mapping. The
   wrapper calls that resolver rather than globbing `usbmodem*`, because this host carries a second
   such node (a Type-C AV adapter) and picking the wrong one is §10.3's trap.
3. **The build.** A stale image looks like a working container running older code — measured on
   this host: a 28-hour-old image started at 921600, reported `KEY_UNVERIFIED` and read HELLO as
   `fw 1.0.0`, none of which HEAD does. So the image is rebuilt by default (`--no-build` skips it).
4. **The report, and what "up" means.** The run is done when the API answers *and* — unless
   `--no-device` — the device has been read back on the control link, so a container that is
   serving while the device never answers is an error (exit 4) with the state printed, not a
   success: the usual causes are another holder of the port (§10.2, flashing) and the wrong node
   (§10.3). With `--no-device` there is no link to expect, and `control DOWN` in the report is
   the honest result rather than a failure.

The two substitutions are the wrapper's whole interface to the deployment file: `REFERENCE_ROOT`
is exported for the mounts, `NS2_IMAGE` when `--image` pins a bench tag (#42). The one generated
file is a `devices:`-only override, and only when the committed files cannot say the right thing —
`--no-device`, or a Linux host whose device is not at `/dev/ttyACM0`. It never restates the image
or the mounts, because those are `container/compose.yaml`'s own now.

```sh
python3 scripts/start_container.py              # preflight, build, up -d, report the state
python3 scripts/start_container.py --no-build   # reuse the image that is already there
python3 scripts/start_container.py --dry-run    # print what it would do, and stop
python3 scripts/start_container.py --no-device  # run with no device: the control link stays down
python3 scripts/start_container.py --stop       # §10.2's down, with the project pinned
```

Exit codes: 0 = up and answering (or a successful `--stop`); 2 = a required host path is missing, or
two arguments that cannot hold together (argparse's own code, with the usage line); 3 = `docker
compose` failed; 4 = the container never answered on `http://localhost:8080`, or the control link
never came up. `--json` carries the *run's* result, so it is refused rather than ignored where there
is none: with `--dry-run`, whose output is the plan in text, and with `--stop`.

The generated override uses Compose's `!override` tag to clear a mapping the base file sets, so
the two modes that generate one need Compose ≥ 2.24 (the bench measured v5.1.2, §10.7).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BASE_COMPOSE = REPO_ROOT / "container" / "compose.yaml"
MACOS_COMPOSE = REPO_ROOT / "container" / "compose.macos.yaml"

#: The container-side path §10.4 fixes; the macOS override maps the host node onto it.
CONTAINER_DEVICE_PATH = "/dev/ttyACM0"

#: The corpus root, one substitution for all three mounts (§10.4). Read here so the preflight
#: resolves a source exactly the way Compose does, including the `$HOME/clone` default.
ROOT_SUBSTITUTION = "${REFERENCE_ROOT:-$HOME/clone}"
UNKNOWN_SUBSTITUTION = re.compile(r"\$\{[^}]*\}")

#: Fixed by `container/compose.yaml`'s `ports:` and by `Settings.http_port`.
HTTP_PORT = 8080

#: How long the container may take to serve, and then to read the device back.
READY_TIMEOUT_SECONDS = 30.0

#: §6.7 — the key has two accepted spellings, and only the first is a mount source.
ESSENTIAL_DIR = "!Essential Files"
KEY_FILE = "key_retail.bin"
TWO_FILE_KEY = ("unfixed-info.bin", "locked-secret.bin")


# ── the preflight: what the compose file will mount ────────────────────────


def mount_sources(compose_text: str) -> list[str]:
    """Every host source in the compose file's `volumes:` list, as the file writes it.

    Indentation-scanned rather than parsed as YAML, because this runs on the host with no
    dependencies and the block is four lines of `- "<source>:<target>:ro"`. A named volume or an
    entry with no host path is skipped; the caller refuses an empty result, so a file this cannot
    read is a loud failure rather than a silent pass.
    """
    lines = compose_text.splitlines()
    index = next((number for number, line in enumerate(lines) if line.strip() == "volumes:"), None)
    if index is None:
        return []
    indent = len(lines[index]) - len(lines[index].lstrip())
    sources: list[str] = []
    for line in lines[index + 1:]:
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break  # the next service key: the block ended
        if not line.lstrip().startswith("-"):
            continue
        entry = re.sub(r":(?:ro|rw)$", "", line.lstrip()[1:].strip().strip('"').strip())
        # The source runs to the last `:` that opens an absolute target path. A substitution's own
        # `:-` cannot be mistaken for it: that colon is followed by a default, not by `/`.
        boundary = entry.rfind(":/")
        source = entry[:boundary] if boundary >= 0 else entry
        if source.startswith("/") or "${" in source:
            sources.append(source)
    return sources


def mount_source_path(source: str, root: Path) -> Path:
    """The host path a mount source resolves to, with the root this run exports.

    Compose's own resolution, for the one variable the deployment file uses. A source spelled with
    a substitution this does not know is refused rather than checked as a literal `${...}` path
    that exists nowhere — `docker compose config` would resolve it, and a wrong answer here is a
    preflight that passes while the mount fails.
    """
    resolved = source.replace(ROOT_SUBSTITUTION, str(root))
    unknown = UNKNOWN_SUBSTITUTION.search(resolved)
    if unknown:
        raise ValueError(
            f"mount source {source!r} uses {unknown.group(0)!r}, which this wrapper does not resolve; "
            "add it here or preflight it by hand"
        )
    return Path(resolved)


def missing_sources(sources: Sequence[str], root: Path) -> list[Path]:
    """The sources that are not on disk, resolved. `-e` and not `-f`: a directory is not a mount."""
    return [
        path
        for path in (mount_source_path(source, root) for source in sources)
        if not path.exists()
    ]


def hint_for(missing: Path) -> str | None:
    """What to say when a mount source is absent, when there is something specific to say.

    One case is worth naming: §6.7 accepts `unfixed-info.bin` + `locked-secret.bin` as well as the
    single file, so a corpus carrying only that pair would otherwise fail here with a message that
    reads as "you have no key".
    """
    if missing.name != KEY_FILE:
        return None
    directory = missing.parent
    if all((directory / name).is_file() for name in TWO_FILE_KEY):
        pair = " + ".join(TWO_FILE_KEY)
        return f"{directory} carries only the two-file spelling ({pair}); §6.7 accepts it, but this mount wants the single file"
    return None


# ── the host's paths ───────────────────────────────────────────────────────


def reference_root(env: Mapping[str, str] | None = None) -> Path:
    """`$REFERENCE_ROOT`, default `~/clone` — the same default `compose.yaml` substitutes."""
    source = os.environ if env is None else env
    value = source.get("REFERENCE_ROOT")
    return Path(value) if value else Path.home() / "clone"


def resolve_port(
    *,
    system: str,
    explicit: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Path | None, str]:
    """The host node for the device: `(node, "")`, or `(None, why not)`.

    macOS resolves by USB serial number through the committed `find_serial_port.py` — the CLI
    contract it advertises for a wrapper — and never by a `usbmodem*` glob, because this host
    carries a second such node (a Type-C AV adapter) and picking the wrong one is §10.3's trap.
    Linux prefers `/dev/serial/by-id` as §10.5 pins it, and falls back to the kernel node.
    """
    source = os.environ if env is None else env
    if explicit is not None:
        node = Path(explicit)
        return (node, "") if node.exists() else (None, f"--port {explicit} does not exist")

    if system == "darwin":
        pinned = source.get("NS2_PORT")
        if pinned:
            node = Path(pinned)
            return (node, "") if node.exists() else (None, f"$NS2_PORT={pinned} does not exist")
        return _resolve_port_macos()

    by_id = Path("/dev/serial/by-id")
    pinned = sorted(by_id.glob("usb-*if00")) if by_id.is_dir() else []
    if len(pinned) == 1:
        return pinned[0], ""
    if len(pinned) > 1:
        names = ", ".join(str(node) for node in pinned)
        return None, f"{len(pinned)} nodes match {by_id}/usb-*if00: {names}"
    kernel_node = Path(CONTAINER_DEVICE_PATH)
    if kernel_node.exists():
        return kernel_node, ""
    return None, f"no device: neither {by_id}/usb-*if00 nor {kernel_node} exists"


def _resolve_port_macos() -> tuple[Path | None, str]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_serial_port.py"), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        reason = result.stderr.strip() or f"find_serial_port.py exited {result.returncode}"
        return None, reason
    try:
        nodes = json.loads(result.stdout).get("nodes") or []
    except ValueError:
        return None, "find_serial_port.py did not print JSON"
    if len(nodes) != 1:
        return None, f"find_serial_port.py resolved {len(nodes)} nodes"
    return Path(nodes[0]), ""


# ── the one generated file ─────────────────────────────────────────────────


def _yaml_scalar(value: str) -> str:
    """A double-quoted YAML scalar.

    Quoted, and not plain: a colon inside would read as a mapping. Backslashes and quotes are
    refused rather than escaped, because a value that contains them is one the operator should
    look at.
    """
    if '"' in value or "\\" in value:
        raise ValueError(f"refusing to render a value with quotes or backslashes: {value!r}")
    return f'"{value}"'


def plan_run_devices(*, system: str, port: Path | None, no_device: bool) -> list[str] | None:
    """The `devices:` mapping this run needs, or `None` when the committed files already say it.

    `None` is the common case: the base file's `/dev/ttyACM0:/dev/ttyACM0` on a Linux host, and the
    committed macOS override's mapping onto that same container path. `[]` clears the mapping for
    `--no-device`; a list replaces it when the resolved node is not the kernel path the base names.
    """
    if no_device:
        return []
    if system == "darwin" or port is None:
        return None
    # `resolve()` and not the string: §10.5's pin is a symlink to the kernel node, and a host
    # where it points there is a host the committed file already describes.
    if port.resolve() == Path(CONTAINER_DEVICE_PATH).resolve():
        return None
    return [f"{port}:{CONTAINER_DEVICE_PATH}"]


def render_override(devices: Sequence[str]) -> str:
    """The generated compose override: `devices:` and nothing else.

    `!override` rather than the base file's per-target merge, because clearing a mapping is the
    point: a macOS host has no `/dev/ttyACM0`, and the base's entry would otherwise survive beside
    this one. The image and the three mounts are deliberately absent — they are
    `container/compose.yaml`'s own substitutions (`NS2_IMAGE`, `REFERENCE_ROOT`), and a wrapper
    that restated them would be the #42/#43 workaround growing back.
    """
    lines = [
        "# Generated by scripts/start_container.py — a run artifact, not a committed file.",
        "services:",
        "  controller:",
    ]
    if not devices:
        lines.append("    devices: !override []")
    else:
        lines.append("    devices: !override")
        lines.extend(f"      - {_yaml_scalar(entry)}" for entry in devices)
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Launch:
    """Everything a run needs, decided: the files, the environment, the override's text."""

    files: list[Path]
    env: dict[str, str]
    root: Path
    sources: list[str]
    node: Path | None = None
    devices: list[str] | None = None
    #: The override's YAML text, not a path: `command`'s `override_path` is where it gets written.
    override_text: str | None = None
    notes: list[str] = field(default_factory=list)

    def command(self, *arguments: str, override_path: str | None = None) -> list[str]:
        """The `docker compose` line. `override_path` is the generated file, appended last."""
        command = ["docker", "compose"]
        for path in self.files:
            command += ["-f", str(path)]
        if override_path is not None:
            command += ["-f", override_path]
        return command + list(arguments)


def plan_run(
    *,
    system: str,
    root: Path,
    sources: Sequence[str],
    port: Path | None,
    no_device: bool,
    image: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Launch:
    """Decide the compose files, the environment and the override text.

    Pure but for `$NS2_PORT` on macOS: the committed override reads that variable and *fails* when
    it is unset (`${NS2_PORT:?…}`), which is why it is added to the environment here rather than
    left to the caller's shell.
    """
    environment = dict(os.environ if env is None else env)
    environment["REFERENCE_ROOT"] = str(root)
    if image is not None:
        environment["NS2_IMAGE"] = image

    files = [BASE_COMPOSE]
    notes: list[str] = []
    no_device_note = "no device: the control link stays down and the container retries it"
    if no_device:
        notes.append(no_device_note)
    elif system == "darwin" and port is not None:
        files.append(MACOS_COMPOSE)
        environment["NS2_PORT"] = str(port)

    devices = plan_run_devices(system=system, port=port, no_device=no_device)
    override_text = render_override(devices) if devices is not None else None
    return Launch(
        files=files,
        env=environment,
        root=root,
        sources=list(sources),
        node=port,
        devices=devices,
        override_text=override_text,
        notes=notes,
    )


# ── running it ─────────────────────────────────────────────────────────────


def run_compose(launch: Launch, arguments: Sequence[str], *, machine: bool = False) -> int:
    """One `docker compose` invocation, with the override appended as the last `-f`.

    Last, and never first: the project name comes from the first file's directory (`container`),
    so the override cannot rename the project and orphan the container the documented line made.

    `machine` routes compose's own chatter (and the printed command line) to stderr, so `--json`
    leaves stdout carrying the JSON document and nothing else.
    """
    chatter = sys.stderr if machine else sys.stdout
    with tempfile.TemporaryDirectory(prefix="ns2-container-") as directory:
        if launch.override_text is None:
            command = launch.command(*arguments)
        else:
            override_path = Path(directory) / "compose.local.yaml"
            override_path.write_text(launch.override_text, encoding="utf-8")
            command = launch.command(*arguments, override_path=str(override_path))
        print(f"$ {' '.join(shlex.quote(part) for part in command)}", file=chatter)
        result = subprocess.run(
            command,
            env=launch.env,
            cwd=REPO_ROOT,
            check=False,
            stdout=sys.stderr if machine else None,
        )
        return result.returncode


def fetch_state(timeout: float = 2.0) -> dict | None:
    """The API's one state document, or `None` when nothing is answering yet."""
    url = f"http://localhost:{HTTP_PORT}/api/state"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")).get("state")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def control_link(state: Mapping[str, object]) -> str:
    """The control link's state, as `/api/state` reports it."""
    return str(_mapping(state.get("control")).get("link", "?"))


def wait_for_state(seconds: float, *, expect_link: bool) -> tuple[dict | None, bool]:
    """Poll until the API answers and the device is read back: `(state, ready)`.

    `ready` is false when the deadline passed with the device still answering nothing, and the
    state is whatever the last poll returned — so the caller can print it and refuse to call the
    run a success. The first HTTP answer is *not* readiness: the container serves while the link
    is still down and retries forever, which `--no-device` is a legitimate all-run example of.
    """
    deadline = time.monotonic() + seconds
    state: dict | None = None
    while True:
        state = fetch_state()
        if state is not None and (not expect_link or control_link(state) == "UP"):
            return state, True
        if time.monotonic() >= deadline:
            return state, False
        time.sleep(0.5)


def report(state: Mapping[str, object]) -> list[str]:
    """The four facts §8.7 puts on the connection screen, plus the library's, one line each."""
    control = _mapping(state.get("control"))
    firmware = _mapping(state.get("firmware"))
    console = _mapping(state.get("console"))
    features = [name for name, on in _mapping(firmware.get("features")).items() if on]
    macros = [entry for entry in _sequence(state.get("macros")) if _mapping(entry).get("status") == "ready"]
    figures = _sequence(state.get("figures"))
    bond = "bonded" if console.get("bonded") else "unbonded"
    return [
        f"control   {control.get('link', '?')}  {control.get('port', '?')} @{control.get('baud', '?')}",
        f"firmware  {firmware.get('fwVersion', '?')}  proto {firmware.get('protoVer', '?')}  "
        f"boot {firmware.get('bootId', '?')}  features {'|'.join(features) or 'none'}",
        f"console   {console.get('link', '?')} ({bond})",
        f"mode      {state.get('mode', '?')}",
        f"library   {len(macros)} macro(s) ready · {len(figures)} figure(s) · key {state.get('key', '?')}",
        f"UI        http://localhost:{HTTP_PORT}",
    ]


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, dict) else {}


def _sequence(value: object) -> Sequence:
    return value if isinstance(value, list) else []


# ── the command line ───────────────────────────────────────────────────────


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="start_container.py",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", help="the device's host node (default: resolve it)")
    parser.add_argument(
        "--no-device",
        action="store_true",
        help="run without the device: the control link stays down and the container retries it",
    )
    parser.add_argument(
        "--reference-root",
        type=Path,
        help="the corpus root to export as REFERENCE_ROOT (default: $REFERENCE_ROOT or ~/clone)",
    )
    parser.add_argument("--image", help="set NS2_IMAGE for this run (default: the compose file's)")
    parser.add_argument("--no-build", action="store_true", help="reuse the image as it is")
    parser.add_argument("--stop", action="store_true", help="docker compose down, then exit")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and run nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    args = parser.parse_args(argv)
    if args.json and (args.dry_run or args.stop):
        # `--json` carries the *run's* result, and neither of these has one: `--dry-run` prints a
        # plan, `--stop` prints the command it ran. Ignoring it would hand text to a caller that is
        # about to `json.load` the pipe, and the failure would surface at the far end of it, naming
        # nothing (#44's review nit).
        parser.error("--json is the run's result; --dry-run prints the plan and --stop has none")
    return args


def _fail(message: str, code: int) -> int:
    print(f"start_container: {message}", file=sys.stderr)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    system = platform.system().lower()
    if args.stop:
        return _stop()

    root = args.reference_root or reference_root()
    try:
        sources = mount_sources(BASE_COMPOSE.read_text(encoding="utf-8"))
    except OSError as failure:
        return _fail(f"cannot read {BASE_COMPOSE}: {failure}", 2)
    if not sources:
        return _fail(
            f"{BASE_COMPOSE} lists no host mounts; the preflight would pass vacuously. "
            "Check the file's `volumes:` block.",
            2,
        )

    missing = missing_sources(sources, root)
    if missing:
        for path in missing:
            print(f"missing mount source: {path}", file=sys.stderr)
            hint = hint_for(path)
            if hint:
                print(f"  {hint}", file=sys.stderr)
        return _fail(
            f"the corpus root is {root} (REFERENCE_ROOT). Docker would create a directory at each "
            "missing source and the container would report it as an empty library or a locked key.",
            2,
        )

    port: Path | None = None
    if not args.no_device:
        port, why = resolve_port(system=system, explicit=args.port)
        if port is None:
            return _fail(f"{why}. Plug the device in, pass --port, or pass --no-device.", 2)

    launch = plan_run(
        system=system,
        root=root,
        sources=sources,
        port=port,
        no_device=args.no_device,
        image=args.image,
    )

    if args.dry_run:
        _print_plan(launch, no_build=args.no_build)
        return 0

    for note in launch.notes:
        print(f"note: {note}", file=sys.stderr if args.json else sys.stdout)

    if shutil.which("docker") is None:
        return _fail("docker is not on PATH", 2)

    arguments = ["up", "-d"] if args.no_build else ["up", "-d", "--build"]
    if run_compose(launch, arguments, machine=args.json) != 0:
        return _fail("docker compose failed; see its output above", 3)

    state, ready = wait_for_state(READY_TIMEOUT_SECONDS, expect_link=not args.no_device)
    if state is None:
        print(
            f"start_container: the container is up but {HTTP_PORT} never answered in "
            f"{READY_TIMEOUT_SECONDS:.0f}s. Check `docker compose -f container/compose.yaml logs`.",
            file=sys.stderr,
        )
        return 4
    if not ready:
        for line in report(state):
            print(line)
        return _fail(
            f"the container is serving but the control link is still DOWN after "
            f"{READY_TIMEOUT_SECONDS:.0f}s. Another holder of the port is the usual cause — "
            f"`idf.py flash` (§10.2) — and the wrong node is the other (§10.3).",
            4,
        )

    if args.json:
        json.dump({"state": state, "notes": launch.notes}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    for line in report(state):
        print(line)
    return 0


def _stop() -> int:
    """`down` needs no mounts and no device: the base file alone names the same project."""
    command = ["docker", "compose", "-f", str(BASE_COMPOSE), "down"]
    print(f"$ {' '.join(shlex.quote(part) for part in command)}")
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def _print_plan(launch: Launch, *, no_build: bool) -> None:
    print(f"reference root  {launch.root}")
    for source in launch.sources:
        print(f"mount           {mount_source_path(source, launch.root)}")
    if launch.devices == []:
        print("device          (none: --no-device)")
    elif launch.node is not None:
        mapping = " (the committed mapping)" if launch.devices is None else " (generated override)"
        print(f"device          {launch.node} -> {CONTAINER_DEVICE_PATH}{mapping}")
    print(f"image           {launch.env.get('NS2_IMAGE', '(the compose file default)')}")
    print("files           " + "  ".join(str(path) for path in launch.files))
    if launch.override_text is None:
        print("override        (none: the committed files already name this run's devices)")
    else:
        print("override:")
        print(launch.override_text, end="")
    arguments = ["up", "-d"] if no_build else ["up", "-d", "--build"]
    printable = launch.command(
        *arguments,
        override_path="<generated compose.local.yaml>" if launch.override_text else None,
    )
    print(f"command         {' '.join(shlex.quote(part) for part in printable)}")


if __name__ == "__main__":
    sys.exit(main())
