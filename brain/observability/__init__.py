"""Observability: execution-history tracing for the brain.

A run is a trace of nested spans/events, persisted to SQLite and exported as a
human-readable Markdown timeline. Components take an optional tracer that
defaults to a no-op ``NullTracer``, so instrumentation never changes behavior.
"""
from .tracer import NullTracer, Run, Span, Tracer, trace_query

__all__ = ["Tracer", "NullTracer", "Run", "Span", "trace_query"]
