import hashlib
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from builder import BuildError
import runtime_builder as runtime


PROFILE = "debian13-php5.6.40-fpm-amd64"


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.profile = runtime.load_profile(PROFILE)

    def test_packages_cannot_be_substituted_with_an_older_revision(self):
        self.profile["packages"][0]["filename"] = self.profile["packages"][0]["filename"].replace("-105+", "-103+")
        with self.assertRaisesRegex(BuildError, "version or architecture"):
            runtime.validate_profile(self.profile)

    def test_target_image_cannot_disagree_with_suite(self):
        self.profile["target"]["image"] = "debian:buster"
        with self.assertRaisesRegex(BuildError, "Base image"):
            runtime.validate_profile(self.profile)

    def test_target_platform_cannot_disagree_with_architecture(self):
        self.profile["target"]["platform"] = "linux/arm64"
        with self.assertRaisesRegex(BuildError, "Architecture"):
            runtime.validate_profile(self.profile)

    def test_keyring_checksum_is_mandatory(self):
        self.profile["keyring"]["sha256"] = ""
        with self.assertRaisesRegex(BuildError, "SHA256"):
            runtime.validate_profile(self.profile)

    def test_mysql_package_cannot_be_omitted(self):
        self.profile["packages"] = [p for p in self.profile["packages"] if p["name"] != "php5.6-mysql"]
        with self.assertRaisesRegex(BuildError, "package set"):
            runtime.validate_profile(self.profile)

    def test_prepare_rejects_a_tampered_package_in_offline_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(runtime.ROOT / "runtimes", root / "runtimes")
            for item in [self.profile["keyring"]] + self.profile["packages"]:
                data = item["filename"].encode()
                item["sha256"] = hashlib.sha256(data).hexdigest()
                cached = root / ".cache/runtime-inputs" / item["sha256"] / item["filename"]
                cached.parent.mkdir(parents=True)
                cached.write_bytes(data)
            victim = self.profile["packages"][-1]
            (root / ".cache/runtime-inputs" / victim["sha256"] / victim["filename"]).write_bytes(b"changed")
            with self.assertRaisesRegex(BuildError, "unavailable offline"):
                runtime.prepare(self.profile, offline=True, root=root)

    def test_verification_changes_cannot_reuse_the_previous_image_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(runtime.ROOT / "runtimes", root / "runtimes")
            original = runtime.image_name(self.profile, root)
            path = root / "runtimes/php56/smoke.py"
            path.write_text(path.read_text() + "\n# new regression check\n")
            self.assertNotEqual(original, runtime.image_name(self.profile, root))

    def test_docker_gets_the_declared_target(self):
        command = runtime.build_command(self.profile, Path("/tmp/context"))
        self.assertEqual(command[command.index("--platform") + 1], "linux/amd64")
        self.assertIn("TARGET_SUITE=trixie", command)
        self.assertIn("TARGET_ARCH=amd64", command)

    def test_failed_docker_build_does_not_write_a_success_report(self):
        with tempfile.TemporaryDirectory() as directory:
            context = Path(directory) / "context"
            context.mkdir()
            shutil.copytree(runtime.ROOT / "runtimes", Path(directory) / "runtimes")
            with mock.patch.object(runtime, "prepare", return_value=context), \
                    mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/docker"), \
                    mock.patch.object(runtime, "stream_command", side_effect=BuildError("failed")) as build, \
                    mock.patch.object(runtime, "ROOT", Path(directory)), \
                    mock.patch.object(runtime, "load_profile", return_value=self.profile), \
                    mock.patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(runtime.main(["build", PROFILE]), 1)
                build.assert_called_once()
            self.assertFalse((Path(directory) / "out").exists())


if __name__ == "__main__":
    unittest.main()
