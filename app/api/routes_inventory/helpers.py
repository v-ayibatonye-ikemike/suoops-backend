"""Helper functions for inventory routes."""

from app.models import inventory_schemas as schemas
from app.storage.s3_client import s3_client


def product_to_out(product) -> schemas.ProductOut:
    """Convert Product model to ProductOut schema."""
    return schemas.ProductOut(
        id=product.id,
        sku=product.sku,
        name=product.name,
        description=product.description,
        barcode=product.barcode,
        category_id=product.category_id,
        category_name=product.category.name if product.category else None,
        cost_price=product.cost_price,
        selling_price=product.selling_price,
        quantity_in_stock=product.quantity_in_stock,
        reorder_level=product.reorder_level,
        reorder_quantity=product.reorder_quantity,
        unit=product.unit,
        is_active=product.is_active,
        track_stock=product.track_stock,
        fulfilment_type=getattr(product, "fulfilment_type", "physical"),
        # Stored product image is a short-lived presigned URL; re-sign so the
        # dashboard inventory thumbnails don't expire (~1h) into broken images.
        image_url=s3_client.refresh_presigned_url(product.image_url),
        is_low_stock=product.is_low_stock,
        is_out_of_stock=product.is_out_of_stock,
        stock_value=product.stock_value,
        profit_margin=product.profit_margin,
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


def movement_to_out(movement) -> schemas.StockMovementOut:
    """Convert StockMovement model to StockMovementOut schema."""
    return schemas.StockMovementOut(
        id=movement.id,
        product_id=movement.product_id,
        product_name=movement.product.name if movement.product else None,
        product_sku=movement.product.sku if movement.product else None,
        movement_type=movement.movement_type.value,
        quantity=movement.quantity,
        quantity_before=movement.quantity_before,
        quantity_after=movement.quantity_after,
        unit_cost=movement.unit_cost,
        total_cost=movement.total_cost,
        reference_type=movement.reference_type,
        reference_id=movement.reference_id,
        reason=movement.reason,
        notes=movement.notes,
        created_at=movement.created_at,
        created_by=movement.created_by,
    )
