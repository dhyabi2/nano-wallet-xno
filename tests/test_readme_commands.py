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
                [sys.executable, "cli.py", "selfcheck", "--profile", "receive-only"],
                cwd=copy, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("7/7 pass", run.stdout)


if __name__ == "__main__":
    unittest.main()


class ThePackageShipsEveryModuleItImports(unittest.TestCase):
    """`py-modules` is an explicit list, so a new module is invisible to it.

    The suite runs from the source tree, where every sibling `.py` imports
    whether or not it is packaged. A module left out of `py-modules` is
    therefore absent only from the built wheel - the installed command dies
    with `ModuleNotFoundError` on an import the source tree resolves fine, and
    nothing in a green suite can see it. `work` was one import away from
    shipping that way.
    """

    def _py_modules(self):
        text = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
        block = text.split("py-modules", 1)[1].split("[", 1)[1].split("]", 1)[0]
        return set(re.findall(r'"([^"]+)"', block))

    def test_every_local_module_imported_by_the_package_is_packaged(self):
        packaged = self._py_modules()
        local = {f[:-3] for f in os.listdir(ROOT) if f.endswith(".py")}
        # Modules that are deliberately not shipped: the end-to-end drivers and
        # the package's own entry-point shim.
        not_shipped = {"e2e_check", "e2e_receive_only", "__main__"}
        for name in sorted(packaged):
            source = os.path.join(ROOT, name + ".py")
            text = open(source, encoding="utf-8").read()
            imported = set(re.findall(r"^\s*(?:import|from)\s+([A-Za-z_][\w]*)",
                                      text, re.M))
            for dep in sorted(imported & local - not_shipped):
                self.assertIn(
                    dep, packaged,
                    "%s.py imports %r, which pyproject.toml's py-modules leaves "
                    "out - the wheel would install %s without it" % (name, dep, name))
