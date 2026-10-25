from fastapi import HTTPException, Request

from lab.services.common import create_app
from lab.services.contracts import CatalogProduct, InventoryItem
from lab.services.database_telemetry import observed_database_read

app = create_app("inventory-service")


@app.get("/catalog", response_model=list[CatalogProduct])
def list_catalog(request: Request) -> list[CatalogProduct]:
    with observed_database_read(request, service="inventory-service",
                                operation="catalog.list") as connection:
        rows = connection.execute(
            "SELECT sku, available, unit_price_cents, currency, name, description, category "
            "FROM lab.inventory_items ORDER BY category, sku"
        ).fetchall()
    return [
        CatalogProduct(**dict(zip(CatalogProduct.model_fields, row, strict=True)))
        for row in rows
    ]


@app.get("/inventory/{sku}", response_model=InventoryItem)
def get_inventory(sku: str, request: Request) -> InventoryItem:
    with observed_database_read(request, service="inventory-service",
                                operation="inventory.lookup") as connection:
        row = connection.execute(
            "SELECT available, unit_price_cents, currency FROM lab.inventory_items WHERE sku = %s",
            (sku,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown SKU")
    return InventoryItem(sku=sku, available=row[0], unit_price_cents=row[1], currency=row[2])
