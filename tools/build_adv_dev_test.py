#!/usr/bin/env python3
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "build-adv-dev.sh"
COMPILER = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else REPO / "build/xsys35c"
ALD = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else REPO / "build/ald"
sys.argv[1:] = []


class BuildAdvDevTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="build-adv-dev-test.")
        self.root = Path(self.tempdir.name)
        self.dev = self.root / "dev"
        self.dev.mkdir()
        for name in ("xsystem35.exe", "鬼畜王GA.ALD", "鬼畜王GB.ALD", "鬼畜王WA.ALD"):
            (self.dev / name).write_bytes(("fixture:" + name).encode())

        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        fake_date = self.fake_bin / "date"
        fake_date.write_text('#!/bin/sh\nprintf "%s\\n" "$TEST_BUILD_TIMESTAMP"\n')
        fake_date.chmod(0o755)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_build(self, timestamp, compiler=COMPILER):
        env = os.environ.copy()
        env.update(
            {
                "ADV_DEV_DIR": str(self.dev),
                "XSYS35C_BIN": str(compiler),
                "TEST_BUILD_TIMESTAMP": timestamp,
                "PATH": str(self.fake_bin) + os.pathsep + env["PATH"],
            }
        )
        return subprocess.run(
            [str(SCRIPT)],
            cwd=REPO,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def snapshot(self):
        result = {}
        for path in sorted(self.dev.rglob("*")):
            relative = path.relative_to(self.dev).as_posix()
            result[relative] = None if path.is_dir() else path.read_bytes()
        return result

    def assert_no_staging_files(self):
        names = [path.name for path in self.dev.iterdir()]
        self.assertNotIn(".adv-build.lock", names)
        self.assertFalse(any(".new." in name for name in names))

    def test_archives_existing_sa_updates_config_and_is_idempotent(self):
        target = self.dev / "鬼畜王SA.ALD"
        target.write_bytes(b"old-sa")
        (self.dev / ".xsys35rc").write_bytes(
            b"# keep this comment\r\ncustom_option: yes\r\n"
            b"ttfont_mincho: old.ttf\r\nttfont_mincho: duplicate.ttf\r\n"
            b"ttfont_gothic: old.ttf\r\nsavedir: old-save\r\n"
        )

        first = self.run_build("20260814123456")
        self.assertEqual(0, first.returncode, first.stderr)
        archive1 = self.dev / "archive/鬼畜王SA_20260814123456.ald"
        self.assertEqual(b"old-sa", archive1.read_bytes())
        first_sa = target.read_bytes()

        config = (self.dev / ".xsys35rc").read_text()
        self.assertIn("# keep this comment\n", config)
        self.assertIn("custom_option: yes\n", config)
        self.assertEqual(1, len(re.findall(r"^ttfont_mincho:", config, re.MULTILINE)))
        self.assertEqual(1, len(re.findall(r"^ttfont_gothic:", config, re.MULTILINE)))
        self.assertEqual(1, len(re.findall(r"^savedir:", config, re.MULTILINE)))
        self.assertIn("ttfont_mincho: C:/Windows/Fonts/msyh.ttc", config)
        self.assertIn("ttfont_gothic: C:/Windows/Fonts/msyh.ttc", config)
        self.assertIn("savedir: save", config)
        self.assertNotIn("\r", config)

        note = (self.dev / "ADV-SCRIPT-BUILD.txt").read_text()
        self.assertIn("build_time_beijing=20260814123456", note)
        self.assertIn("previous_sa_sha256=" + hashlib.sha256(b"old-sa").hexdigest(), note)
        self.assertIn("archive=archive/鬼畜王SA_20260814123456.ald", note)
        subprocess.run(
            ["sha256sum", "--check", "--status", "SHA256SUMS.txt"],
            cwd=self.dev,
            check=True,
        )
        members = subprocess.run(
            [str(ALD), "list", str(target)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.splitlines()
        self.assertEqual(201, len(members))

        second = self.run_build("20260814123457")
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(config, (self.dev / ".xsys35rc").read_text())
        self.assertEqual(first_sa, (self.dev / "archive/鬼畜王SA_20260814123457.ald").read_bytes())
        self.assertEqual(2, len(list((self.dev / "archive").glob("*.ald"))))
        self.assert_no_staging_files()

    def test_first_deployment_does_not_create_archive(self):
        result = self.run_build("20260814130000")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.dev / "鬼畜王SA.ALD").is_file())
        self.assertFalse((self.dev / "archive").exists())
        note = (self.dev / "ADV-SCRIPT-BUILD.txt").read_text()
        self.assertIn("previous_sa_sha256=none", note)
        self.assertIn("archive=none", note)
        self.assert_no_staging_files()

    def test_missing_or_failing_compiler_leaves_dev_unchanged(self):
        (self.dev / "鬼畜王SA.ALD").write_bytes(b"current")
        before = self.snapshot()
        missing = self.run_build("20260814140000", self.root / "missing-xsys35c")
        self.assertNotEqual(0, missing.returncode)
        self.assertEqual(before, self.snapshot())

        failing = self.root / "failing-xsys35c"
        failing.write_text("#!/bin/sh\nexit 7\n")
        failing.chmod(0o755)
        failed = self.run_build("20260814140001", failing)
        self.assertNotEqual(0, failed.returncode)
        self.assertEqual(before, self.snapshot())
        self.assert_no_staging_files()

    def test_archive_collision_leaves_dev_unchanged(self):
        (self.dev / "鬼畜王SA.ALD").write_bytes(b"current")
        archive = self.dev / "archive"
        archive.mkdir()
        (archive / "鬼畜王SA_20260814150000.ald").write_bytes(b"existing-archive")
        before = self.snapshot()

        result = self.run_build("20260814150000")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("archive already exists", result.stderr)
        self.assertEqual(before, self.snapshot())
        self.assert_no_staging_files()

    def test_metadata_failure_rolls_back_sa_and_config(self):
        (self.dev / "鬼畜王SA.ALD").write_bytes(b"current")
        (self.dev / ".xsys35rc").write_text("custom_option: original\n")
        (self.dev / "ADV-SCRIPT-BUILD.txt").write_text("old-note\n")
        (self.dev / "SHA256SUMS.txt").write_text("old-sums\n")
        fake_sha256sum = self.fake_bin / "sha256sum"
        fake_sha256sum.write_text(
            '#!/bin/sh\n'
            'if [ "${1-}" = "--check" ]; then exit 1; fi\n'
            'exec /usr/bin/sha256sum "$@"\n'
        )
        fake_sha256sum.chmod(0o755)
        before = self.snapshot()

        result = self.run_build("20260814160000")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("restoring the previous development package", result.stderr)
        self.assertEqual(before, self.snapshot())
        self.assert_no_staging_files()


if __name__ == "__main__":
    unittest.main()
