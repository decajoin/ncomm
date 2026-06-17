"""Tests for ncomm.config — resolution order and round-trip."""


from ncomm import config as cfgmod


def test_config_path_respects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NCOMM_CONFIG", str(tmp_path / "c.toml"))
    assert cfgmod.config_path() == tmp_path / "c.toml"


def test_save_and_load_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("NCOMM_CONFIG", str(tmp_path / "c.toml"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("NCOMM_API_KEY", raising=False)
    cfgmod.save_config({"api_key": "sk-test123", "model": "deepseek-v4-pro"})
    cfg = cfgmod.load_config()
    assert cfg.api_key == "sk-test123"
    assert cfg.model == "deepseek-v4-pro"
    assert cfg.has_key


def test_env_overrides_file(monkeypatch, tmp_path):
    monkeypatch.setenv("NCOMM_CONFIG", str(tmp_path / "c.toml"))
    cfgmod.save_config({"api_key": "sk-file"})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    cfg = cfgmod.load_config()
    assert cfg.api_key == "sk-env"
