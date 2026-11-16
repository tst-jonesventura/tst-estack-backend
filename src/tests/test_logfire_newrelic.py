from opentelemetry.sdk.trace.export import BatchSpanProcessor

from utils.config import Settings, build_new_relic_span_processors


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_no_processors_without_license_key(monkeypatch):
    monkeypatch.delenv("NEW_RELIC_LICENSE_KEY", raising=False)
    assert build_new_relic_span_processors(_settings()) == []


def test_otlp_processor_with_license_key(monkeypatch):
    monkeypatch.setenv("NEW_RELIC_LICENSE_KEY", "nr-key")
    monkeypatch.setenv("NEW_RELIC_OTLP_ENDPOINT", "https://otlp.eu01.nr-data.net:4317")

    processors = build_new_relic_span_processors(_settings())

    assert len(processors) == 1
    assert isinstance(processors[0], BatchSpanProcessor)
    exporter = processors[0].span_exporter
    assert exporter._endpoint == "otlp.eu01.nr-data.net:4317"
    assert ("api-key", "nr-key") in exporter._headers


def test_default_endpoint_is_new_relic_us(monkeypatch):
    monkeypatch.delenv("NEW_RELIC_OTLP_ENDPOINT", raising=False)
    assert _settings().new_relic_otlp_endpoint == "https://otlp.nr-data.net:4317"


def test_default_app_name(monkeypatch):
    monkeypatch.delenv("NEW_RELIC_APP_NAME", raising=False)
    assert _settings().new_relic_app_name == "estack-backend"
