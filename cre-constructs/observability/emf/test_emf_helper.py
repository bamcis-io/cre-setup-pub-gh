"""Lightweight tests for emf_helper (run with pytest if available; otherwise importable).

These do not require pytest as a hard dependency of the template -- the module is shipped
as source. If pytest is present, ``pytest test_emf_helper.py`` exercises the single-line
client-metric rule (C3 / the #1 footgun).
"""
import json

from emf_helper import emit_request


def _capture(capsys):
    out = capsys.readouterr().out.strip().splitlines()
    return [json.loads(line) for line in out if line.strip()]


def test_single_region_request_emits_region_and_client_lines(capsys):
    emit_request("us-east-1", success=True, latency_ms=12.0, namespace="NS")
    lines = _capture(capsys)
    assert len(lines) == 2  # one region line + one client line

    region_line, client_line = lines
    assert region_line["Region"] == "us-east-1"
    assert region_line["RegionSuccess"] == 1
    assert region_line["RegionError"] == 0
    assert region_line["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "NS"
    assert region_line["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Region"]]

    assert client_line["ClientSuccess"] == 1
    assert client_line["ClientError"] == 0
    assert client_line["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[]]


def test_failure_sets_error_counts(capsys):
    emit_request("eu-west-1", success=False, latency_ms=99.0, namespace="NS")
    lines = _capture(capsys)
    region_line, client_line = lines
    assert region_line["RegionSuccess"] == 0 and region_line["RegionError"] == 1
    assert client_line["ClientSuccess"] == 0 and client_line["ClientError"] == 1


def test_is_first_false_suppresses_client_line(capsys):
    # The fan-out rule: only the first region line carries client metrics.
    emit_request("us-east-1", success=True, latency_ms=1.0, is_first=True, namespace="NS")
    emit_request("us-west-2", success=True, latency_ms=2.0, is_first=False, namespace="NS")
    lines = _capture(capsys)
    # 2 region lines + exactly 1 client line == 3 total (no double-count).
    assert len(lines) == 3
    client_lines = [ln for ln in lines if "ClientSuccess" in ln]
    assert len(client_lines) == 1
