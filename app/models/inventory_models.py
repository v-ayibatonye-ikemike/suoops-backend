"""
Inventory management models for small-scale business stock tracking.

Follows OOP principles with proper encapsulation, inheritance (Base), and
clear relationships between entities. Designed to integrate seamlessly
with the existing User, Invoice, and InvoiceLine models.
"""
from __future__ import annotations

import datetime as dt
import enum
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base_class import Base

if TYPE_CHECKING:
    from app.models.models import User


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class StockMovementType(str, enum.Enum):
    """Types of inventory stock movements."""
    PURCHASE = "purchase"           # Stock received from supplier
    SALE = "sale"                   # Stock sold to customer (via invoice)
    ADJUSTMENT = "adjustment"       # Manual adjustment (damage, count correction)
    RETURN_IN = "return_in"         # Customer return (stock back in)
    RETURN_OUT = "return_out"       # Return to supplier
    TRANSFER = "transfer"           # Transfer between locations (future)
    OPENING = "opening"             # Opening stock balance


class ProductCategory(Base):
    """
    Product categories for organizing inventory.
    
    Allows businesses to group products for easier management and reporting.
    Each user has their own set of categories.
    """
    __tablename__ = "product_category"
    __table_args__ = (
        Index("ix_product_category_user_name", "user_id", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)  # Hex color for UI
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Optional packaging/pack fee (₦) added ONCE to a storefront order that
    # contains any product in this category — e.g. takeaway packs for cooked
    # food. NULL/0 means the category needs no packaging.
    pack_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="product_categories")
    products: Mapped[list[Product]] = relationship(
        "Product",
        back_populates="category",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<ProductCategory(id={self.id}, name='{self.name}')>"


class Product(Base):
    """
    Product/Item in inventory.
    
    Represents a unique product that can be tracked in inventory.
    Each user has their own product catalog with independent SKUs.
    
    OOP Principles:
    - Encapsulation: Business logic methods for stock calculations
    - Single Responsibility: Handles product info, delegates stock to InventoryItem
    """
    __tablename__ = "product"
    __table_args__ = (
        Index("ix_product_user_sku", "user_id", "sku", unique=True),
        Index("ix_product_user_name", "user_id", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), nullable=False, index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("product_category.id"), nullable=True, index=True)
    
    # Product identification
    sku: Mapped[str] = mapped_column(String(50), nullable=False)  # Stock Keeping Unit
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    barcode: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    
    # Pricing
    cost_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)  # Purchase/cost price
    selling_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)  # Default selling price
    
    # Stock management
    quantity_in_stock: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reorder_level: Mapped[int] = mapped_column(Integer, default=10, server_default="10")  # Low stock alert threshold
    reorder_quantity: Mapped[int] = mapped_column(Integer, default=20, server_default="20")  # Suggested reorder qty
    unit: Mapped[str] = mapped_column(String(20), default="pcs", server_default="pcs")  # pieces, kg, liters, etc.
    
    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    track_stock: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
    )  # Whether to track inventory
    # Fulfilment kind: "physical" (default) needs delivery; "service" / "digital"
    # do not — no courier, no delivery address, and a faster buyer-protection
    # window since there's nothing to ship.
    fulfilment_type: Mapped[str] = mapped_column(
        String(20), default="physical", server_default="physical", nullable=False
    )
    
    # Media
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Opt-out of the storefront-wide social promotion opt-in (User.
    # social_promotion_opt_in) for this specific product — e.g. a business
    # opts in generally but wants one item kept out of public posts.
    exclude_from_social: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    # Metadata
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        onupdate=utcnow,
        nullable=True,
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="products")
    category: Mapped[ProductCategory | None] = relationship("ProductCategory", back_populates="products")
    stock_movements: Mapped[list[StockMovement]] = relationship(
        "StockMovement",
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="StockMovement.created_at.desc()",
    )

    def __repr__(self) -> str:
        return f"<Product(id={self.id}, sku='{self.sku}', name='{self.name}')>"

    # Business Logic Methods (Encapsulation)
    @property
    def is_low_stock(self) -> bool:
        """Check if current stock is at or below reorder level."""
        return self.quantity_in_stock <= self.reorder_level

    @property
    def is_out_of_stock(self) -> bool:
        """Check if product is completely out of stock."""
        return self.quantity_in_stock <= 0

    @property
    def stock_value(self) -> Decimal:
        """Calculate total value of current stock at cost price."""
        cost = self.cost_price or self.selling_price
        return cost * Decimal(self.quantity_in_stock)

    @property
    def potential_revenue(self) -> Decimal:
        """Calculate potential revenue if all stock is sold."""
        return self.selling_price * Decimal(self.quantity_in_stock)

    @property
    def profit_margin(self) -> Decimal | None:
        """Calculate profit margin percentage."""
        if not self.cost_price or self.cost_price == 0:
            return None
        margin = ((self.selling_price - self.cost_price) / self.cost_price) * 100
        return margin.quantize(Decimal("0.01"))

    def adjust_stock(self, quantity_change: int) -> None:
        """
        Adjust stock quantity. Positive adds, negative removes.
        
        Note: This only updates the quantity. The caller should also
        create a StockMovement record for audit trail.
        """
        new_quantity = self.quantity_in_stock + quantity_change
        if new_quantity < 0:
            raise ValueError(
                "Insufficient stock. Current: "
                f"{self.quantity_in_stock}, Requested change: {quantity_change}"
            )
        self.quantity_in_stock = new_quantity


class StockMovement(Base):
    """
    Record of all stock movements for audit trail.
    
    Every change to product quantity is recorded here for complete
    traceability and reporting. Follows the immutable event pattern.
    
    OOP Principles:
    - Single Responsibility: Records stock changes only
    - Immutability: Records should not be modified after creation
    """
    __tablename__ = "stock_movement"
    __table_args__ = (
        Index("ix_stock_movement_product_date", "product_id", "created_at"),
        Index("ix_stock_movement_user_date", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), nullable=False, index=True)
    
    # Movement details
    movement_type: Mapped[StockMovementType] = mapped_column(
        Enum(StockMovementType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        index=True,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)  # Positive for in, negative for out
    quantity_before: Mapped[int] = mapped_column(Integer, nullable=False)  # Stock level before this movement
    quantity_after: Mapped[int] = mapped_column(Integer, nullable=False)  # Stock level after this movement
    
    # Financial tracking
    unit_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2),
        nullable=True,
    )  # Cost per unit at time of movement
    total_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2),
        nullable=True,
    )  # Total cost of this movement
    
    # Reference to source document
    reference_type: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )  # 'invoice', 'purchase_order', 'adjustment'
    reference_id: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )  # ID of the referenced document
    invoice_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoiceline.id"),
        nullable=True,
    )  # Link to invoice line
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier.id"),
        nullable=True,
    )  # Link to supplier for purchases
    
    # Additional info
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)  # Reason for adjustment
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    # Metadata
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)  # Username or 'system'

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="stock_movements")
    product: Mapped[Product] = relationship("Product", back_populates="stock_movements")

    def __repr__(self) -> str:
        return (
            f"<StockMovement(id={self.id}, product_id={self.product_id}, "
            f"type={self.movement_type}, qty={self.quantity})>"
        )


class Supplier(Base):
    """
    Supplier/Vendor for inventory purchases.
    
    Tracks suppliers from whom products are purchased.
    Useful for purchase orders and supplier management.
    """
    __tablename__ = "supplier"
    __table_args__ = (
        Index("ix_supplier_user_name", "user_id", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), nullable=False, index=True)
    
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        onupdate=utcnow,
        nullable=True,
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="suppliers")

    def __repr__(self) -> str:
        return f"<Supplier(id={self.id}, name='{self.name}')>"


class PurchaseOrderStatus(str, enum.Enum):
    """Status of a purchase order."""
    DRAFT = "draft"               # Auto-generated, needs review
    PENDING = "pending"           # Submitted, awaiting supplier
    CONFIRMED = "confirmed"       # Supplier confirmed
    RECEIVED = "received"         # Goods received, stock updated
    CANCELLED = "cancelled"       # Order cancelled


class PurchaseOrder(Base):
    """
    Purchase order for restocking inventory.
    
    Can be auto-generated when stock falls below reorder level,
    or manually created. When received, updates inventory automatically.
    """
    __tablename__ = "purchase_order"
    __table_args__ = (
        Index("ix_purchase_order_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), nullable=False, index=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("supplier.id"), nullable=True, index=True)
    
    # Order identification
    order_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    status: Mapped[PurchaseOrderStatus] = mapped_column(
        Enum(PurchaseOrderStatus),
        default=PurchaseOrderStatus.DRAFT,
        server_default="draft",
        index=True,
    )
    
    # Financial
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    
    # Auto-generation tracking
    auto_generated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    trigger_invoice_id: Mapped[str | None] = mapped_column(String(50), nullable=True)  # Invoice that triggered this PO
    
    # Dates
    order_date: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    expected_date: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_date: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="purchase_orders")
    supplier: Mapped[Supplier | None] = relationship("Supplier")
    lines: Mapped[list[PurchaseOrderLine]] = relationship(
        "PurchaseOrderLine",
        back_populates="purchase_order",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<PurchaseOrder(id={self.id}, order_number='{self.order_number}', status={self.status})>"


class PurchaseOrderLine(Base):
    """Line item in a purchase order."""
    __tablename__ = "purchase_order_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_order.id"), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), nullable=False, index=True)
    
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    total_cost: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    
    # Track quantities received (for partial deliveries)
    quantity_received: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    
    # Relationships
    purchase_order: Mapped[PurchaseOrder] = relationship("PurchaseOrder", back_populates="lines")
    product: Mapped[Product] = relationship("Product")

    def __repr__(self) -> str:
        return f"<PurchaseOrderLine(id={self.id}, product_id={self.product_id}, qty={self.quantity})>"
