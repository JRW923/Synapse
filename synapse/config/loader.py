"""Configuration loader: YAML file + environment variables."""

import os
from pathlib import Path
import yaml
from synapse.config.schema import SynapseConfig


def load_config(config_path: str | None = None) -> tuple[SynapseConfig, str]:
    """Load config from YAML file, with env var overrides.

    Returns ``(config, source)`` where *source* is the path that was loaded
    (or ``"defaults"`` if no file was found).
    """
    config = SynapseConfig()
    source = "defaults"

    if config_path:
        path = Path(config_path)
    else:
        path = Path("synapse.yaml")
        if not path.exists():
            path = Path.home() / ".synapse" / "config.yaml"

    if path.exists():
        raw = yaml.safe_load(path.read_text())
        if raw:
            config = SynapseConfig.model_validate(raw)
            source = str(path)

    # Environment variable overrides
    if os.environ.get("SYNAPSE_PROVIDER"):
        config.provider.provider = os.environ["SYNAPSE_PROVIDER"]
    if os.environ.get("SYNAPSE_MODEL"):
        config.provider.model = os.environ["SYNAPSE_MODEL"]
    if os.environ.get("ANTHROPIC_API_KEY"):
        config.provider.api_key = os.environ["ANTHROPIC_API_KEY"]
    if os.environ.get("OPENAI_API_KEY"):
        config.provider.api_key = os.environ["OPENAI_API_KEY"]
    if os.environ.get("DEEPSEEK_API_KEY"):
        config.provider.api_key = os.environ["DEEPSEEK_API_KEY"]
    if os.environ.get("GOOGLE_API_KEY"):
        config.provider.api_key = os.environ["GOOGLE_API_KEY"]
    if os.environ.get("SYNAPSE_SANDBOX"):
        val = os.environ["SYNAPSE_SANDBOX"].lower()
        config.security.sandbox_enabled = val not in ("0", "false", "off")

    return config, source
