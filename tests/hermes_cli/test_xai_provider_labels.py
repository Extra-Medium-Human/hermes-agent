"""Regression tests for xAI provider label disambiguation."""

from hermes_cli.models import provider_label
from hermes_cli.providers import get_label


def test_xai_oauth_provider_label_is_not_collapsed_to_api_key_label(monkeypatch):
    """The model picker must distinguish xAI API-key and OAuth providers."""
    # Fresh/offline installs have no catalog; built-in labels still apply.
    monkeypatch.setattr("agent.models_dev.get_provider_info", lambda *_args, **_kwargs: None)
    assert get_label("xai") == "xAI"
    assert get_label("xai-oauth") == "xAI Grok OAuth (SuperGrok / Premium+)"
    assert get_label("grok-oauth") == "xAI Grok OAuth (SuperGrok / Premium+)"


