from pathlib import Path

from lazyreview.config import Settings, write_default_config


def test_load_toml_config(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[server]
host = "10.0.0.2"
port = 4444

[paths]
data_dir = "{data}"

[worker]
job_timeout_seconds = 120
retention_sweep_seconds = 60
max_concurrent_jobs = 1
""".replace("{data}", str(tmp_path / "data").replace("\\", "/")),
        encoding="utf-8",
    )
    monkeypatch.setenv("LAZYREVIEW_CONFIG", str(cfg))
    monkeypatch.delenv("LAZYREVIEW_HOST", raising=False)
    monkeypatch.delenv("LAZYREVIEW_PORT", raising=False)
    monkeypatch.delenv("LAZYREVIEW_DATA_DIR", raising=False)

    s = Settings.load(create_if_missing=False)
    assert s.host == "10.0.0.2"
    assert s.port == 4444
    assert s.data_dir == tmp_path / "data"
    assert s.runs_dir == tmp_path / "data" / "runs"
    assert s.job_timeout_seconds == 120
    assert s.base_url == "http://10.0.0.2:4444"


def test_write_default_config(tmp_path: Path):
    path = tmp_path / "lazyreview" / "config.toml"
    written = write_default_config(path)
    assert written.exists()
    text = written.read_text(encoding="utf-8")
    assert "port = 3334" in text
    assert 'host = "127.0.0.1"' in text
