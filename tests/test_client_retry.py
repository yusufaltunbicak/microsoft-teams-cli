from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

import teams_cli.client as client_mod
from teams_cli.client import _CircuitBreaker
from teams_cli.exceptions import RetryableError


def test_request_with_retry_honors_retry_after_seconds(teams_client, mocker):
    request = httpx.Request("GET", "https://example.com")
    responses = [
        httpx.Response(429, headers={"Retry-After": "3"}, request=request),
        httpx.Response(200, json={"ok": True}, request=request),
    ]

    mocker.patch.object(teams_client._session.client, "request", side_effect=responses)
    sleep = mocker.patch.object(client_mod.time, "sleep")

    payload = teams_client._request_with_retry("GET", "https://example.com", headers={})

    assert payload == {"ok": True}
    sleep.assert_called_once_with(3.0)


def test_request_with_retry_parses_retry_after_http_date(teams_client, mocker):
    request = httpx.Request("GET", "https://example.com")
    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=2)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    responses = [
        httpx.Response(429, headers={"Retry-After": retry_at}, request=request),
        httpx.Response(200, json={"ok": True}, request=request),
    ]

    mocker.patch.object(teams_client._session.client, "request", side_effect=responses)
    sleep = mocker.patch.object(client_mod.time, "sleep")

    payload = teams_client._request_with_retry("GET", "https://example.com", headers={})

    assert payload == {"ok": True}
    assert 0 < sleep.call_args.args[0] <= 3


def test_request_with_retry_retries_single_5xx(teams_client, mocker):
    request = httpx.Request("GET", "https://example.com")
    responses = [
        httpx.Response(502, request=request),
        httpx.Response(200, json={"ok": True}, request=request),
    ]

    request_mock = mocker.patch.object(teams_client._session.client, "request", side_effect=responses)
    sleep = mocker.patch.object(client_mod.time, "sleep")

    payload = teams_client._request_with_retry("GET", "https://example.com", headers={})

    assert payload == {"ok": True}
    assert request_mock.call_count == 2
    sleep.assert_called_once_with(1)


def test_request_with_retry_retries_network_error_once(teams_client, mocker):
    request = httpx.Request("GET", "https://example.com")
    responses = [
        httpx.ReadTimeout("timed out", request=request),
        httpx.Response(200, json={"ok": True}, request=request),
    ]

    request_mock = mocker.patch.object(teams_client._session.client, "request", side_effect=responses)
    sleep = mocker.patch.object(client_mod.time, "sleep")

    payload = teams_client._request_with_retry("GET", "https://example.com", headers={})

    assert payload == {"ok": True}
    assert request_mock.call_count == 2
    sleep.assert_called_once_with(1)


def test_circuit_breaker_opens_and_resets(mocker):
    breaker = _CircuitBreaker(threshold=2, reset_seconds=30)
    breaker.record_failure()
    breaker.record_failure()

    with pytest.raises(RetryableError, match="Circuit breaker is open"):
        breaker.raise_if_open()

    mocker.patch.object(client_mod.time, "time", return_value=breaker.opened_at + 31)
    breaker.raise_if_open()
    assert breaker.is_open is False


def test_request_with_retry_does_not_retry_non_retryable_4xx(teams_client, mocker):
    request = httpx.Request("GET", "https://example.com")
    request_mock = mocker.patch.object(
        teams_client._session.client,
        "request",
        return_value=httpx.Response(403, request=request),
    )

    with pytest.raises(httpx.HTTPStatusError):
        teams_client._request_with_retry("GET", "https://example.com", headers={})

    assert request_mock.call_count == 1


def test_create_group_chat_uses_retry_hardened_raw_request(teams_client, mocker):
    browser_headers = mocker.patch.object(
        teams_client._session,
        "browser_headers",
        return_value={"Authorization": "Bearer ic3"},
    )
    jitter = mocker.patch.object(teams_client._session, "jitter")
    request = httpx.Request("POST", f"{teams_client._chatsvc}/threads")
    responses = [
        httpx.Response(500, request=request),
        httpx.Response(
            201,
            request=request,
            headers={"location": f"{teams_client._chatsvc}/threads/19:group@thread.v2"},
        ),
    ]
    raw_request = mocker.patch.object(teams_client._session.client, "request", side_effect=responses)
    sleep = mocker.patch.object(client_mod.time, "sleep")
    set_topic = mocker.patch.object(teams_client, "_ic3_put", return_value={})

    created = teams_client.create_group_chat(["other-user"], topic="Release Room")

    assert created == {"id": "19:group@thread.v2", "status": "created"}
    browser_headers.assert_called_once_with(teams_client._ic3)
    jitter.assert_called_once_with(is_write=True)
    assert raw_request.call_count == 2
    sleep.assert_called_once_with(1)
    set_topic.assert_called_once()
