"""Build a glibc 2.28 compatible bundle on a RHEL 8 x86_64 host/container."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request

REPO = Path(__file__).resolve().parents[2]
EDITABLE_PATHS = ("src/", "tests/", "templates/", "config/config.toml")
ASSETS = {
    "python": {
        "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20240224/cpython-3.11.8%2B20240224-x86_64-unknown-linux-gnu-install_only.tar.gz",
        "filename": "cpython-3.11.8-20240224-linux-x86_64.tar.gz",
        "sha256": "94e13d0e5ad417035b80580f3e893a72e094b0900d5d64e7e34ab08e95439987",
    },
    "git": {
        "url": "https://www.kernel.org/pub/software/scm/git/git-2.52.0.tar.gz",
        "filename": "git-2.52.0.tar.gz",
        "sha256": "6880cb1e737e26f81cf7db9957ab2b5bb2aa1490d87619480b860816e0c10c32",
    },
    "zlib": {
        "url": "https://www.zlib.net/fossils/zlib-1.3.1.tar.gz",
        "filename": "zlib-1.3.1.tar.gz",
        "sha256": "9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23",
    },
}


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bundle_files(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if any(name == editable or (editable.endswith("/") and name.startswith(editable))
               for editable in EDITABLE_PATHS):
            continue
        if path.is_symlink():
            files[name] = "symlink:" + os.readlink(path)
        elif path.is_file():
            files[name] = sha256(path)
    return files


def download(asset: dict, cache: Path) -> Path:
    destination = cache / asset["filename"]
    if not destination.exists():
        temporary = destination.with_suffix(destination.suffix + ".partial")
        print(f"Downloading {asset['filename']}", flush=True)
        with urllib.request.urlopen(asset["url"], timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        if sha256(temporary) != asset["sha256"]:
            raise ValueError(f"download checksum mismatch: {asset['filename']}")
        temporary.replace(destination)
    if sha256(destination) != asset["sha256"]:
        raise ValueError(f"cached checksum mismatch: {asset['filename']}")
    return destination


def extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as stream:
        stream.extractall(destination, filter="data")


def run(*command: str | Path, cwd: Path | None = None) -> None:
    subprocess.run([str(item) for item in command], cwd=cwd, check=True)


def verify_elf_compatibility(root: Path) -> dict:
    """Reject native binaries requiring a newer glibc than the target server."""
    checked = 0
    maximum = (0, 0)
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
        checked += 1
        versions = subprocess.check_output(["readelf", "-W", "--version-info", str(path)], text=True)
        for symbol in re.findall(r"\bName: (GLIBC_[\w.]+)", versions):
            requirement = symbol.removeprefix("GLIBC_")
            if not re.fullmatch(r"\d+(?:\.\d+)+", requirement):
                raise ValueError(f"unsupported glibc ABI {symbol}: {path.relative_to(root)}")
            version = tuple(map(int, requirement.split(".")))
            if version > (2, 28):
                raise ValueError(f"requires {symbol}, target is glibc 2.28: {path.relative_to(root)}")
            maximum = max(maximum, version)
    if not checked:
        raise ValueError("bundle contains no ELF binaries")
    return {"elf_files_checked": checked, "max_glibc_required": ".".join(map(str, maximum))}


def build(output: Path, cache: Path) -> Path:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("build inside the supplied RHEL 8 compatible container")
    release = platform.freedesktop_os_release()
    if release.get("VERSION_ID", "").split(".")[0] != "8" or not (
        release.get("ID") in {"rhel", "rocky", "almalinux", "centos"}
    ):
        raise ValueError("RHEL 8 compatible builder required for the bundled Git binary")
    if platform.libc_ver() != ("glibc", "2.28"):
        raise ValueError("builder must use glibc 2.28")
    for executable in ("gcc", "make", "cmp", "strip", "readelf"):
        if not shutil.which(executable):
            raise ValueError(f"builder requires {executable}; use deploy/offline/Containerfile")
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    wheels = json.loads((REPO / "deploy/offline/wheels.lock.json").read_text())
    inputs = {name: download(asset, cache) for name, asset in ASSETS.items()}
    wheel_paths = [download(asset, cache) for asset in wheels]
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    archive = output / f"log-analyzer-{version}-rhel8-x86_64-python3.11.8.tar.gz"
    if archive.exists():
        raise FileExistsError(f"use a new output directory: {archive}")

    with tempfile.TemporaryDirectory(prefix="log-analyzer-build-") as work_name:
        work = Path(work_name)
        root = work / f"log-analyzer-{version}"
        root.mkdir()
        for directory in ("app", "runtime/git/bin", "config/credentials", "config/certs",
                          "repos", "data", "data/input", "third-party/sources", "docs"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        (root / "config/credentials").chmod(0o700)
        (root / "data").chmod(0o700)
        extract(inputs["python"], root / "runtime")
        python = root / "runtime/python/bin/python3.11"
        run(python, "-I", "-c", "import sys; assert sys.version_info[:3] == (3,11,8)")
        run(python, "-I", "-m", "pip", "--disable-pip-version-check", "install", "--no-index",
            "--no-deps", "--no-compile", "--target", root / "app", *wheel_paths)
        for directory in ("src", "tests"):
            shutil.copytree(REPO / directory, root / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("pyproject.toml", "Dockerfile.test", "compose.yaml"):
            shutil.copy2(REPO / name, root / name)
        shutil.copytree(REPO / "templates", root / "templates")
        for document in (REPO / "docs").glob("*.md"):
            shutil.copy2(document, root / "docs" / document.name)
        shutil.copytree(REPO / "docs/examples", root / "docs/examples")
        if (REPO / "docs/diagrams").is_dir():
            shutil.copytree(REPO / "docs/diagrams", root / "docs/diagrams")
        shutil.copy2(REPO / "README.md", root / "PROJECT_README.md")
        shutil.copy2(REPO / "SYSTEM_DESIGN.md", root / "SYSTEM_DESIGN.md")
        for example in (REPO / "config").glob("*.example"):
            shutil.copy2(example, root / "config" / example.name)
        (root / "deploy").mkdir()
        for deploy_file in (REPO / "deploy").iterdir():
            if deploy_file.is_file():
                shutil.copy2(deploy_file, root / "deploy" / deploy_file.name)
        shutil.copytree(REPO / "deploy/offline", root / "deploy/offline",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(REPO / "docs/OFFLINE_DEPLOYMENT.md", root / "README.md")
        shutil.copy2(REPO / "config/file-onprem.toml.example", root / "config/config.toml")
        shutil.copy2(REPO / "config/onprem.toml.example", root / "config/onprem.toml.example")
        for name in ("log-analyzer", "launch.py"):
            shutil.copy2(REPO / "deploy/offline" / name, root / name)
        (root / "log-analyzer").chmod(0o755)

        # Build only Git's built-in local commands, with zlib linked statically.
        # No system Git, OpenSSL, libcurl, Perl or installation step is needed at runtime.
        extract(inputs["zlib"], work / "sources")
        zlib = work / "sources/zlib-1.3.1"
        run("sh", "configure", "--static", f"--prefix={work / 'zlib'}", cwd=zlib)
        run("make", "-j2", cwd=zlib)
        run("make", "install", cwd=zlib)
        extract(inputs["git"], work / "sources")
        git = work / "sources/git-2.52.0"
        run("make", "-j2", "git", "prefix=/usr/local", "RUNTIME_PREFIX=YesPlease",
            "NO_CURL=YesPlease", "NO_EXPAT=YesPlease",
            "NO_GETTEXT=YesPlease", "NO_OPENSSL=YesPlease", "NO_ICONV=YesPlease",
            "NO_PERL=YesPlease", "NO_TCLTK=YesPlease", "NO_PYTHON=YesPlease",
            f"ZLIB_PATH={work / 'zlib'}", cwd=git)
        shutil.copy2(git / "git", root / "runtime/git/bin/git")
        run("strip", root / "runtime/git/bin/git")
        linked = subprocess.check_output(["ldd", str(root / "runtime/git/bin/git")], text=True)
        glibc_libraries = {"libc.so.6", "libpthread.so.0", "librt.so.1", "libdl.so.2", "libm.so.6"}
        unexpected = [line for line in linked.splitlines() if "=>" in line
                      and (line.split()[0] not in glibc_libraries or "not found" in line)]
        if unexpected:
            raise ValueError(f"unexpected bundled Git libraries: {unexpected}")
        for name in ("git", "zlib"):
            shutil.copy2(inputs[name], root / "third-party/sources" / inputs[name].name)
        shutil.copy2(git / "COPYING", root / "third-party/GIT-COPYING")
        shutil.copy2(zlib / "README", root / "third-party/ZLIB-README")
        shutil.copy2(Path(__file__), root / "third-party/build.py")
        shutil.copy2(REPO / "deploy/offline/wheels.lock.json", root / "third-party/wheels.lock.json")
        compatibility = verify_elf_compatibility(root)
        manifest = {"application_version": version, "python": "3.11.8", "target": "rhel8-x86_64",
                    "minimum_glibc": "2.28", "elf_compatibility": compatibility,
                    "editable_paths": list(EDITABLE_PATHS),
                    "builder": {"os": release, "glibc": platform.libc_ver()},
                    "archives": ASSETS, "wheels": wheels, "files": bundle_files(root)}
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        run(root / "log-analyzer", "doctor", cwd=root)
        with tarfile.open(archive, "w:gz", compresslevel=6) as stream:
            stream.add(root, arcname=root.name)
    checksum = sha256(archive)
    archive.with_suffix(archive.suffix + ".sha256").write_text(f"{checksum}  {archive.name}\n")
    print(json.dumps({"archive": str(archive), "sha256": checksum}), flush=True)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO / "dist/offline")
    parser.add_argument("--cache", type=Path, default=REPO / "build/offline-cache")
    args = parser.parse_args()
    build(args.output.resolve(), args.cache.resolve())
