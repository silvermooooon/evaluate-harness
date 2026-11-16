"""Prepare local-only benchmark dependencies without reading API credentials."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
TAC_COMMIT = "98b68ef82a47690c316f42fddb05baafaab56851"
SQL_IMAGE = "ghcr.io/theagentcompany/ds-sql-exercise-image:1.0.0"
TASKS = ("admin-remove-pages-pdf", "ds-format-excel-sheets",
         "finance-expense-validation", "hr-resume-categorization")
IMAGES = [SQL_IMAGE, "ghcr.io/theagentcompany/servers-owncloud:1.0.0"] + [
    f"ghcr.io/theagentcompany/{task}-image:1.0.0" for task in TASKS
]
OPENCODE = ROOT / ".tools/opencode/v2.2.7/linux-x64/opencode"
ARCHIVE_SHA = "89a6cee227b54bc011179c528a5e63176032462b1a5e21723b616801cddc2ca7"
BINARY_SHA = "589253d36810f21217edd51545cd435b328cdc3cf4f451fbe0cce6f88afcf5e1"
DOWNLOAD = "https://github.com/anomalyco/opencode/releases/download/v2.2.7/opencode-linux-x64.tar.gz"
SNAPSHOT = ROOT / "vendor/agent-runtime-snapshot"
SDK_ROOT_FILES = ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "NOTICE")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-path", type=Path, help="Your local agent-runtime checkout; never reads its .env")
    parser.add_argument("--check", action="store_true", help="Read-only readiness report; no downloads or containers started")
    parser.add_argument("--skip-images", action="store_true", help="Skip office/ownCloud image pulls; dependency installation may still pull the SQL image")
    args = parser.parse_args()
    if args.check:
        return readiness(args)
    require_commands()
    subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL)
    source = validate_sdk(args.sdk_path or SNAPSHOT)
    for name in ("vendor", ".tools", "reports", "runs/setup"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    freeze_sdk(source)
    prepare_tac()
    prepare_opencode()
    prepare_python()
    prepare_manifests()
    if not args.skip_images:
        for image in IMAGES[1:]:
            ensure_image(image)
    print("Setup complete. Install host dependencies with: uv pip install -r requirements.txt")
    print("No API key was read and no model request was made. See README.md to start services.")
    return 0


def require_commands():
    missing = [name for name in ("docker", "git", "uv") if not shutil.which(name)]
    if missing:
        raise RuntimeError("Install required commands: " + ", ".join(missing))


def validate_sdk(path):
    path = path.expanduser().resolve()
    for relative in ("src/agent_runtime/__init__.py", "pyproject.toml", "uv.lock"):
        if not (path / relative).is_file():
            raise RuntimeError(f"Missing SDK file {path / relative}; pass --sdk-path to your checkout")
    return path


def sdk_files(path):
    files = {name: digest(path / name) for name in SDK_ROOT_FILES if (path / name).is_file()}
    for file in sorted((path / "src").rglob("*")):
        if file.is_file() and not any(part == "__pycache__" or part.startswith(".env") for part in file.relative_to(path).parts) and file.suffix != ".pyc":
            files[file.relative_to(path).as_posix()] = digest(file)
    return dict(sorted(files.items()))


def freeze_sdk(source):
    files = sdk_files(source)
    if SNAPSHOT.exists():
        if sdk_files(SNAPSHOT) != files:
            raise RuntimeError("Existing SDK snapshot differs; use a fresh evaluation checkout instead of overwriting an experiment")
    else:
        shutil.copytree(source / "src", SNAPSHOT / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env*"))
        for name in SDK_ROOT_FILES:
            if (source / name).is_file():
                shutil.copy2(source / name, SNAPSHOT / name)
    version = ROOT / "reports/sdk-version.json"
    if version.exists():
        if json.loads(version.read_text())["files"] != files:
            raise RuntimeError("Existing SDK version manifest does not match the snapshot; it was not overwritten")
    else:
        head = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True)
        dirty = subprocess.run(["git", "-C", str(source), "status", "--porcelain"], capture_output=True, text=True)
        version.write_text(json.dumps({
            "source": str(source), "head": head.stdout.strip() if head.returncode == 0 else None,
            "dirty": bool(dirty.stdout) if dirty.returncode == 0 else None,
            "files": files, "snapshot_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        }, indent=2) + "\n")


def prepare_manifests():
    tasks = ROOT / "reports/task-selection.json"
    if not tasks.exists():
        shutil.copy2(ROOT / "config/task-selection.json", tasks)
    experiment = ROOT / "reports/experiment-manifest.json"
    if not experiment.exists():
        sources = ("adapters/duet_agent.py", "adapters/opencode_agent.py", "scripts/run_trial.py",
                   "scripts/budget_proxy.py", "infra/controller.py", "infra/compose.yml")
        experiment.write_text(json.dumps({
            "protocol_version": 1, "model": "gpt-6-luna", "api": "openai-responses",
            "reasoning_effort": "medium", "opencode_version": "2.2.7", "upstream_commit": TAC_COMMIT,
            "sdk_snapshot_sha256": json.loads((ROOT / "reports/sdk-version.json").read_text())["snapshot_sha256"],
            "hashes": {name: digest(ROOT / name) for name in sources},
        }, indent=2) + "\n")


def prepare_tac():
    target = ROOT / "vendor/TheAgentCompany"
    if target.exists():
        head = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()
        if head != TAC_COMMIT:
            raise RuntimeError("Existing TheAgentCompany checkout differs from the pinned commit; it was not modified")
        return
    subprocess.run(["git", "init", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "remote", "add", "origin", "https://github.com/TheAgentCompany/TheAgentCompany.git"], check=True)
    subprocess.run(["git", "-C", str(target), "fetch", "--depth", "1", "origin", TAC_COMMIT], check=True)
    subprocess.run(["git", "-C", str(target), "checkout", "--detach", "FETCH_HEAD"], check=True)


def prepare_opencode():
    if OPENCODE.exists():
        if digest(OPENCODE) != BINARY_SHA:
            raise RuntimeError("Existing OpenCode binary does not match the pinned release; it was not overwritten")
        return
    archive = OPENCODE.parents[1] / "opencode-linux-x64.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        print("Downloading pinned OpenCode Linux x64 release...", flush=True)
        urllib.request.urlretrieve(DOWNLOAD, archive)
    if digest(archive) != ARCHIVE_SHA:
        raise RuntimeError(f"OpenCode archive checksum mismatch: {archive}")
    OPENCODE.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as bundle:
        # Only the known executable is extracted; the archive itself stays local.
        with bundle.extractfile("opencode") as source, OPENCODE.open("wb") as output:
            shutil.copyfileobj(source, output)
    OPENCODE.chmod(0o755)
    if digest(OPENCODE) != BINARY_SHA:
        raise RuntimeError("Extracted OpenCode checksum mismatch")


def ensure_image(image):
    result = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        subprocess.run(["docker", "pull", "--platform", "linux/amd64", image], check=True)


def prepare_python():
    requirements = ROOT / ".tools/sdk-requirements.txt"
    with tempfile.TemporaryDirectory(prefix="requirements-", dir=ROOT / ".tools") as temporary:
        exported = Path(temporary) / "requirements.txt"
        subprocess.run(["uv", "export", "--project", str(SNAPSHOT), "--locked", "--no-dev", "--no-emit-project",
                        "--format", "requirements-txt", "--output-file", str(exported)], check=True, stdout=subprocess.DEVNULL)
        if requirements.exists():
            normalized = lambda text: "\n".join(line for line in text.splitlines() if not line.startswith("#"))
            if normalized(requirements.read_text()) != normalized(exported.read_text()):
                raise RuntimeError("Existing SDK dependency export differs from the lock; it was not overwritten")
        else:
            shutil.copy2(exported, requirements)
    ensure_image(SQL_IMAGE)
    target = ROOT / ".tools/linux-python-locked"
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix="linux-python-", dir=ROOT / ".tools") as temporary:
            subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64",
                            "-v", f"{requirements}:/requirements.txt:ro", "-v", f"{temporary}:/packages",
                            SQL_IMAGE, "python_default", "-m", "pip", "install", "--require-hashes",
                            "--target", "/packages", "-r", "/requirements.txt"], check=True)
            Path(temporary).rename(target)
    validate_python(target, requirements)


def validate_python(target, requirements):
    code = """import importlib.metadata, pathlib
from packaging.requirements import Requirement
for line in pathlib.Path('/requirements.txt').read_text().splitlines():
    if not line or line.startswith(('#', ' ', '-')):
        continue
    requirement = Requirement(line.removesuffix('\\\\').strip())
    if requirement.marker and not requirement.marker.evaluate():
        continue
    actual = importlib.metadata.version(requirement.name)
    if actual not in requirement.specifier:
        raise RuntimeError(f'{requirement.name}: installed {actual}, expected {requirement.specifier}')
import agent_runtime
print('Locked Linux SDK dependencies verified')
"""
    subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64",
                    "-v", f"{requirements}:/requirements.txt:ro", "-v", f"{target}:/packages:ro",
                    "-v", f"{SNAPSHOT / 'src'}:/sdk:ro", "-e", "PYTHONPATH=/sdk:/packages",
                    SQL_IMAGE, "python_default", "-c", code], check=True)


def readiness(args):
    checks = {name: bool(shutil.which(name)) for name in ("docker", "git", "uv")}
    checks["host_httpx"] = importlib.util.find_spec("httpx") is not None
    checks["sdk_snapshot"] = (SNAPSHOT / "src/agent_runtime/__init__.py").is_file()
    checks["sdk_version_manifest"] = (ROOT / "reports/sdk-version.json").is_file()
    checks["linux_sdk_dependencies"] = (ROOT / ".tools/linux-python-locked/httpx").is_dir()
    checks["locked_dependency_export"] = (ROOT / ".tools/sdk-requirements.txt").is_file()
    checks["opencode_checksum"] = OPENCODE.is_file() and digest(OPENCODE) == BINARY_SHA
    tac = ROOT / "vendor/TheAgentCompany"
    checks["tac_checkout"] = False
    if checks["git"] and tac.is_dir():
        result = subprocess.run(["git", "-C", str(tac), "rev-parse", "HEAD"], capture_output=True, text=True)
        checks["tac_checkout"] = result.returncode == 0 and result.stdout.strip() == TAC_COMMIT
    if args.sdk_path and checks["sdk_snapshot"]:
        checks["source_matches_snapshot"] = sdk_files(validate_sdk(args.sdk_path)) == sdk_files(SNAPSHOT)
    if checks["docker"]:
        result = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        checks["docker_running"] = result.returncode == 0
        if checks["docker_running"]:
            for image in [SQL_IMAGE] if args.skip_images else IMAGES:
                result = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                checks[image] = result.returncode == 0
    print(json.dumps({"ready": all(checks.values()), "checks": checks,
                      "note": "Read-only file/image checks; normal setup also validates locked package versions in the original Linux image."}, indent=2))
    return 0 if all(checks.values()) else 1


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        sys.exit(1)
