"""Configuration loader: YAML + user model registry + environment variables."""

import os
from pathlib import Path
import yaml
from synapse.config.schema import SynapseConfig, _PROVIDER_ENV_VARS
from synapse.config import models as model_registry


def load_config(
    config_path: str | None = None,
    *,
    models_path: str | Path | None = None,
) -> tuple[SynapseConfig, str]:
    """Load project config, user models, then environment overrides.

    Lookup order (when *config_path* is ``None``):

    1. ``./synapse.yaml``
    2. Walk up the directory tree from CWD for ``synapse.yaml`` (like ``.git``)
    3. Check the synapse package's parent directory (handles ``pip install -e .``)
    4. ``~/.synapse/config.yaml``
    5. Built-in defaults

    The user-level ``~/.synapse/models.json`` is overlaid after YAML and owns
    the active provider/model plus registered model credentials.

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

    registry_path = (
        Path(models_path) if models_path is not None else model_registry.models_config_path()
    )
    registry = model_registry.load_models_config(registry_path)
    if registry is not None:
        model_registry.apply_models_config(config, registry)
        source = str(registry_path) if source == "defaults" else f"{source} + {registry_path}"

    # Environment variable overrides
    env_provider = os.environ.get("SYNAPSE_PROVIDER")
    env_model = os.environ.get("SYNAPSE_MODEL")
    if env_provider or env_model:
        model_registry.apply_model_selection(
            config,
            env_provider or config.provider.provider,
            env_model or config.provider.model,
        )
    # provider API keys: only the CURRENT provider's env var applies. Looping
    # over all providers let e.g. ANTHROPIC + GOOGLE keys both set clobber the
    # active key arbitrarily (dict order decided the winner).
    current = config.provider.provider
    env_var = _PROVIDER_ENV_VARS.get(current, "")
    if env_var and os.environ.get(env_var):
        config.provider.api_key = os.environ[env_var]
    if os.environ.get("SYNAPSE_SANDBOX"):
        val = os.environ["SYNAPSE_SANDBOX"].lower()
        config.security.sandbox_enabled = val not in ("0", "false", "off")

    return config, source
