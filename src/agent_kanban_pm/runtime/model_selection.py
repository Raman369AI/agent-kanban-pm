"""Resolve adapter model choices consistently across launch paths."""


def effective_model(adapter, requested):
    # Bundled and older user adapters use these labels for the CLI's default,
    # not as literal model identifiers. A CLI without a flag chooses its own.
    if not adapter.invoke.model_flag or requested in (None, "", "default", adapter.name + "-default"):
        return None
    return requested


def model_arguments(adapter, requested):
    model = effective_model(adapter, requested)
    return [adapter.invoke.model_flag, model] if model else []
