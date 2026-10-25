package aegisops.remediation

default decision := {
    "outcome": "DENY",
    "reason": "proposal failed a policy precondition",
    "policy_version": "remediation-policy-v4",
    "required_role": "",
    "required_approvals": 0,
}

decision := {
    "outcome": "REQUIRE_APPROVAL",
    "reason": "high-risk registered action requires an independent approver",
    "policy_version": "remediation-policy-v4",
    "required_role": "Approver",
    "required_approvals": 1,
} if {
    input.stage == "PLAN"
    input.actor.role == "Operator"
    input.tenant.id != ""
    input.incident.status == "OPEN"
    input.incident.version == input.plan.source_incident_version
    input.plan.status == "PROPOSED"
    input.plan.is_stale == false
    input.action.status == "PROPOSED"
    input.action.snapshot.plan_hash == input.plan.plan_hash
    object.get(input.action.snapshot, "schema_version", 1) == 1
    input.action.snapshot.risk == "HIGH"
    input.action.snapshot.action_type in {"restart_cache", "restart_service", "rollback_service"}
    input.resource.version == input.action.snapshot.resource_version
    input.environment.name == "lab"
    input.runtime.kill_switch == false
}

decision := {
    "outcome": "ALLOW",
    "reason": "approved allowlisted Lab action meets execution gate",
    "policy_version": "remediation-policy-v4",
    "required_role": "",
    "required_approvals": 0,
} if {
    input.stage == "EXECUTE"
    input.actor.role == "Executor"
    input.actor.id == "lab-executor"
    input.tenant.id != ""
    input.incident.status == "OPEN"
    input.incident.version == input.plan.source_incident_version
    input.plan.status == "AUTHORIZED"
    input.plan.is_stale == false
    input.action.status == "AUTHORIZED"
    input.action.snapshot.plan_hash == input.plan.plan_hash
    object.get(input.action.snapshot, "schema_version", 1) == 1
    lab_target_allowed
    input.action.snapshot.risk == "HIGH"
    input.action.snapshot_hash == input.approval.snapshot_hash
    input.approval.status == "APPROVED"
    input.approval.expires_at_unix > input.runtime.now_unix
    input.resource.version == input.action.snapshot.resource_version
    input.environment.name == "lab"
    input.runtime.kill_switch == false
}

lab_target_allowed if {
    input.action.snapshot.action_type == "restart_cache"
    input.resource.kind == "resource"
    input.resource.slug == "redis-main"
}

lab_target_allowed if {
    input.action.snapshot.action_type == "restart_service"
    input.resource.kind == "service"
    input.resource.slug == "order-service"
}

lab_target_allowed if {
    input.action.snapshot.action_type == "rollback_service"
    input.resource.kind == "service"
    input.resource.slug == "order-service"
    params := input.action.snapshot.parameters
    params.to_ref == "baseline"
    params.current_change_id != "baseline"
    params.current_bad_deployment == true
    params.target_bad_deployment == false
    regex.match("^sha256:[0-9a-f]{64}$", params.current_image_id)
    params.current_image_id == params.target_image_id
    regex.match("^[0-9a-f]{64}$", params.current_config_hash)
    regex.match("^[0-9a-f]{64}$", params.target_config_hash)
    params.current_config_hash != params.target_config_hash
}
