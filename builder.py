"""Profile-driven native PHP Debian package builds; Python standard library only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
PLATFORMS = {"amd64": "linux/amd64", "arm64": "linux/arm64", "armhf": "linux/arm/v7"}
MAX_SOURCE_BYTES = 256 * 1024 * 1024


class BuildError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise BuildError(message)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_profile(profile_id, root=ROOT):
    require(isinstance(profile_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", profile_id),
            "Invalid profile ID")
    path = root / "profiles" / (profile_id + ".json")
    try:
        profile = json.loads(path.read_text())
        validate_profile(profile)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise BuildError("Cannot load profile {}: {}".format(profile_id, error)) from error
    require(profile["id"] == profile_id, "Profile filename and ID differ")
    return profile


def validate_profile(p):
    require(isinstance(p, dict) and p.get("schema_version") == 1, "Unsupported profile schema")
    require(re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", p["id"]), "Invalid profile ID")
    require(p["status"] in ("experimental", "verified"), "Invalid profile status")
    php, target, source = p["php"], p["target"], p["source"]
    require(re.fullmatch(r"\d+\.\d+\.\d+", php["version"]), "PHP version must be explicit")
    require(re.fullmatch(r"\d{8}", php["module_api"]), "Invalid module API")
    require(re.fullmatch(re.escape(php["version"]) + r"-[A-Za-z0-9.+:~\-]+",
                         php["package_version"]), "Invalid local package version")
    require(target["family"] == "debian", "This backend currently supports Debian only")
    require(re.fullmatch(r"[a-z][a-z0-9]*", target["suite"]), "Invalid Debian suite")
    require(PLATFORMS.get(target["architecture"]) == target["platform"],
            "Debian architecture and Docker platform differ")
    expected_image = "debian:" + target["suite"]
    require(re.fullmatch(re.escape(expected_image) + r"(?:-slim)?(?:@sha256:[a-f0-9]{64})?",
                         target["image"]), "Base image must match the Debian suite")
    require(isinstance(target["apt_sources"], list) and target["apt_sources"], "Missing APT sources")
    for line in target["apt_sources"]:
        require(isinstance(line, str) and line.startswith("deb ") and "\n" not in line
                and "\r" not in line, "Invalid APT source line")
        require(not re.search(r"(?:trusted|allow-insecure|allow-weak)\s*=\s*yes", line, re.I),
                "APT signature verification must remain enabled")
    require(isinstance(source["files"], list) and len(source["files"]) == 3,
            "A source profile must contain the dsc, orig and debian archives")
    names = []
    for item in source["files"]:
        name = item["filename"]
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._~\-]{0,199}", name),
                "Unsafe source filename")
        require(re.fullmatch(r"[a-f0-9]{64}", item["sha256"]), "Missing or invalid SHA256")
        url = urllib.parse.urlsplit(item["url"])
        require(url.scheme == "https" and url.hostname and not url.username and not url.password,
                "Source URLs must use HTTPS without credentials")
        names.append(name)
    require(len(set(names)) == len(names), "Duplicate source filenames")
    require(source["dsc"] in names and source["dsc"].endswith(".dsc"), "Missing source descriptor")
    require(sum(".orig.tar." in name for name in names) == 1
            and sum(".debian.tar." in name for name in names) == 1, "Invalid source archive set")
    checks = p["verification"]
    require(isinstance(checks["extensions"], list) and checks["extensions"], "Missing extension checks")
    for module in checks["extensions"] + checks["zend_extensions"]:
        require(re.fullmatch(r"[a-z][a-z0-9_]*", module), "Invalid extension name")
    for test_path in checks["test_paths"]:
        require(re.fullmatch(r"(?:tests|Zend/tests|ext/[a-z0-9_]+/tests)(?:/[A-Za-z0-9_.-]+)*",
                             test_path) and ".." not in Path(test_path).parts, "Unsafe test path")
    require(checks["test_paths"], "At least one upstream test path is required")
    return p


def source_file(item, cache, sources_dir=None, offline=False):
    destination = cache / item["sha256"] / item["filename"]
    if destination.is_file() and sha256(destination) == item["sha256"]:
        return destination
    local = sources_dir / item["filename"] if sources_dir is not None else None
    if local is not None and local.is_file():
        require(sha256(local) == item["sha256"], "Local source checksum mismatch: " + local.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, destination)
        return destination
    require(not offline, "Source is unavailable offline: " + item["filename"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            with urllib.request.urlopen(item["url"], timeout=60) as response:
                require(urllib.parse.urlsplit(response.geturl()).scheme == "https",
                        "Source redirect downgraded HTTPS")
                size = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    require(size <= MAX_SOURCE_BYTES, "Source exceeds the download size limit")
                    output.write(chunk)
        require(sha256(temporary) == item["sha256"], "Downloaded checksum mismatch: " + item["filename"])
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def fingerprint(profile, root=ROOT):
    digest = hashlib.sha256(json.dumps(profile, sort_keys=True).encode())
    for path in (root / "docker" / "Dockerfile", root / "scripts" / "verify-build.sh"):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def image_name(profile, root=ROOT):
    return "php-package-builder:{}-{}".format(profile["id"], fingerprint(profile, root))


def prepare(profile, sources_dir=None, offline=False, root=ROOT):
    context = root / ".work" / profile["id"] / fingerprint(profile, root) / "context"
    sources = context / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    sums = []
    for item in profile["source"]["files"]:
        cached = source_file(item, root / ".cache" / "sources", sources_dir, offline)
        shutil.copyfile(cached, sources / item["filename"])
        sums.append("{}  {}".format(item["sha256"], item["filename"]))
    (sources / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    (context / "apt-sources.list").write_text("\n".join(profile["target"]["apt_sources"]) + "\n")
    (context / "extensions.txt").write_text("\n".join(profile["verification"]["extensions"]) + "\n")
    (context / "zend-extensions.txt").write_text("\n".join(profile["verification"]["zend_extensions"]) + "\n")
    (context / "test-paths.txt").write_text("\n".join(profile["verification"]["test_paths"]) + "\n")
    (context / "profile.json").write_text(json.dumps(profile, indent=2) + "\n")
    shutil.copyfile(root / "docker" / "Dockerfile", context / "Dockerfile")
    shutil.copyfile(root / "scripts" / "verify-build.sh", context / "verify-build.sh")
    return context


def build_command(profile, context, jobs, root=ROOT):
    require(1 <= jobs <= 128, "Build jobs must be between 1 and 128")
    args = {"BASE_IMAGE": profile["target"]["image"],
            "TARGET_SUITE": profile["target"]["suite"],
            "TARGET_ARCH": profile["target"]["architecture"],
            "PHP_VERSION": profile["php"]["version"],
            "MODULE_API": profile["php"]["module_api"],
            "PACKAGE_VERSION": profile["php"]["package_version"],
            "SOURCE_DSC": profile["source"]["dsc"], "BUILD_JOBS": str(jobs)}
    command = ["docker", "build", "--platform", profile["target"]["platform"],
               "--iidfile", str(context.parent / "image-id"), "--tag", image_name(profile, root)]
    for key, value in args.items():
        command.extend(["--build-arg", key + "=" + value])
    return command + [str(context)]


def stream_command(command, log_path):
    with log_path.open("w") as log:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace") as process:
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
            code = process.wait()
    require(code == 0, "Docker build failed ({}); log: {}".format(code, log_path))


def export(profile, root=ROOT):
    output = root / "out" / profile["id"] / fingerprint(profile, root)
    output.mkdir(parents=True, exist_ok=True)
    # docker cp transfers through the Docker API; no host directories are
    # mounted and the container is never started.
    container = subprocess.check_output(["docker", "create", "--platform", profile["target"]["platform"],
                                         "--network", "none", image_name(profile, root)], text=True).strip()
    require(re.fullmatch(r"[a-f0-9]{12,64}", container), "Unexpected Docker container ID")
    try:
        subprocess.run(["docker", "cp", container + ":/artifacts/.", str(output)], check=True)
    finally:
        subprocess.run(["docker", "rm", container], check=True, stdout=subprocess.DEVNULL)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List explicit build profiles")
    commands.add_parser("check", help="Validate profiles and shell syntax, without Docker")
    for name in ("prepare", "build", "export"):
        child = commands.add_parser(name)
        child.add_argument("profile")
        if name in ("prepare", "build"):
            child.add_argument("--sources-dir", type=Path)
            child.add_argument("--offline", action="store_true")
        if name == "build":
            child.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args(argv)
    try:
        if args.command in ("list", "check"):
            for path in sorted((ROOT / "profiles").glob("*.json")):
                profile = load_profile(path.stem)
                print("{}\t{}\t{}".format(profile["id"], profile["status"], profile["target"]["platform"]))
            if args.command == "check":
                subprocess.run(["bash", "-n", str(ROOT / "scripts" / "verify-build.sh")], check=True)
            return 0
        profile = load_profile(args.profile)
        if args.command == "export":
            print(export(profile))
            return 0
        if args.command == "build":
            require(1 <= args.jobs <= 128, "Build jobs must be between 1 and 128")
        context = prepare(profile, args.sources_dir, args.offline)
        if args.command == "prepare":
            print(context)
            return 0
        log_path = context.parent / "build.log"
        command = build_command(profile, context, args.jobs)
        stream_command(command, log_path)
        output = export(profile)
        shutil.copyfile(log_path, output / "build.log")
        report = {"profile": profile, "fingerprint": fingerprint(profile),
                  "image": image_name(profile), "image_id": (context.parent / "image-id").read_text().strip(),
                  "finished_at": datetime.now(timezone.utc).isoformat(), "build_jobs": args.jobs,
                  "container_checks": "passed", "application_checks": "not-run",
                  "pecl_checks": "not-run"}
        (output / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
        print("Packages and report: " + str(output))
        return 0
    except (BuildError, OSError, subprocess.CalledProcessError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
