"""Owner PR P3: the state API accepts configured tag prefixes (no network): python -m unittest discover tests"""
import importlib
import os
import unittest
from unittest import mock

import config
import state_api


def reload(env):
    with mock.patch.dict(os.environ, env, clear=False):
        importlib.reload(config)
        importlib.reload(state_api)
    return state_api


class TestPrefixes(unittest.TestCase):
    def tearDown(self):
        reload({"PM_EXTRA_TAG_PREFIXES": ""})

    def test_default_is_pmsynth_only(self):
        sa = reload({"PM_EXTRA_TAG_PREFIXES": ""})
        self.assertEqual(config.TAG_PREFIXES, ("PMSYNTH-",))
        self.assertIn('var PREFIXES = ["PMSYNTH-"]', sa.SCRIPT)
        self.assertIn('var PREFIXES = ["[PMSYNTH]"]', sa.PUBLISH_SCRIPT)

    def test_extra_prefix_reaches_both_server_scripts(self):
        sa = reload({"PM_EXTRA_TAG_PREFIXES": "SYN-E2E-"})
        self.assertEqual(config.TAG_PREFIXES, ("PMSYNTH-", "SYN-E2E-"))
        self.assertIn('var PREFIXES = ["PMSYNTH-", "SYN-E2E-"]', sa.SCRIPT)
        self.assertIn('var PREFIXES = ["[PMSYNTH]", "[SYN-E2E-"]', sa.PUBLISH_SCRIPT)

    def test_a_prefix_that_could_change_the_script_is_refused(self):
        for bad in ("SYN-E2E", "syn-e2e-", "X-'];gs.log(1);//-", "SYN E2E-"):
            with self.assertRaises(SystemExit, msg=bad):
                reload({"PM_EXTRA_TAG_PREFIXES": bad})


if __name__ == "__main__":
    unittest.main()
