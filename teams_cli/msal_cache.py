"""Read MSAL credentials without executing the Teams web application.

MSAL cache keys are an implementation detail, so credentialType and JWT audience
are authoritative. Both older dash keys and newer msal.2 pipe keys are accepted.
No credential values are included in errors or diagnostic output.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass

from .constants import TEAMS_CLIENT_ID
from .exceptions import AuthRequiredError

TOKEN_AUDIENCES = {
    "ic3": "ic3.teams.office.com",
    "graph": "graph.microsoft.com",
    "presence": "presence.teams.microsoft.com",
    "csa": "chatsvcagg.teams.microsoft.com",
    "substrate": "substrate.office.com",
}
TEAMS_ORIGINS = {"https://teams.cloud.microsoft", "https://teams.microsoft.com"}
EXPIRY_BUFFER = 60


def claims(token: str) -> dict:
    if not isinstance(token, str):
        return {}
    try:
        part = token.split(".")[1]
        result = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError, IndexError):
        return {}


def audience(token: str, target: str = "", key: str = "") -> str | None:
    value = str(claims(token).get("aud", "")).lower().rstrip("/")
    if value == "00000003-0000-0000-c000-000000000000":
        return "graph"
    for name, host in TOKEN_AUDIENCES.items():
        if value in (host, "https://" + host):
            return name
    # Opaque tokens still have a resource in the MSAL target/cache key.
    if not value:
        for name, host in TOKEN_AUDIENCES.items():
            if host in target.lower() or host in key.lower():
                return name
    return None


def number(value: object) -> float:
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0


@dataclass
class Credential:
    origin: str
    entry: dict
    value: dict

    @property
    def token(self) -> str:
        return str(self.value.get("secret") or "")

    @property
    def kind(self) -> str:
        kind = str(self.value.get("credentialType") or "").lower()
        if kind:
            return kind
        key = str(self.entry.get("name") or "").lower()
        for name in ("accesstoken", "refreshtoken"):
            if re.search(r"(?:^|[-|])" + name + r"(?:[-|]|$)", key):
                return name
        return ""

    @property
    def resource(self) -> str | None:
        return audience(self.token, str(self.value.get("target") or ""), str(self.entry.get("name") or ""))

    @property
    def expires(self) -> float:
        # Use the earlier bound if both JWT and cache expiry exist.
        bounds = [number(claims(self.token).get("exp")), number(self.value.get("expiresOn"))]
        return min(value for value in bounds if value > 0) if any(bounds) else 0

    @property
    def account(self) -> tuple[str, str]:
        token_claims = claims(self.token)
        return (
            str(self.value.get("homeAccountId") or token_claims.get("oid") or ""),
            str(self.value.get("realm") or token_claims.get("tid") or ""),
        )


def credentials(state: dict) -> list[Credential]:
    result = []
    for origin in state.get("origins", []):
        if origin.get("origin") not in TEAMS_ORIGINS:
            continue
        for entry in origin.get("localStorage", []):
            try:
                value = json.loads(entry.get("value", ""))
            except (ValueError, TypeError):
                continue
            if not isinstance(value, dict):
                continue
            credential = Credential(origin["origin"], entry, value)
            if (credential.kind in ("accesstoken", "refreshtoken") and credential.token
                    and value.get("clientId", TEAMS_CLIENT_ID) == TEAMS_CLIENT_ID):
                result.append(credential)
    return result


def select_account(records: list[Credential], expected: dict | None = None) -> tuple[str, str]:
    expected = expected or {}
    expected_claims = claims(str(expected.get("ic3") or ""))
    user = str(expected.get("user_id") or expected_claims.get("oid") or "")
    tenant = str(expected.get("tenant_id") or expected_claims.get("tid") or "")
    home = str(expected.get("home_account_id") or "")
    candidates = [record for record in records if record.kind == "accesstoken" and record.resource == "ic3"]
    if home:
        candidates = [record for record in candidates if record.account[0] == home]
    if user:
        candidates = [record for record in candidates if claims(record.token).get("oid") == user]
    if tenant:
        candidates = [record for record in candidates if record.account[1] == tenant]
    accounts = {record.account for record in candidates}
    if not accounts:
        raise AuthRequiredError("Saved Teams session has no credentials for this account. Run: teams login")
    if len(accounts) != 1:
        raise AuthRequiredError("Saved Teams session contains multiple accounts. Run: teams login --force")
    return accounts.pop()


def newest_access(records: list[Credential], account: tuple[str, str]) -> dict[str, Credential]:
    result = {}
    ic3_records = [record for record in records if record.kind == "accesstoken"
                   and record.account == account and record.resource == "ic3"]
    reference = claims(max(ic3_records, key=lambda record: record.expires).token) if ic3_records else {}
    for record in records:
        if record.kind != "accesstoken" or record.account != account or not record.resource:
            continue
        token_claims = claims(record.token)
        if any(reference.get(key) and token_claims.get(key) and reference[key] != token_claims[key]
               for key in ("oid", "tid")):
            continue
        previous = result.get(record.resource)
        if previous is None or record.expires > previous.expires:
            result[record.resource] = record
    return result


def extract_tokens(state: dict, expected: dict | None = None, *, include_expired: bool = False) -> dict[str, str]:
    records = credentials(state)
    account = select_account(records, expected)
    selected = newest_access(records, account)
    ic3 = selected.get("ic3")
    token_claims = claims(ic3.token) if ic3 else {}
    result = {
        name: record.token for name, record in selected.items()
        if include_expired or record.expires > time.time() + EXPIRY_BUFFER
    }
    result.update({
        "user_id": str(token_claims.get("oid") or ""),
        "tenant_id": account[1],
        "home_account_id": account[0],
        "region": discover_region(state),
    })
    return result


def discover_region(state: dict) -> str:
    for origin in state.get("origins", []):
        if origin.get("origin") not in TEAMS_ORIGINS:
            continue
        for entry in origin.get("localStorage", []):
            if "discover-region-gtm" not in str(entry.get("name", "")).lower():
                continue
            try:
                value = json.loads(entry["value"])
                gtms = value.get("regionGtms", value)
                if isinstance(gtms, str):
                    gtms = json.loads(gtms)
                match = re.search(r"/chatsvc/(emea|amer|apac)(?:/|$)", str(gtms.get("chatService", "")))
                if match:
                    return match.group(1)
                if value.get("region") in ("emea", "amer", "apac"):
                    return value["region"]
            except (ValueError, TypeError, AttributeError, KeyError):
                continue
    return "emea"


def refresh_scopes(record: Credential) -> str:
    scopes = str(record.value.get("target") or "").split()
    if not scopes:
        host = TOKEN_AUDIENCES.get(record.resource or "")
        return "https://" + host + "/.default" if host else ""
    explicit = [scope for scope in scopes if not scope.lower().endswith("/.default")]
    # Cached targets contain expanded delegated grants plus .default. Entra
    # rejects that combination in a request (AADSTS70011).
    return " ".join(explicit or scopes)
