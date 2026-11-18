"""The README's commands must work for a reader who follows it from the top.

The README says no install is needed, then its first command is
`nano-wallet selfcheck`, which does not exist until something installs the
console script. These laws pin both routes: the no-install form every
`nano-wallet` command has, and a one-step install named before the first
`nano-wallet` command is shown.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")
INSTALL = "pip install git+https://github.com/dhyabi2/nano-wallet-xno"


def readme():
    with open(README, encoding="utf-8") as f:
        return f.read()


class ReadmeCommands(unittest.TestCase):
    def test_install_step_comes_before_the_first_nano_wallet_command(self):
        text = readme()
        first_cmd = re.search(r"^\s*(?:\$ |\d+\. )?(?:\S+=\S+ )?nano-wallet \w", text, re.M)
        self.assertIsNotNone(first_cmd, "README shows no nano-wallet command")
        install = text.find(INSTALL)
        self.assertNotEqual(install, -1, "README never says how to get the nano-wallet command")
        self.assertLess(install, first_cmd.start(),
                        "the install line must come before the first nano-wallet command")

    def test_numbered_steps_use_the_no_install_form(self):
        # The four steps are what an agent copies; they must run from a plain clone,
        # like step 1 (python3 mcp_server.py) already does.
        steps = re.findall(r"^\d+\. (\S.*?)\s{2,}", readme(), re.M)
        self.assertTrue(steps, "README has no numbered steps")
        for step in steps:
            self.assertFalse(step.startswith("nano-wallet "),
                             "step needs an install that a clone does not give: " + step)
            if step.startswith("python3 "):
                script = step.split()[1]
                self.assertTrue(os.path.exists(os.path.join(ROOT, script)),
                                "step names a file the repo does not have: " + step)

    def test_no_install_selfcheck_passes_from_a_plain_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, "nano-wallet-xno")
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            env = {k: v for k, v in os.environ.items() if not k.startswith("NANO_WALLET")}
            run = subprocess.run(
                [sys.executable, "-m", "nano_wallet_xno", "selfcheck", "--profile", "receive-only"],
                cwd=copy, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("7/7 pass", run.stdout)


if __name__ == "__main__":
    unittest.main()


class ThePackageShipsEveryModuleItImports(unittest.TestCase):
    """Every module the package imports must be inside the package.

    Up to 1.3.0 the wheel was an explicit `py-modules` list, and a module left
    out of it was absent only from the built wheel - the installed command died
    with `ModuleNotFoundError` on an import the source tree resolved fine.
    Since 1.4.0 the wheel is the one package `nano_wallet_xno`, found by
    setuptools; what can still go wrong is a module that stays (or is added)
    at the repository root, where the suite imports it and the wheel does not
    have it. `work` was one import away from shipping that way.
    """

    PACKAGE = os.path.join(ROOT, "nano_wallet_xno")

    def test_every_local_module_imported_by_the_package_is_packaged(self):
        text = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
        self.assertNotIn("py-modules", text, "a flat module list ships modules outside the package")
        self.assertRegex(text, r'include\s*=\s*\[\s*"nano_wallet_xno\*"\s*\]')
        packaged = {f[:-3] for f in os.listdir(self.PACKAGE) if f.endswith(".py")}
        # Only the end-to-end drivers stay at the root; they are not shipped.
        loose = {f[:-3] for f in os.listdir(ROOT) if f.endswith(".py")}
        self.assertEqual(loose - {"e2e_check", "e2e_receive_only"}, set(),
                         "a module at the repository root is not in the wheel")
        for name in sorted(packaged):
            source = os.path.join(self.PACKAGE, name + ".py")
            with open(source, encoding="utf-8") as fh:
                text = fh.read()
            imported = set(re.findall(r"^\s*from\s+nano_wallet_xno\.(\w+)\s+import", text, re.M))
            for line in re.findall(r"^\s*from\s+nano_wallet_xno\s+import\s+(.+)$", text, re.M):
                imported.update(part.split()[0] for part in line.split("#")[0].split(","))
            # A bare `import x` of a sibling would only resolve from the source tree.
            bare = set(re.findall(r"^\s*import\s+([A-Za-z_]\w*)", text, re.M))
            bare |= set(re.findall(r"^\s*from\s+([A-Za-z_]\w*)\s+import\b", text, re.M))
            self.assertEqual(bare & (packaged - {"__init__", "__main__"}), set(),
                             "%s.py imports a sibling as a top-level module" % name)
            for dep in sorted(imported):
                self.assertIn(
                    dep, packaged,
                    "%s.py imports %r, which is not a module in nano_wallet_xno/ - "
                    "the wheel would install %s without it" % (name, dep, name))
