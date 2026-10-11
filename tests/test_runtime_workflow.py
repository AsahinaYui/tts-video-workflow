"""Offline regressions for the deployed WebUI workflow; no model calls."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("video_workflow_app", ROOT / "video-webui/app.py")
app = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = app
SPEC.loader.exec_module(app)


class RuntimeWorkflowTests(unittest.TestCase):
    def test_changed_upload_replaces_prepared_image_and_allows_same_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old.png", root / "new.png"
            old.write_bytes(b"original image")
            new.write_bytes(b"replacement image")
            job = root / "job"
            app.current_task_image(job, str(old))
            selected = Path(app.current_task_image(job, str(new)))
            self.assertEqual(selected.read_bytes(), b"replacement image")
            self.assertEqual(Path(app.current_task_image(job, str(selected))), selected)

    def test_asr_substitutions_do_not_trigger_omission_retries(self):
        self.assertEqual(app.segment_omission_report("今天我们一起出去玩", "明天大家共同去散步")[0], "PASS")
        self.assertEqual(app.segment_omission_report("今天我们一起出去玩", "今天我们一起出")[0], "PASS")
        self.assertEqual(app.segment_omission_report("今天我们一起出去玩", "今天我们")[0], "RETRY")

    def test_subtitle_manual_lines_paginate_without_losing_text(self):
        config = app.AppConfig(ROOT / "unused.json", {})
        pages = app.subtitle_pages_from_block(config, "第一行\n第二行\n第三行")
        self.assertEqual(pages, ["第一行\n第二行", "第三行"])

    def test_storyboard_mapping_preserves_clause_identifiers(self):
        images = [Path("first.png"), Path("second.png")]
        result = app.storyboard_json_from_mapping("1: 1.1,1.2\n2: 2.1", images, "pad")
        self.assertEqual(result["shots"][0]["segments"], ["1.1", "1.2"])
        self.assertEqual(result["shots"][1]["image"], "second.png")

    def test_replacement_keeps_short_neighbor_edges_without_crossfade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = app.AppConfig(root / "unused.json", {})
            with patch.object(app, "audio_duration_seconds", return_value=2.0), \
                 patch.object(app, "make_analysis_wav", side_effect=RuntimeError("no refinement")), \
                 patch.object(app, "cut_audio_segment", side_effect=lambda c, s, out, *args: out) as cut, \
                 patch.object(app, "concat_audio_with_padding", return_value=root / "out.wav") as join, \
                 patch.object(app, "concat_audio_with_crossfade") as fade:
                app.splice_audio_replace_range(config, root / "source.wav", root / "patch.wav",
                                               root / "out.wav", 0.01, 1.99, root / "splice")
                self.assertEqual(cut.call_count, 2)
                self.assertEqual(cut.call_args_list[0].args[3:5], (0.0, 0.01))
                self.assertEqual(cut.call_args_list[1].args[3:5], (1.99, 2.0))
                self.assertEqual(join.call_args.args[3], 0.0)
                fade.assert_not_called()


if __name__ == "__main__":
    unittest.main()
