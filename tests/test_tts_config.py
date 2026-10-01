import importlib.util
import logging
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills/gptsovits-tts/scripts/gsv_tts.py"
SPEC = importlib.util.spec_from_file_location("gsv_tts", SCRIPT)
gsv = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gsv
SPEC.loader.exec_module(gsv)


class WorkflowConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.patch = patch.object(gsv, "PROJECT_ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.logger = logging.getLogger(__name__)
        self.source = self.root / "source.yaml"
        self.original = (
            "custom:\n  device: cpu\n  is_half: false\n"
            "  t2s_weights_path: missing.ckpt\n"
            "  vits_weights_path: missing.pth\n  version: v2ProPlus\n"
            "v2Pro:\n  device: cpu\n  version: v2Pro\n"
        )
        self.source.write_text(self.original, encoding="utf-8")
        self.paths = self.selected("Mujica_若葉睦_v2p")
        self.target = gsv.workflow_tts_config_path(self.root)

    def selected(self, name):
        folder = self.root / "weights_v2Pro"
        folder.mkdir(exist_ok=True)
        gpt, sovits = folder / (name + ".ckpt"), folder / (name + ".pth")
        gpt.touch()
        sovits.touch()
        return gsv.RuntimePaths(self.root, Path(sys.executable), self.source,
                                gpt, sovits, self.root / "api_v2.py")

    def check_selection(self, paths):
        result = gsv.prepare_workflow_tts_config(paths, self.logger)
        custom = gsv.parse_simple_tts_yaml(result.tts_config_path)["custom"]
        self.assertEqual(Path(custom["t2s_weights_path"]), paths.gpt_weights_path)
        self.assertEqual(Path(custom["vits_weights_path"]), paths.sovits_weights_path)
        self.assertEqual(custom["version"], "v2Pro")
        self.assertEqual(custom["device"], "cpu")
        gsv.inspect_tts_config(result, self.logger)
        return result

    def test_first_run_preserves_source_and_other_sections(self):
        self.check_selection(self.paths)
        self.assertEqual(self.source.read_text(encoding="utf-8"), self.original)
        self.assertEqual(gsv.parse_simple_tts_yaml(self.target)["v2Pro"],
                         gsv.parse_simple_tts_yaml(self.source)["v2Pro"])

    def test_stale_cache_does_not_override_saved_preset(self):
        self.target.parent.mkdir()
        self.target.write_text(self.original.replace("missing.ckpt", "Mutsumi.ckpt"),
                               encoding="utf-8")
        self.check_selection(self.paths)

    def test_switching_characters_updates_both_weights(self):
        self.check_selection(self.paths)
        self.check_selection(self.selected("Mujica_Mortis_v2p"))
        self.check_selection(self.paths)

    def test_source_already_at_target_is_refreshed(self):
        self.check_selection(self.paths)
        selected = replace(self.selected("another_role"), tts_config_path=self.target)
        self.check_selection(selected)


if __name__ == "__main__":
    unittest.main()
