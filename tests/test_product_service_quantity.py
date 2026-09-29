"""Service/digital products can set quantity inline; physical stock stays audited."""

from decimal import Decimal

from app.models import models
from app.models.inventory_schemas import ProductCreate, ProductUpdate
from app.services.inventory.product_service import ProductService


def _owner(db, phone):
    u = models.User(phone=phone, name="Owner", business_name="Biz")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_service_quantity_is_editable_inline(db_session):
    owner = _owner(db_session, "+2348160000040")
    svc = ProductService(db_session, owner.id)
    product = svc.create_product(
        ProductCreate(
            name="Consultation",
            selling_price=Decimal("5000"),
            fulfilment_type="service",
            track_stock=False,
            quantity_in_stock=0,
        )
    )
    # Adding an availability quantity to a service applies directly (no stock
    # movement needed) and flips it to tracked/limited.
    updated = svc.update_product(product.id, ProductUpdate(quantity_in_stock=5, track_stock=True))
    assert updated.quantity_in_stock == 5
    assert updated.track_stock is True
    assert updated.fulfilment_type == "service"


def test_physical_quantity_not_editable_inline(db_session):
    owner = _owner(db_session, "+2348160000041")
    svc = ProductService(db_session, owner.id)
    product = svc.create_product(
        ProductCreate(
            name="Mug",
            selling_price=Decimal("3000"),
            fulfilment_type="physical",
            track_stock=True,
            quantity_in_stock=10,
        )
    )
    # Physical stock must go through stock movements — a direct update is ignored.
    updated = svc.update_product(product.id, ProductUpdate(quantity_in_stock=999))
    assert updated.quantity_in_stock == 10
