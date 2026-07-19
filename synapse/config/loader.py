"""Configuration loader: YAML file + environment variables."""

import os
from pathlib import Path
import yaml
from synapse.config.schema import SynapseConfig


def load_config(config_path: str | None = None) -> tuple[SynapseConfig, str]:
    """Load config from YAML file, with env var overrides.

    Lookup order (when *config_path* is ``None``):

    1. ``./synapse.yaml``
    2. Walk up the directory tree from CWD for ``synapse.yaml`` (like ``.git``)
    3. Check the synapse package's parent directory (handles ``pip install -e .``)
    4. ``~/.synapse/config.yaml``
    5. Built-in defaults

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
            # Walk up the tree to find a project-level synapse.yaml
            found: Path | None = None
            parent = Path.cwd().resolve()
            while True:
                candidate = parent / "synapse.yaml"
                if candidate.exists():
                    found = candidate
                    break
                next_parent = parent.parent
                if next_parent == parent:  # reached root
                    break
                parent = next_parent

            # Not found via CWD walk — try the package install directory.
            # Works when the project was installed with ``pip install -e .``
            # because then the package lives at ``<project>/synapse/``.
            if found is None:
                try:
                    import synapse
                    pkg_root = Path(synapse.__file__).resolve().parent.parent
                    candidate = pkg_root / "synapse.yaml"
                    if candidate.exists():
                        found = candidate
                except Exception:
                    pass

            if found is not None:
                path = found
            else:
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
