import hashlib
import json
import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SOURCE = PROJECT_ROOT / "examples/dbt_project/models/orders_daily.sql"
EXAMPLE_PROPOSED = (
    PROJECT_ROOT / "examples/generated/proposed/models/orders_daily.sql"
)
EXAMPLE_MANIFEST = PROJECT_ROOT / "examples/generated/manifest.json"
EXAMPLE_PATCH = (
    PROJECT_ROOT / "examples/generated/patches/models/orders_daily.sql.patch"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GeneratedExampleTests(unittest.TestCase):
    def test_manifest_hashes_match_checked_in_models(self):
        manifest = json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
        patch = manifest["downstreamPatches"][0]

        self.assertEqual(sha256(EXAMPLE_SOURCE), patch["sourceSha256"])
        self.assertEqual(sha256(EXAMPLE_PROPOSED), patch["proposedSha256"])
        self.assertEqual(patch["preservedOutputAliases"], 1)
        self.assertEqual(patch["remainingExecutableOldIdentifiers"], 1)
        self.assertEqual(manifest["downstreamColumnMode"], "preserve")
        self.assertFalse(manifest["safety"]["targetRepositoryModified"])
        self.assertFalse(manifest["safety"]["sqlExecuted"])
        self.assertFalse(manifest["safety"]["downstreamOutputContractMayChange"])

    def test_checked_in_patch_applies_to_example_project(self):
        completed = subprocess.run(
            [
                "git",
                "apply",
                "--check",
                "--directory=examples/dbt_project",
                str(EXAMPLE_PATCH.relative_to(PROJECT_ROOT)),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
