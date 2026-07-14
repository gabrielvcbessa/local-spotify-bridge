from scripts.verify_live_deployment import validate_payloads


def payloads(commit: str = "abc123", ref: str = "main", source: str = "environment"):
    architecture = {
        "profile_model": "profile_registry",
        "multi_profile_selection": True,
        "multi_profile_selection_blocker": "",
        "profile_registry": {
            "schema_version": 1,
            "active_profile_id": "default",
            "selection_transport": "bridge_profile_registry",
            "profiles": [{"id": "default", "name": "Local bridge"}],
        },
    }
    contract = {
        "playback_ready_for_live_control": "resolved_unrestricted_active_target",
        "risk_taxonomy": ["inactive_device", "volume_unavailable", "zero_volume"],
        "guarded_commands": ["play", "next", "previous"],
    }
    health = {
        "build": {"commit": commit, "ref": ref, "source": source},
        "backend_capabilities": {"architecture": architecture, "devices": {"readiness_contract": contract}},
    }
    config = {
        "build": {"commit": commit, "ref": ref, "source": source},
        "capabilities": {"architecture": architecture, "devices": {"readiness_contract": contract}},
    }
    return health, config


def test_live_deployment_validation_accepts_stamped_contract():
    health, config = payloads()

    assert validate_payloads(health, config, expected_commit="abc123") == []


def test_live_deployment_validation_rejects_unknown_build():
    health, config = payloads(commit="unknown", ref="unknown", source="unknown")

    failures = validate_payloads(health, config, expected_commit=None)

    assert "health build.commit is unknown" in failures
    assert "config build.ref is unknown" in failures


def test_live_deployment_validation_rejects_stale_commit():
    health, config = payloads(commit="old")

    failures = validate_payloads(health, config, expected_commit="new")

    assert "health build.commit 'old' != expected 'new'" in failures
    assert "config build.commit 'old' != expected 'new'" in failures


def test_live_deployment_validation_requires_playback_ready_contract():
    health, config = payloads()
    health["backend_capabilities"]["devices"]["readiness_contract"].pop("playback_ready_for_live_control")

    failures = validate_payloads(health, config, expected_commit="abc123")

    assert any("health readiness_contract.playback_ready_for_live_control" in failure for failure in failures)


def test_live_deployment_validation_requires_profile_registry_contract():
    health, config = payloads()
    health["backend_capabilities"]["architecture"] = {
        "profile_model": "single_bridge_profile",
        "multi_profile_selection": False,
        "multi_profile_selection_blocker": "profile_registry_not_implemented",
    }
    config["capabilities"]["architecture"]["profile_registry"]["selection_transport"] = "manual"

    failures = validate_payloads(health, config, expected_commit="abc123")

    assert "health architecture.profile_model 'single_bridge_profile' != 'profile_registry'" in failures
    assert "health architecture.multi_profile_selection is not true" in failures
    assert (
        "health architecture.multi_profile_selection_blocker must be empty, got 'profile_registry_not_implemented'"
        in failures
    )
    assert "health architecture.profile_registry missing" in failures
    assert (
        "config architecture.profile_registry.selection_transport 'manual' != 'bridge_profile_registry'"
        in failures
    )
