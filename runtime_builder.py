"""Assemble and verify pinned PHP-FPM runtime images; Python standard library only."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

from builder import BuildError, PLATFORMS, require, source_file, stream_command

ROOT = Path(__file__).resolve().parent
RECIPE_FILES = ("Dockerfile", "php-fpm.conf", "legacy.ini", "verify.php", "smoke.py")


def validate_profile(p):
    require(p["schema_version"] == 1 and p["kind"] == "runtime", "Invalid runtime schema")
    require(re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", p["id"]), "Invalid profile ID")
    require(p["status"] in ("experimental", "verified"), "Invalid runtime status")
    target, php = p["target"], p["php"]
    require(target["family"] == "debian", "Runtime backend supports Debian only")
    require(re.fullmatch(r"[a-z][a-z0-9]*", target["suite"]), "Invalid suite")
    require(PLATFORMS.get(target["architecture"]) == target["platform"], "Architecture mismatch")
    require(re.fullmatch(re.escape("debian:" + target["suite"]) +
                         r"(?:-slim)?(?:@sha256:[a-f0-9]{64})?", target["image"]),
            "Base image must match target suite")
    # This recipe intentionally handles PHP 5.6 only; newer branches have
    # different package names, INI settings and expected OOM behavior.
    require(php["version"] == "5.6.40" and php["module_api"] == "20131226",
            "This runtime recipe supports PHP 5.6.40 only")
    require(re.fullmatch(r"5\.6\.40-[A-Za-z0-9.+~_-]+", php["package_version"]),
            "An exact package version is required")
    require(p["repository"] == "https://packages.sury.org/php/", "Unsupported signed repository")
    require(p["keyring"]["filename"] == "debsuryorg-archive-keyring.deb", "Invalid keyring filename")
    items = [p["keyring"]] + p["packages"]
    names = []
    for item in items:
        name = item["filename"]
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._~-]{0,199}\.deb", name),
                "Unsafe package filename")
        require(re.fullmatch(r"[a-f0-9]{64}", item["sha256"]), "SHA256 is required")
        url = urlsplit(item["url"])
        require(url.scheme == "https" and url.hostname == "packages.sury.org"
                and not url.username and not url.password, "Invalid package origin")
        names.append(name)
    require(len(set(names)) == len(names), "Duplicate input filenames")
    required = {"php5.6-" + name for name in
                ("common", "json", "opcache", "readline", "cli", "fpm", "mysql", "mbstring", "gd", "xml")}
    require({item["name"] for item in p["packages"]} == required, "Incomplete runtime package set")
    for item in p["packages"]:
        expected = "{}_{}_{}.deb".format(item["name"], php["package_version"], target["architecture"])
        require(item["filename"] == expected, "Package version or architecture mismatch")
    checks = p["verification"]
    require({"mysql", "mbstring", "iconv", "gd", "dom", "session"}.issubset(checks["extensions"]),
            "Missing legacy extension checks")
    require(checks["oom_exit_code"] == 1, "Unexpected PHP 5.6 backport OOM exit code")
    require(set(p["security"]["cves"]) == {"CVE-2026-109618", "CVE-2026-109619"},
            "Missing security provenance")
    return p


def load_profile(profile_id, root=ROOT):
    require(re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", profile_id), "Invalid profile ID")
    try:
        p = json.loads((root / "runtimes" / "profiles" / (profile_id + ".json")).read_text())
        validate_profile(p)
    except (OSError, KeyError, ValueError, TypeError) as error:
        raise BuildError("Cannot load runtime profile: " + str(error)) from error
    require(p["id"] == profile_id, "Profile filename and ID differ")
    return p


def fingerprint(p, root=ROOT):
    digest = hashlib.sha256(json.dumps(p, sort_keys=True).encode())
    for name in RECIPE_FILES:
        digest.update((root / "runtimes" / "php56" / name).read_bytes())
    return digest.hexdigest()[:16]


def image_name(p, root=ROOT):
    return "php-runtime:{}-{}".format(p["id"], fingerprint(p, root))


def prepare(p, offline=False, root=ROOT):
    context = root / ".work" / "runtimes" / p["id"] / fingerprint(p, root) / "context"
    context.mkdir(parents=True, exist_ok=True)
    packages = context / "packages"
    packages.mkdir(exist_ok=True)
    sums = []
    manifest = []
    for item in [p["keyring"]] + p["packages"]:
        cached = source_file(item, root / ".cache" / "runtime-inputs", offline=offline)
        destination = context / item["filename"] if item is p["keyring"] else packages / item["filename"]
        shutil.copyfile(cached, destination)
        sums.append("{}  {}".format(item["sha256"], destination.relative_to(context)))
        if item is not p["keyring"]:
            manifest.append("{}\t{}\t{}".format(item["name"], p["php"]["package_version"],
                                               p["target"]["architecture"]))
    for name in RECIPE_FILES:
        shutil.copyfile(root / "runtimes" / "php56" / name, context / name)
    (context / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    (context / "package-inputs.tsv").write_text("\n".join(manifest) + "\n")
    (context / "profile.json").write_text(json.dumps(p, indent=2) + "\n")
    return context


def build_command(p, context, root=ROOT):
    command = ["docker", "build", "--platform", p["target"]["platform"],
               "--iidfile", str(context.parent / "image-id"), "--tag", image_name(p, root)]
    for key, value in {"BASE_IMAGE": p["target"]["image"], "TARGET_SUITE": p["target"]["suite"],
                       "TARGET_ARCH": p["target"]["architecture"]}.items():
        command += ["--build-arg", key + "=" + value]
    return command + [str(context)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("check")
    for name in ("prepare", "build", "image"):
        child = commands.add_parser(name)
        child.add_argument("profile")
        if name in ("prepare", "build"):
            child.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command in ("list", "check"):
            for path in sorted((ROOT / "runtimes" / "profiles").glob("*.json")):
                p = load_profile(path.stem)
                print("{}\t{}\t{}".format(p["id"], p["status"], p["target"]["platform"]))
            return 0
        p = load_profile(args.profile)
        if args.command == "image":
            print(image_name(p))
            return 0
        if args.command == "build":
            require(shutil.which("docker"), "Docker CLI is not installed")
        context = prepare(p, args.offline)
        if args.command == "prepare":
            print(context)
            return 0
        stream_command(build_command(p, context), context.parent / "build.log")
        image_id = (context.parent / "image-id").read_text().strip()
        require(re.fullmatch(r"sha256:[a-f0-9]{64}", image_id), "Invalid built image ID")
        # Reports are written only after the final stage, which depends on
        # the FPM, multipart upload, session-progress and OOM checks.
        output = ROOT / "out" / "runtimes" / p["id"] / fingerprint(p)
        output.mkdir(parents=True, exist_ok=True)
        report = {"profile": p, "fingerprint": fingerprint(p), "image": image_name(p),
                  "image_id": image_id, "finished_at": datetime.now(timezone.utc).isoformat(),
                  "runtime_checks": "passed", "application_checks": "not-run",
                  "full_security_audit": "not-run"}
        (output / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
        shutil.copyfile(context.parent / "build.log", output / "build.log")
        print("Runtime image: " + image_name(p))
        print("Report: " + str(output / "build-report.json"))
        return 0
    except (BuildError, OSError, subprocess.CalledProcessError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
