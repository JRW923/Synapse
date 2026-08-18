"""免费模型 CLI 覆盖逻辑回归测试。

核心约束：_save_free_model 必须复用 schema.py 的 OPENROUTER_DEFAULT_MODEL 常量，
不得在 cli.py 里写第二份硬编码默认模型；用户给出的覆盖模型名要能落盘。
"""

from synapse.config.schema import OPENROUTER_DEFAULT_MODEL
from synapse.config.models import load_models_config


def test_save_free_model_default_uses_schema_constant(monkeypatch, tmp_path):
    from synapse.adapters.cli import _save_free_model

    cfg = tmp_path / "models.json"
    monkeypatch.setattr("synapse.config.models.models_config_path", lambda: cfg)

    _save_free_model(None)  # 仅走环境变量 key
    loaded = load_models_config(cfg)
    assert loaded.default_provider == "openrouter"
    assert loaded.default_model == OPENROUTER_DEFAULT_MODEL


def test_save_free_model_override_persists_custom(monkeypatch, tmp_path):
    from synapse.adapters.cli import _save_free_model

    cfg = tmp_path / "models.json"
    monkeypatch.setattr("synapse.config.models.models_config_path", lambda: cfg)

    _save_free_model("sk-or-x", model="deepseek/deepseek-v3:free")
    loaded = load_models_config(cfg)
    assert loaded.default_model == "deepseek/deepseek-v3:free"
