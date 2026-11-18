from __future__ import annotations

from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST as CONTENT_TYPE_LATEST
from prometheus_client import Counter, Histogram
from prometheus_client import generate_latest as generate_latest

tracer = trace.get_tracer("careerforge", "1.6.0")
HTTP_LATENCY = Histogram("careerforge_http_seconds", "API duration", ["route", "method", "status"])
RUNS = Counter("careerforge_runs_total", "Run attempts", ["workflow", "outcome"])
MODEL_CALLS = Counter(
    "careerforge_model_requests_total", "Paid/model requests", ["role", "outcome"]
)
MODEL_LATENCY = Histogram("careerforge_model_seconds", "Provider duration", ["role"])
RETRIEVAL_LATENCY = Histogram("careerforge_retrieval_seconds", "Retrieval duration", ["task_type"])


_configured = False


def setup(endpoint: str):
    global _configured
    if not endpoint or _configured:
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        raise RuntimeError("Install the production extra for OTLP export") from exc
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _configured = True
