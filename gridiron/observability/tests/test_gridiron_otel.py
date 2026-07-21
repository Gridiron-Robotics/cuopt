# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""cuOpt OpenObserve/OTLP self-heal drop-in (gridiron_otel.py).

A raised ERROR emits an ERROR-severity OTLP log record tagged
service.name=cuopt (the OpenObserve stream == the incident 'module'); lower
severities don't ship; and it no-ops when OTEL_* is unset. The `level=error`
alert on the ``cuopt`` stream drives the langgraph self-heal loop, so a failed
GPU solve surfaces as an incident automatically.

These tests exercise ONLY the Gridiron overlay drop-in — they never import the
upstream cuOpt server, so they need neither a GPU nor the cuopt runtime.
"""

import logging

import pytest
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

from gridiron.observability import gridiron_otel as otel

SERVICE = "cuopt"


@pytest.fixture(autouse=True)
def _reset():
    otel.shutdown()
    yield
    otel.shutdown()
    for h in list(logging.getLogger().handlers):
        if h.__class__.__name__ == "LoggingHandler":
            logging.getLogger().removeHandler(h)


def test_error_emits_error_severity_otlp_record():
    exp = InMemoryLogRecordExporter()
    status = otel.setup_observability(SERVICE, _log_exporter=exp)
    assert status["logs"] is True
    logging.getLogger("cuopt.solver").error("solve failed: infeasible VRP")
    otel.shutdown()
    hit = [
        r
        for r in exp.get_finished_logs()
        if "solve failed: infeasible VRP" in str(r.log_record.body)
    ]
    assert hit and hit[0].log_record.severity_number == SeverityNumber.ERROR


def test_service_name_tags_the_stream():
    exp = InMemoryLogRecordExporter()
    otel.setup_observability(SERVICE, _log_exporter=exp)
    lp = next(
        p
        for p in otel._STATE["providers"]
        if p.__class__.__name__ == "LoggerProvider"
    )
    assert lp.resource.attributes["service.name"] == SERVICE


def test_info_below_threshold_not_shipped():
    exp = InMemoryLogRecordExporter()
    otel.setup_observability(SERVICE, _log_exporter=exp)
    logging.getLogger("cuopt.solver").info("routine solve accepted")
    otel.shutdown()
    assert not [
        r
        for r in exp.get_finished_logs()
        if "routine solve accepted" in str(r.log_record.body)
    ]


def test_disabled_by_default_is_graceful_noop(monkeypatch):
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    status = otel.setup_observability(SERVICE)
    assert status == {
        "traces": False,
        "logs": False,
        "fastapi": False,
        "httpx": False,
    }
