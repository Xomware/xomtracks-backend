"""RED-before-GREEN: extractor/watermark.py -- persists the last-processed
message.ROWID between extractor runs."""

import json
import os


class TestLoadWatermark:
    def test_missing_file_defaults_to_zero(self, tmp_path):
        from extractor.watermark import load_watermark

        path = str(tmp_path / "state.json")
        assert load_watermark(path) == 0

    def test_loads_persisted_value(self, tmp_path):
        from extractor.watermark import load_watermark

        path = str(tmp_path / "state.json")
        with open(path, "w") as f:
            json.dump({"last_rowid": 42}, f)

        assert load_watermark(path) == 42

    def test_corrupt_file_defaults_to_zero(self, tmp_path):
        from extractor.watermark import load_watermark

        path = str(tmp_path / "state.json")
        with open(path, "w") as f:
            f.write("not json{{{")

        assert load_watermark(path) == 0


class TestSaveWatermark:
    def test_creates_parent_dirs(self, tmp_path):
        from extractor.watermark import save_watermark, load_watermark

        path = str(tmp_path / "nested" / "dir" / "state.json")
        save_watermark(path, 99)

        assert os.path.exists(path)
        assert load_watermark(path) == 99

    def test_overwrites_existing_value(self, tmp_path):
        from extractor.watermark import save_watermark, load_watermark

        path = str(tmp_path / "state.json")
        save_watermark(path, 1)
        save_watermark(path, 2)

        assert load_watermark(path) == 2
