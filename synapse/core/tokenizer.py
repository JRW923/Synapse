"""Best-effort provider tokenizer with a deterministic stdlib fallback."""

from functools import lru_cache


@lru_cache(maxsize=8)
def _encoding(model: str):
    try:
        import tiktoken
        try:
            return tiktoken.encoding_for_model(model or "gpt-4o-mini")
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")
    except ImportError:
        return None


def count_tokens(text: str, model: str = "") -> int:
    """Count tokens using tiktoken when installed, otherwise a conservative estimate."""
    if not text:
        return 0
    encoder = _encoding(model)
    if encoder is not None:
        return len(encoder.encode(text, disallowed_special=()))
    return max(1, (len(text) + 3) // 4)
