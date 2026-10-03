"""Profile fallback is passed to the Langfuse SDK constructor."""
import importlib
import sys


def test_client_defaults_environment_to_scoped_profile(monkeypatch, tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    sys.modules.pop("plugins.observability.langfuse", None)
    plugin = importlib.import_module("plugins.observability.langfuse")
    settings = {
        "HERMES_LANGFUSE_PUBLIC_KEY": "pk-lf-test",
        "HERMES_LANGFUSE_SECRET_KEY": "sk-lf-test",
    }
    monkeypatch.setattr(plugin, "_secret", lambda key: settings.get(key, ""))
    monkeypatch.setattr(plugin, "Langfuse", lambda **kwargs: kwargs)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "launcher"))
    token = set_hermes_home_override(tmp_path / ".hermes" / "profiles" / "research")
    try:
        client = plugin._build_client()
        assert client.get("environment") == "research"
    finally:
        reset_hermes_home_override(token)
