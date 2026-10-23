import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import builder


PROFILE_ID = "debian10-php8.3.36-amd64"


class Response(io.BytesIO):
    def geturl(self):
        return "https://example.org/source.tar.xz"


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = builder.load_profile(PROFILE_ID)

    def test_target_architecture_cannot_disagree_with_docker_platform(self):
        self.profile["target"]["platform"] = "linux/arm64"
        with self.assertRaisesRegex(builder.BuildError, "architecture and Docker platform"):
            builder.validate_profile(self.profile)

    def test_suite_cannot_use_a_different_distribution_image(self):
        self.profile["target"]["image"] = "debian:bookworm"
        with self.assertRaisesRegex(builder.BuildError, "Base image"):
            builder.validate_profile(self.profile)

    def test_profile_id_cannot_escape_profile_directory(self):
        with self.assertRaises(builder.BuildError):
            builder.load_profile("../profile")

    def test_sources_require_https(self):
        self.profile["source"]["files"][0]["url"] = "http://example.org/source.dsc"
        with self.assertRaisesRegex(builder.BuildError, "HTTPS"):
            builder.validate_profile(self.profile)

    def test_checksum_is_mandatory(self):
        self.profile["source"]["files"][0]["sha256"] = ""
        with self.assertRaisesRegex(builder.BuildError, "SHA256"):
            builder.validate_profile(self.profile)

    def test_source_filenames_cannot_escape_context(self):
        self.profile["source"]["files"][0]["filename"] = "../../Dockerfile"
        with self.assertRaisesRegex(builder.BuildError, "filename"):
            builder.validate_profile(self.profile)

    def test_apt_signatures_cannot_be_disabled_by_profile(self):
        self.profile["target"]["apt_sources"] = ["deb [trusted=yes] http://example.org buster main"]
        with self.assertRaisesRegex(builder.BuildError, "signature"):
            builder.validate_profile(self.profile)

    def test_test_paths_are_confined_to_upstream_test_directories(self):
        self.profile["verification"]["test_paths"] = ["tests/../../other"]
        with self.assertRaisesRegex(builder.BuildError, "test path"):
            builder.validate_profile(self.profile)

    @unittest.skipUnless(shutil.which("dpkg"), "dpkg is unavailable")
    def test_local_package_is_an_upgrade_from_the_deployed_version(self):
        subprocess.run(["dpkg", "--compare-versions", self.profile["php"]["package_version"],
                        "gt", "8.3.8-2+0~20240608.35+debian10~1.gbp4d7dbc"], check=True)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = b"verified upstream bytes"
        self.item = {"filename": "source.orig.tar.xz", "url": "https://example.org/source.tar.xz",
                     "sha256": hashlib.sha256(self.data).hexdigest()}

    def test_a_valid_local_archive_can_be_imported_without_network(self):
        sources = self.root / "sources"
        sources.mkdir()
        (sources / self.item["filename"]).write_bytes(self.data)
        with mock.patch("builder.urllib.request.urlopen") as network:
            destination = builder.source_file(self.item, self.root / "cache", sources, offline=True)
        self.assertEqual(destination.read_bytes(), self.data)
        network.assert_not_called()

    def test_a_changed_local_archive_is_rejected(self):
        sources = self.root / "sources"
        sources.mkdir()
        (sources / self.item["filename"]).write_bytes(b"changed")
        with self.assertRaisesRegex(builder.BuildError, "checksum mismatch"):
            builder.source_file(self.item, self.root / "cache", sources, offline=True)

    def test_a_corrupted_cached_archive_is_rejected_offline(self):
        cached = self.root / "cache" / self.item["sha256"] / self.item["filename"]
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"changed")
        with self.assertRaisesRegex(builder.BuildError, "unavailable offline"):
            builder.source_file(self.item, self.root / "cache", offline=True)

    def test_an_unexpected_download_does_not_enter_the_verified_cache(self):
        cache = self.root / "cache"
        with mock.patch("builder.urllib.request.urlopen", return_value=Response(b"changed")):
            with self.assertRaisesRegex(builder.BuildError, "checksum mismatch"):
                builder.source_file(self.item, cache)
        self.assertEqual(list(cache.rglob("*.*")), [])

    def test_a_verified_download_can_be_reused_offline(self):
        cache = self.root / "cache"
        with mock.patch("builder.urllib.request.urlopen", return_value=Response(self.data)):
            original = builder.source_file(self.item, cache)
        with mock.patch("builder.urllib.request.urlopen") as network:
            reused = builder.source_file(self.item, cache, offline=True)
        self.assertEqual(original, reused)
        network.assert_not_called()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = builder.load_profile(PROFILE_ID)
        for folder in ("docker", "scripts"):
            shutil.copytree(builder.ROOT / folder, self.root / folder)

    def test_offline_prepare_preserves_the_declared_inputs_and_module_order(self):
        sources = self.root / "import"
        sources.mkdir()
        for index, item in enumerate(self.profile["source"]["files"]):
            data = ("source {}".format(index)).encode()
            item["sha256"] = hashlib.sha256(data).hexdigest()
            (sources / item["filename"]).write_bytes(data)
        context = builder.prepare(self.profile, sources, offline=True, root=self.root)
        copied = json.loads((context / "profile.json").read_text())
        self.assertEqual(copied, self.profile)
        self.assertEqual((context / "extensions.txt").read_text().splitlines(),
                         self.profile["verification"]["extensions"])
        for item in self.profile["source"]["files"]:
            self.assertEqual(builder.sha256(context / "sources" / item["filename"]), item["sha256"])

    def test_modified_verification_code_selects_a_different_image(self):
        original = builder.image_name(self.profile, self.root)
        path = self.root / "scripts" / "verify-build.sh"
        path.write_text(path.read_text() + "\necho additional-check\n")
        self.assertNotEqual(original, builder.image_name(self.profile, self.root))

    def test_docker_build_receives_the_target_and_exact_package_version(self):
        command = builder.build_command(self.profile, self.root / "context", 2, self.root)
        self.assertEqual(command[command.index("--platform") + 1], "linux/amd64")
        self.assertIn("PACKAGE_VERSION=8.3.36-1~deb10.local1", command)
        self.assertIn("TARGET_SUITE=buster", command)

    def test_failed_build_does_not_export_previous_packages(self):
        with mock.patch("builder.prepare", return_value=self.root / "context"), \
                mock.patch("builder.stream_command", side_effect=builder.BuildError("build failed")), \
                mock.patch("builder.export") as export, mock.patch("sys.stderr", new_callable=io.StringIO):
            result = builder.main(["build", PROFILE_ID, "--offline"])
        self.assertEqual(result, 1)
        export.assert_not_called()

    def test_export_cleans_up_its_container_when_copy_fails(self):
        container_id = "a" * 64
        def run(command, **kwargs):
            if command[:2] == ["docker", "cp"]:
                raise subprocess.CalledProcessError(1, command)
        with mock.patch("builder.subprocess.check_output", return_value=container_id), \
                mock.patch("builder.subprocess.run", side_effect=run) as command:
            with self.assertRaises(subprocess.CalledProcessError):
                builder.export(self.profile, self.root)
        command.assert_any_call(["docker", "rm", container_id], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
