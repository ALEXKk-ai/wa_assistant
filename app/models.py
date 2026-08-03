"""Database models.

Every business-scoped table carries a business_id foreign key and every
repository function in app/repositories.py takes business_id as an explicit,
required argument (see repositories.py module docstring for why we chose
explicit params over a context-object for this v1 scale).

SQLite is used for v1 (single-operator, a handful of businesses). The models
avoid SQLite-specific types so a later move to Postgres is a connection
string + Alembic migration, not a rewrite - see README "Scaling beyond v1".
"""
import enum
from datetime import datetime

from sqlalchemy import (
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class FlexibleEnum(str, enum.Enum):
    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            val_upper = value.upper()
            for member in cls:
                if member.name == val_upper or member.value.upper() == val_upper:
                    return member
        return None


class BusinessType(FlexibleEnum):
    SERVICES = "services"
    GOODS = "goods"


class ConfirmationMode(FlexibleEnum):
    """Whether a paid booking/order confirms itself automatically, or waits
    for the owner to explicitly CONFIRM/REJECT it via WhatsApp."""

    AUTOMATIC = "automatic"
    MANUAL = "manual"


class FulfillmentMode(FlexibleEnum):
    DELIVERY_ONLY = "delivery_only"
    PICKUP_ONLY = "pickup_only"
    BOTH = "both"


class PaymentStatus(FlexibleEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class BookingStatus(FlexibleEnum):
    PENDING_DEPOSIT = "pending_deposit"
    AWAITING_OWNER_CONFIRMATION = "awaiting_owner_confirmation"
    AWAITING_RESCHEDULE_CONFIRMATION = "awaiting_reschedule_confirmation"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class OrderStatus(FlexibleEnum):
    PENDING_DEPOSIT = "pending_deposit"
    AWAITING_OWNER_CONFIRMATION = "awaiting_owner_confirmation"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


from sqlalchemy.types import String as StringType, TypeDecorator


class RobustEnumType(TypeDecorator):
    """Custom SQLAlchemy type decorator that handles both member names and member values
    case-insensitively when reading from DB columns."""

    impl = StringType
    cache_ok = True

    def __init__(self, enum_class, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.enum_class = enum_class

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, enum.Enum):
            return value.value
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return value
        val_str = str(value).strip().lower()
        for member in self.enum_class:
            if member.value.lower() == val_str or member.name.lower() == val_str:
                return member
        return self.enum_class(value)


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    business_type: Mapped[BusinessType] = mapped_column(RobustEnumType(BusinessType))

    # WhatsApp - phone_number_id is not secret, the access token is.
    whatsapp_phone_number_id: Mapped[str] = mapped_column(String(64), unique=True)
    whatsapp_token_encrypted: Mapped[str] = mapped_column(String, default="")

    # M-Pesa - shortcode/passkey are per-business (each business has its own
    # till/paybill so deposits land in the right owner's account).
    mpesa_shortcode: Mapped[str] = mapped_column(String(20), default="")
    mpesa_passkey_encrypted: Mapped[str] = mapped_column(String, default="")
    mpesa_consumer_key_encrypted: Mapped[str] = mapped_column(String, default="")
    mpesa_consumer_secret_encrypted: Mapped[str] = mapped_column(String, default="")

    # Deposit policy: either a flat amount or a percentage of the
    # service/product price; percentage takes precedence if set.
    deposit_flat_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    deposit_percentage: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)

    owner_whatsapp_number: Mapped[str] = mapped_column(String(20))

    # AUTOMATIC: paid deposit confirms the booking/order immediately.
    # MANUAL: paid deposit moves it to "awaiting owner confirmation" and the
    # owner must explicitly reply CONFIRM/REJECT via WhatsApp (see
    # app/engine.py:handle_owner_command).
    confirmation_mode: Mapped[ConfirmationMode] = mapped_column(
        RobustEnumType(ConfirmationMode), default=ConfirmationMode.AUTOMATIC
    )
    fulfillment_mode: Mapped[FulfillmentMode] = mapped_column(
        RobustEnumType(FulfillmentMode), default=FulfillmentMode.BOTH
    )

    # JSON blob, see app/hours.py for the schema and parsing. "{}" (the
    # default) means "no restriction" - see hours.py's migration note.
    hours_json: Mapped[str] = mapped_column(default="{}")
    # Stored for reference/display only - datetimes throughout this app are
    # naive (no tzinfo), treated as the business's own local time. Adding
    # real timezone-aware datetime handling is a bigger change than v1
    # needs; this field exists so the operator's intent is on record and
    # AI Q&A can mention it, not to drive any conversion logic yet.
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Nairobi")

    # Address & Extra Info / FAQ grounding
    address_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    extra_info_text: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    services: Mapped[list["Service"]] = relationship(back_populates="business")
    products: Mapped[list["Product"]] = relationship(back_populates="business")


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    price: Mapped[float] = mapped_column(Numeric(10, 2))
    duration_minutes: Mapped[int] = mapped_column(default=60)
    deposit_flat_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    deposit_percentage: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    active: Mapped[bool] = mapped_column(default=True)

    business: Mapped["Business"] = relationship(back_populates="services")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    price: Mapped[float] = mapped_column(Numeric(10, 2))
    stock_qty: Mapped[int] = mapped_column(default=0)
    deposit_flat_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    deposit_percentage: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    active: Mapped[bool] = mapped_column(default=True)

    business: Mapped["Business"] = relationship(back_populates="products")


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("business_id", "phone_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    phone_number: Mapped[str] = mapped_column(String(20))
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ConversationState(Base):
    """Tracks where a customer is mid-flow (e.g. picked a service, awaiting
    slot choice) so a multi-turn WhatsApp conversation survives across
    separate webhook calls (each inbound message is its own HTTP request)."""

    __tablename__ = "conversation_states"
    __table_args__ = (UniqueConstraint("business_id", "customer_phone"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    customer_phone: Mapped[str] = mapped_column(String(20))
    state_json: Mapped[str] = mapped_column(default="{}")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    slot_start: Mapped[datetime]
    slot_end: Mapped[datetime]
    # Proposed new slot while a confirmed booking's reschedule awaits owner approval
    # (manual mode). Current slot_start/slot_end stay unchanged until CONFIRM.
    proposed_slot_start: Mapped[datetime | None] = mapped_column(nullable=True)
    proposed_slot_end: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus), default=BookingStatus.PENDING_DEPOSIT
    )
    deposit_amount: Mapped[float] = mapped_column(Numeric(10, 2))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        # Prevents two CONFIRMED bookings for the same business/service/slot.
        # Partial/filtered unique indexes aren't portable across SQLite and
        # Postgres in the same syntax, so double-booking is additionally
        # guarded at the application layer in repositories.create_booking
        # via a transaction + explicit overlap check. This index catches the
        # exact-slot race case cheaply regardless.
        Index("ix_booking_slot", "business_id", "service_id", "slot_start"),
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    items_json: Mapped[str]  # [{"product_id": .., "qty": .., "unit_price": ..}]
    total_amount: Mapped[float] = mapped_column(Numeric(10, 2))
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING_DEPOSIT)
    deposit_amount: Mapped[float] = mapped_column(Numeric(10, 2))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Payment(Base):
    """One row per M-Pesa STK Push attempt. This is the sole source of truth
    for idempotency: a callback is only ever applied once against a payment
    row, keyed by the M-Pesa-issued checkout_request_id (unique)."""

    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("checkout_request_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    checkout_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    merchant_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus), default=PaymentStatus.PENDING)
    mpesa_receipt: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_callback_json: Mapped[str | None] = mapped_column(nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class AuditEvent(Base):
    """Immutable audit trail log of state transitions and critical actions."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(50))  # "booking" | "order"
    entity_id: Mapped[int] = mapped_column()
    actor: Mapped[str] = mapped_column(String(50))  # "customer" | "owner" | "system"
    action: Mapped[str] = mapped_column(String(50))  # "CREATE" | "CONFIRM" | "REJECT" | "CANCEL" | "RESCHEDULE"
    previous_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    new_status: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
