"""Install the generated workspace's npm dependencies once per dependency set.

Step 10 and step 12 both prepare the same workspace. Re-running ``npm install`` when
nothing changed only spends wall-clock time, so the declared dependency set and the
lockfile are hashed and recorded. An installation is reused only when ``node_modules``,
the recorded signature and every tool the compiler invokes still exist.
"""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
from threading import Thread

from infrastructure.utils.files import save_json
from infrastructure.utils.processes import npm_command, start_process, stop_process

MARKER = ".arc/dependencies.json"
# Tools the compiler invokes directly; a partial tree must not count as installed.
REQUIRED = ("typescript/bin/tsc", "@playwright/test/cli.js", "vite/bin/vite.js",
    ".package-lock.json")
DECLARED = ("dependencies", "devDependencies", "optionalDependencies")


def dependency_signature(output_dir: Path) -> str | None:
    """Hash the declared dependency set; formatting and scripts do not affect installation."""
    package = output_dir / "package.json"
    if not package.is_file():
        return None
    try:
        manifest = json.loads(package.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    lock = output_dir / "package-lock.json"
    parts = [json.dumps({key: manifest.get(key) for key in DECLARED}, sort_keys=True),
        lock.read_text(encoding="utf-8-sig") if lock.is_file() else ""]
    return sha256("\n".join(parts).encode("utf-8")).hexdigest()


def dependencies_ready(output_dir: Path) -> bool:
    """Reuse an installation only when the tree, the signature and the needed tools match."""
    marker = output_dir / MARKER
    if not (output_dir / "node_modules").is_dir() or not marker.is_file():
        return False
    if not all((output_dir / "node_modules" / relative).exists() for relative in REQUIRED):
        return False
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return recorded.get("signature") == dependency_signature(output_dir)


def install_dependencies(output_dir: Path) -> None:
    """Install once per dependency set: stream npm output, and fail on timeout or nonzero exit."""
    if not (output_dir / "package.json").is_file():
        raise ValueError("Run 00_project_initialization.py before installing project dependencies")
    if dependencies_ready(output_dir):
        print(f"Dependencies already installed in {output_dir}: node_modules matches package files; "
            "skipping npm install", flush=True)
        return
    signature = dependency_signature(output_dir)
    command = npm_command()
    print(f"Installing dependencies in {output_dir}: npm install --include=dev --no-audit --no-fund", flush=True)
    with start_process(
        [*command, "install", "--include=dev", "--no-audit", "--no-fund"],
        cwd=output_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    ) as process:
        # Forward child output through Python so the console log also receives it.
        def forward_output():
            """Forward npm output through Python so both the terminal and compiler.log receive it."""
            for line in process.stdout:
                print(line, end="", flush=True)

        reader = Thread(target=forward_output, daemon=True)
        reader.start()
        try:
            process.wait(timeout=600)
        except subprocess.TimeoutExpired as error:
            stop_process(process)
            raise RuntimeError("npm install timed out after 600 seconds") from error
        except BaseException:
            stop_process(process)
            raise
        finally:
            reader.join()
        if process.returncode:
            raise RuntimeError(f"npm install failed with exit code {process.returncode}; see the npm output above")
    save_json(output_dir / MARKER, {"signature": signature})

