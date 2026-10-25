package aegisops.remediation_test

import data.aegisops.remediation

base_input := {
    "stage": "PLAN",
    "actor": {"role": "Operator"},
    "tenant": {"id": "tenant-1"},
    "incident": {"status": "OPEN", "version": 3},
    "plan": {"status": "PROPOSED", "source_incident_version": 3, "is_stale": false, "plan_hash": "hash"},
    "action": {"status": "PROPOSED", "snapshot": {"plan_hash": "hash", "risk": "HIGH", "resource_version": 1, "action_type": "restart_cache"}},
    "resource": {"version": 1},
    "environment": {"name": "lab"},
    "runtime": {"kill_switch": false},
}

test_execution_requires_current_approval if {
    execution := object.union(base_input, {
        "stage": "EXECUTE",
        "actor": {"id": "lab-executor", "role": "Executor"},
        "plan": {"status": "AUTHORIZED", "source_incident_version": 3, "is_stale": false, "plan_hash": "hash"},
        "action": {"status": "AUTHORIZED", "snapshot_hash": "snapshot", "snapshot": {"plan_hash": "hash", "risk": "HIGH", "resource_version": 1, "action_type": "restart_cache"}},
        "approval": {"status": "APPROVED", "snapshot_hash": "snapshot", "expires_at_unix": 20},
        "resource": {"kind": "resource", "slug": "redis-main", "version": 1},
        "runtime": {"kill_switch": false, "now_unix": 10},
    })
    result := remediation.decision with input as execution
    result.outcome == "ALLOW"
}

test_execution_expired_approval_denied if {
    execution := object.union(base_input, {
        "stage": "EXECUTE",
        "actor": {"id": "lab-executor", "role": "Executor"},
        "plan": {"status": "AUTHORIZED", "source_incident_version": 3, "is_stale": false, "plan_hash": "hash"},
        "action": {"status": "AUTHORIZED", "snapshot_hash": "snapshot", "snapshot": {"plan_hash": "hash", "risk": "HIGH", "resource_version": 1, "action_type": "restart_cache"}},
        "approval": {"status": "APPROVED", "snapshot_hash": "snapshot", "expires_at_unix": 10},
        "resource": {"kind": "resource", "slug": "redis-main", "version": 1},
        "runtime": {"kill_switch": false, "now_unix": 10},
    })
    result := remediation.decision with input as execution
    result.outcome == "DENY"
}

test_order_service_restart_allowed if {
    execution := object.union(base_input, {
        "stage": "EXECUTE",
        "actor": {"id": "lab-executor", "role": "Executor"},
        "plan": {"status": "AUTHORIZED", "source_incident_version": 3, "is_stale": false, "plan_hash": "hash"},
        "action": {"status": "AUTHORIZED", "snapshot_hash": "snapshot", "snapshot": {"plan_hash": "hash", "risk": "HIGH", "resource_version": 1, "action_type": "restart_service"}},
        "approval": {"status": "APPROVED", "snapshot_hash": "snapshot", "expires_at_unix": 20},
        "resource": {"kind": "service", "slug": "order-service", "version": 1},
        "runtime": {"kill_switch": false, "now_unix": 10},
    })
    result := remediation.decision with input as execution
    result.outcome == "ALLOW"
}

test_order_service_restart_wrong_target_denied if {
    execution := object.union(base_input, {
        "stage": "EXECUTE",
        "actor": {"id": "lab-executor", "role": "Executor"},
        "plan": {"status": "AUTHORIZED", "source_incident_version": 3, "is_stale": false, "plan_hash": "hash"},
        "action": {"status": "AUTHORIZED", "snapshot_hash": "snapshot", "snapshot": {"plan_hash": "hash", "risk": "HIGH", "resource_version": 1, "action_type": "restart_service"}},
        "approval": {"status": "APPROVED", "snapshot_hash": "snapshot", "expires_at_unix": 20},
        "resource": {"kind": "service", "slug": "payment-service", "version": 1},
        "runtime": {"kill_switch": false, "now_unix": 10},
    })
    result := remediation.decision with input as execution
    result.outcome == "DENY"
}

test_order_rollback_scoped_and_approved if {
    execution := object.union(base_input, {
        "stage": "EXECUTE",
        "actor": {"id": "lab-executor", "role": "Executor"},
        "plan": {"status": "AUTHORIZED", "source_incident_version": 3, "is_stale": false, "plan_hash": "hash"},
        "action": {"status": "AUTHORIZED", "snapshot_hash": "snapshot", "snapshot": {
            "plan_hash": "hash", "risk": "HIGH", "resource_version": 1,
            "action_type": "rollback_service", "parameters": {
                "to_ref": "baseline", "current_change_id": "bad-123",
                "current_bad_deployment": true, "target_bad_deployment": false,
                "current_image_id": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "target_image_id": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "current_config_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "target_config_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            },
        }},
        "approval": {"status": "APPROVED", "snapshot_hash": "snapshot", "expires_at_unix": 20},
        "resource": {"kind": "service", "slug": "order-service", "version": 1},
        "runtime": {"kill_switch": false, "now_unix": 10},
    })
    result := remediation.decision with input as execution
    result.outcome == "ALLOW"
}

test_order_rollback_missing_fingerprint_denied if {
    execution := object.union(base_input, {
        "stage": "EXECUTE",
        "actor": {"id": "lab-executor", "role": "Executor"},
        "plan": {"status": "AUTHORIZED", "source_incident_version": 3, "is_stale": false, "plan_hash": "hash"},
        "action": {"status": "AUTHORIZED", "snapshot_hash": "snapshot", "snapshot": {
            "plan_hash": "hash", "risk": "HIGH", "resource_version": 1,
            "action_type": "rollback_service", "parameters": {"to_ref": "baseline"},
        }},
        "approval": {"status": "APPROVED", "snapshot_hash": "snapshot", "expires_at_unix": 20},
        "resource": {"kind": "service", "slug": "order-service", "version": 1},
        "runtime": {"kill_switch": false, "now_unix": 10},
    })
    result := remediation.decision with input as execution
    result.outcome == "DENY"
}

test_high_risk_requires_approval if {
    result := remediation.decision with input as base_input
    result.outcome == "REQUIRE_APPROVAL"
    result.required_approvals == 1
}

test_stale_plan_denied if {
    modified := object.union(base_input, {"plan": object.union(base_input.plan, {"is_stale": true})})
    result := remediation.decision with input as modified
    result.outcome == "DENY"
}

test_resource_version_change_denied if {
    modified := object.union(base_input, {"resource": {"version": 2}})
    result := remediation.decision with input as modified
    result.outcome == "DENY"
}

test_kill_switch_denied if {
    modified := object.union(base_input, {"runtime": {"kill_switch": true}})
    result := remediation.decision with input as modified
    result.outcome == "DENY"
}

test_unknown_action_denied if {
    modified := object.union(base_input, {"action": {"status": "PROPOSED", "snapshot": {"plan_hash": "hash", "risk": "HIGH", "resource_version": 1, "action_type": "arbitrary_shell"}}})
    result := remediation.decision with input as modified
    result.outcome == "DENY"
}

test_v2_snapshot_plan_denied_by_lab_policy if {
    modified := object.union(base_input, {"action": {"status": "PROPOSED", "snapshot": {
        "schema_version": 2, "plan_hash": "hash", "risk": "HIGH",
        "resource_version": 1, "action_type": "restart_cache",
    }}})
    result := remediation.decision with input as modified
    result.outcome == "DENY"
}

test_v2_snapshot_execution_denied_by_lab_policy if {
    execution := object.union(base_input, {
        "stage": "EXECUTE",
        "actor": {"id": "lab-executor", "role": "Executor"},
        "plan": {"status": "AUTHORIZED", "source_incident_version": 3, "is_stale": false, "plan_hash": "hash"},
        "action": {"status": "AUTHORIZED", "snapshot_hash": "snapshot", "snapshot": {
            "schema_version": 2, "plan_hash": "hash", "risk": "HIGH",
            "resource_version": 1, "action_type": "restart_cache",
        }},
        "approval": {"status": "APPROVED", "snapshot_hash": "snapshot", "expires_at_unix": 20},
        "resource": {"kind": "resource", "slug": "redis-main", "version": 1},
        "runtime": {"kill_switch": false, "now_unix": 10},
    })
    result := remediation.decision with input as execution
    result.outcome == "DENY"
}
