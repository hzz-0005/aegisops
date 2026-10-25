"""Idempotently register the local business topology in the control-plane catalog."""

from uuid import UUID, uuid4

from sqlalchemy import select

from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    ResourceRecord,
    ServiceDependencyRecord,
    ServiceRecord,
)

SERVICES = (
    ("lab-gateway", "gateway-team", "tier-1"),
    ("order-service", "order-team", "tier-1"),
    ("payment-service", "payment-team", "tier-1"),
    ("inventory-service", "inventory-team", "tier-2"),
)
RESOURCES = (
    ("redis-main", "cache", "payment-team"),
    ("orders-postgres", "database", "order-team"),
)
DEPENDENCIES = (
    ("lab-gateway", "order-service", None),
    ("order-service", "payment-service", None),
    ("order-service", "inventory-service", None),
    ("order-service", None, "orders-postgres"),
    ("payment-service", None, "redis-main"),
    ("payment-service", None, "orders-postgres"),
    ("inventory-service", None, "orders-postgres"),
)


def bootstrap(settings: Settings) -> None:
    with session_for(settings.database_url) as session, session.begin():
        service_ids: dict[str, UUID] = {}
        resource_ids: dict[str, UUID] = {}
        for slug, owner, criticality in SERVICES:
            service_row = session.scalar(
                select(ServiceRecord).where(
                    ServiceRecord.tenant_id == settings.dev_tenant_id,
                    ServiceRecord.environment == "lab",
                    ServiceRecord.slug == slug,
                )
            )
            if service_row is None:
                service_row = ServiceRecord(
                    id=uuid4(),
                    tenant_id=settings.dev_tenant_id,
                    slug=slug,
                    environment="lab",
                    owner=owner,
                    criticality=criticality,
                )
                session.add(service_row)
                session.flush()
            service_ids[slug] = service_row.id
        for slug, resource_type, owner in RESOURCES:
            resource_row = session.scalar(
                select(ResourceRecord).where(
                    ResourceRecord.tenant_id == settings.dev_tenant_id,
                    ResourceRecord.environment == "lab",
                    ResourceRecord.slug == slug,
                )
            )
            if resource_row is None:
                resource_row = ResourceRecord(
                    id=uuid4(),
                    tenant_id=settings.dev_tenant_id,
                    slug=slug,
                    environment="lab",
                    resource_type=resource_type,
                    owner=owner,
                )
                session.add(resource_row)
                session.flush()
            resource_ids[slug] = resource_row.id
        for service, dependency_service, dependency_resource in DEPENDENCIES:
            source_id = service_ids[service]
            target_service_id = service_ids[dependency_service] if dependency_service else None
            target_resource_id = resource_ids[dependency_resource] if dependency_resource else None
            existing_dependency = session.scalar(
                select(ServiceDependencyRecord.id).where(
                    ServiceDependencyRecord.tenant_id == settings.dev_tenant_id,
                    ServiceDependencyRecord.service_id == source_id,
                    ServiceDependencyRecord.depends_on_service_id == target_service_id,
                    ServiceDependencyRecord.depends_on_resource_id == target_resource_id,
                )
            )
            if existing_dependency is None:
                session.add(
                    ServiceDependencyRecord(
                        id=uuid4(),
                        tenant_id=settings.dev_tenant_id,
                        service_id=source_id,
                        depends_on_service_id=target_service_id,
                        depends_on_resource_id=target_resource_id,
                    )
                )


if __name__ == "__main__":
    bootstrap(Settings())
    print("lab catalog ready")
