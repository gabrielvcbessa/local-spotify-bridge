#!/usr/bin/env python3
"""Verify that a live bridge is running the expected deployed contract."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


EXPECTED_PLAYBACK_READY_CONTRACT = "resolved_unrestricted_active_target"
VALID_BUILD_SOURCES = {"environment", "git"}


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError(f"{url} did not return a JSON object")
    return data


def local_git_head(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def build_info(payload: dict[str, Any]) -> dict[str, Any]:
    build = payload.get("build")
    return build if isinstance(build, dict) else {}


def readiness_contract(payload: dict[str, Any], *, source: str) -> dict[str, Any]:
    if source == "health":
        devices = payload.get("backend_capabilities", {}).get("devices", {})
    elif source == "config":
        devices = payload.get("capabilities", {}).get("devices", {})
    else:
        raise ValueError(f"unknown source {source!r}")
    contract = devices.get("readiness_contract") if isinstance(devices, dict) else None
    return contract if isinstance(contract, dict) else {}


def validate_build(
    payload: dict[str, Any],
    *,
    label: str,
    expected_commit: str | None,
    require_known_build: bool,
) -> list[str]:
    failures: list[str] = []
    build = build_info(payload)
    commit = str(build.get("commit") or "").strip()
    ref = str(build.get("ref") or "").strip()
    source = str(build.get("source") or "").strip()

    if not commit:
        failures.append(f"{label} build.commit missing")
    if not ref:
        failures.append(f"{label} build.ref missing")
    if not source:
        failures.append(f"{label} build.source missing")
    if require_known_build and commit == "unknown":
        failures.append(f"{label} build.commit is unknown")
    if require_known_build and ref == "unknown":
        failures.append(f"{label} build.ref is unknown")
    if require_known_build and source not in VALID_BUILD_SOURCES:
        failures.append(f"{label} build.source must be one of {sorted(VALID_BUILD_SOURCES)}")
    if expected_commit and commit != expected_commit:
        failures.append(f"{label} build.commit {commit!r} != expected {expected_commit!r}")
    return failures


def validate_contract(payload: dict[str, Any], *, label: str, source: str) -> list[str]:
    failures: list[str] = []
    contract = readiness_contract(payload, source=source)
    playback_ready = contract.get("playback_ready_for_live_control")
    if playback_ready != EXPECTED_PLAYBACK_READY_CONTRACT:
        failures.append(
            f"{label} readiness_contract.playback_ready_for_live_control "
            f"{playback_ready!r} != {EXPECTED_PLAYBACK_READY_CONTRACT!r}"
        )
    risk_taxonomy = contract.get("risk_taxonomy")
    if not isinstance(risk_taxonomy, list) or "volume_unavailable" not in risk_taxonomy or "zero_volume" not in risk_taxonomy:
        failures.append(f"{label} readiness_contract.risk_taxonomy missing volume risks")
    guarded = contract.get("guarded_commands")
    if not isinstance(guarded, list) or "next" not in guarded or "previous" not in guarded:
        failures.append(f"{label} readiness_contract.guarded_commands missing next/previous")
    return failures


def validate_payloads(
    health: dict[str, Any],
    config: dict[str, Any],
    *,
    expected_commit: str | None = None,
    require_known_build: bool = True,
) -> list[str]:
    failures: list[str] = []
    failures.extend(
        validate_build(
            health,
            label="health",
            expected_commit=expected_commit,
            require_known_build=require_known_build,
        )
    )
    failures.extend(
        validate_build(
            config,
            label="config",
            expected_commit=expected_commit,
            require_known_build=require_known_build,
        )
    )
    failures.extend(validate_contract(health, label="health", source="health"))
    failures.extend(validate_contract(config, label="config", source="config"))
    if build_info(health).get("commit") != build_info(config).get("commit"):
        failures.append("health/config build.commit mismatch")
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8090")
    parser.add_argument("--expected-commit", default=None)
    parser.add_argument("--allow-unknown-build", action="store_true")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--summary-json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    root = Path(__file__).resolve().parents[1]
    expected_commit = args.expected_commit
    if expected_commit is None:
        expected_commit = local_git_head(root) or None

    try:
        health = fetch_json(f"{base_url}/health", args.timeout)
        config = fetch_json(f"{base_url}/v1/knob/config", args.timeout)
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        print(f"live-bridge-deployment=FAIL base_url={base_url} error={exc}", file=sys.stderr)
        return 1

    failures = validate_payloads(
        health,
        config,
        expected_commit=expected_commit,
        require_known_build=not args.allow_unknown_build,
    )
    summary = {
        "base_url": base_url,
        "ok": not failures,
        "expected_commit": expected_commit,
        "health_build": build_info(health),
        "config_build": build_info(config),
        "failures": failures,
    }
    if args.summary_json:
        Path(args.summary_json).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if failures:
        print(f"live-bridge-deployment=FAIL base_url={base_url}", file=sys.stderr)
        for failure in failures:
            print(f"fail: {failure}", file=sys.stderr)
        return 1

    commit = build_info(health).get("commit")
    print(f"live-bridge-deployment=PASS base_url={base_url} commit={commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
