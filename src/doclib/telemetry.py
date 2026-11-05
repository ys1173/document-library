"""Optional OTLP export of metrics, traces and logs (spec section 15). No-op unless an endpoint is set."""

from __future__ import annotations

import logging

from opentelemetry import metrics, trace

_meter = metrics.get_meter("doclib")
tracer = trace.get_tracer("doclib")

files_by_status = _meter.create_gauge("doclib.files", description="Files by status")
queue_depth = _meter.create_gauge("doclib.queue.depth", description="Queued jobs by kind")
job_duration = _meter.create_histogram("doclib.job.duration", unit="s", description="Stage duration")
pages_by_path = _meter.create_counter("doclib.pages", description="Pages by extraction path")
embedded_chunks = _meter.create_counter("doclib.embedding.chunks", description="Chunks embedded")
search_latency = _meter.create_histogram("doclib.search.latency", unit="s", description="search_docs latency")
stale_hits = _meter.create_counter("doclib.open_doc.stale", description="Stale-index hits from open_doc")
scan_duration = _meter.create_histogram("doclib.scan.duration", unit="s", description="Scan duration")
scan_age = _meter.create_gauge("doclib.scan.age", unit="s", description="Age of the last successful scan")
webhook_sent = _meter.create_counter("doclib.webhook.deliveries", description="Webhook delivery attempts by result")


def setup_telemetry(service: str, endpoint: str | None) -> None:
    if not endpoint:
        return
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from .log import DocumentContentFilter

    base = endpoint.rstrip("/")
    resource = Resource.create({"service.name": service})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces")))
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[
        PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{base}/v1/metrics"))]))
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{base}/v1/logs")))
    set_logger_provider(logger_provider)
    handler = LoggingHandler(logger_provider=logger_provider)
    handler.addFilter(DocumentContentFilter())
    logging.getLogger().addHandler(handler)
