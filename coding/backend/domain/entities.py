from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from uuid import uuid4

from .errors import EmptyCartError, InsufficientStockError, NotFoundError, ValidationError


def money(value: Decimal | str | int) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValidationError("El precio debe expresarse como Decimal, texto o entero; no se acepta float.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError("El precio debe ser un número decimal válido.") from None
    if not result.is_finite() or result <= 0:
        raise ValidationError("El precio debe ser finito y mayor que cero.")
    return result


def quantity(value: int, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("La cantidad debe ser un número entero.")
    if value < (0 if allow_zero else 1):
        raise ValidationError("La cantidad debe ser mayor que cero." if not allow_zero else "El stock no puede ser negativo.")
    return value


def product_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("El nombre del producto debe ser texto.")
    name = value.strip()
    if not name:
        raise ValidationError("El nombre del producto es obligatorio.")
    if "\n" in name or "\r" in name or "\t" in name:
        raise ValidationError("El nombre del producto no puede contener saltos de línea ni tabuladores.")
    return name


@dataclass(frozen=True)
class Product:
    name: str
    unit_price: Decimal
    stock: int
    registered_at: datetime

    def __post_init__(self) -> None:
        if product_name(self.name) != self.name:
            raise ValidationError("El nombre del producto no puede tener espacios externos.")
        object.__setattr__(self, "unit_price", money(self.unit_price))
        quantity(self.stock, allow_zero=True)
        if not isinstance(self.registered_at, datetime):
            raise ValidationError("La fecha de registro debe ser datetime.")
        if self.registered_at.tzinfo is not None:
            raise ValidationError("La fecha debe usar hora local sin zona horaria explícita.")

    @classmethod
    def create(cls, name: str, unit_price: Decimal | str | int, stock: int,
               registered_at: datetime | None = None) -> Product:
        return cls(product_name(name), money(unit_price), quantity(stock, allow_zero=True),
                   registered_at or datetime.now())

    def restock(self, amount: int) -> Product:
        return replace(self, stock=self.stock + quantity(amount))

    def validate_discount(self, amount: int) -> None:
        amount = quantity(amount)
        if amount > self.stock:
            raise InsufficientStockError(f"Stock insuficiente para «{self.name}»: disponible {self.stock}, solicitado {amount}.")

    def discounted(self, amount: int) -> Product:
        self.validate_discount(amount)
        return replace(self, stock=self.stock - amount)


class Inventory:
    MAX_PRODUCTS = 10_000

    def __init__(self, products: list[Product] | None = None):
        self._products: dict[str, Product] = {}
        for product in products or []:
            self.register(product)

    def register(self, product: Product) -> None:
        if not isinstance(product, Product):
            raise ValidationError("El producto no es válido.")
        if product.name in self._products:
            raise ValidationError(f"Ya existe un producto llamado «{product.name}».")
        if len(self._products) >= self.MAX_PRODUCTS:
            raise ValidationError(f"El inventario admite como máximo {self.MAX_PRODUCTS} productos.")
        self._products[product.name] = product

    def list_products(self) -> list[Product]:
        return sorted(self._products.values(), key=lambda p: p.name.casefold())

    def search(self, text: str) -> list[Product]:
        if not isinstance(text, str):
            raise ValidationError("El texto de búsqueda debe ser texto.")
        needle = text.strip().casefold()
        matches = [p for p in self._products.values() if needle in p.name.casefold()]
        return sorted(matches, key=lambda p: p.name.casefold())

    def get(self, name: str) -> Product:
        try:
            return self._products[name]
        except KeyError:
            raise NotFoundError(f"No existe el producto «{name}».") from None

    def restock(self, name: str, amount: int) -> None:
        self._products[name] = self.get(name).restock(amount)

    def validate_discount_batch(self, items: dict[str, int]) -> None:
        for name, amount in items.items():
            self.get(name).validate_discount(amount)

    def discount_batch(self, items: dict[str, int]) -> None:
        self.validate_discount_batch(items)
        replacements = {name: self.get(name).discounted(amount) for name, amount in items.items()}
        self._products.update(replacements)


@dataclass(frozen=True)
class CartLine:
    product_name: str
    unit_price: Decimal
    quantity: int

    def __post_init__(self) -> None:
        product_name(self.product_name)
        object.__setattr__(self, "unit_price", money(self.unit_price))
        quantity(self.quantity)

    @property
    def subtotal(self) -> Decimal:
        return self.unit_price * self.quantity


class Cart:
    def __init__(self):
        self._lines: dict[str, CartLine] = {}

    @property
    def lines(self) -> tuple[CartLine, ...]:
        return tuple(self._lines.values())

    def add(self, product: Product, amount: int) -> None:
        amount = quantity(amount)
        existing = self._lines.get(product.name)
        total = (existing.quantity if existing else 0) + amount
        product.validate_discount(total)
        self._lines[product.name] = CartLine(product.name, product.unit_price, total)

    def change_quantity(self, product: Product, amount: int) -> None:
        amount = quantity(amount)
        product.validate_discount(amount)
        self._lines[product.name] = CartLine(product.name, product.unit_price, amount)

    def remove(self, name: str) -> None:
        if name not in self._lines:
            raise NotFoundError(f"El producto «{name}» no está en el carrito.")
        del self._lines[name]

    def clear(self) -> None:
        self._lines.clear()

    def total(self) -> Decimal:
        return sum((line.subtotal for line in self._lines.values()), Decimal("0"))

    def quantity_for(self, name: str) -> int:
        line = self._lines.get(name)
        return line.quantity if line else 0


class SaleStatus(str, Enum):
    COMPLETED = "COMPLETADA"


@dataclass(frozen=True)
class SaleLine:
    product_name: str
    unit_price: Decimal
    quantity: int
    subtotal: Decimal

    def __post_init__(self) -> None:
        product_name(self.product_name)
        object.__setattr__(self, "unit_price", money(self.unit_price))
        quantity(self.quantity)
        if self.subtotal != self.unit_price * self.quantity:
            raise ValidationError("El subtotal no coincide con precio por cantidad.")

    @classmethod
    def from_cart_line(cls, line: CartLine) -> SaleLine:
        return cls(line.product_name, line.unit_price, line.quantity, line.subtotal)


@dataclass(frozen=True)
class Sale:
    id: str
    created_at: datetime
    details: tuple[SaleLine, ...]
    total: Decimal
    status: SaleStatus = SaleStatus.COMPLETED

    def __post_init__(self) -> None:
        if not self.id or not isinstance(self.id, str):
            raise ValidationError("El identificador de venta no es válido.")
        if not isinstance(self.created_at, datetime):
            raise ValidationError("La fecha de venta no es válida.")
        if self.created_at.tzinfo is not None:
            raise ValidationError("La fecha de venta debe usar hora local sin zona explícita.")
        if not isinstance(self.status, SaleStatus):
            raise ValidationError("El estado de venta no es válido.")
        if not self.details:
            raise EmptyCartError("No se puede completar una venta sin productos.")
        if not isinstance(self.details, tuple) or not all(isinstance(item, SaleLine) for item in self.details):
            raise ValidationError("Los detalles de la venta deben ser inmutables y válidos.")
        if self.total != sum((line.subtotal for line in self.details), Decimal("0")):
            raise ValidationError("El total no coincide con la suma de subtotales.")

    @classmethod
    def complete(cls, lines: tuple[CartLine, ...], at: datetime | None = None) -> Sale:
        details = tuple(SaleLine.from_cart_line(line) for line in lines)
        if not details:
            raise EmptyCartError("No se puede completar una venta con el carrito vacío.")
        total = sum((line.subtotal for line in details), Decimal("0"))
        return cls(str(uuid4()), at or datetime.now(), details, total)


@dataclass
class AppState:
    inventory: Inventory = field(default_factory=Inventory)
    sales: list[Sale] = field(default_factory=list)

    def add_sale(self, sale: Sale) -> None:
        if any(item.id == sale.id for item in self.sales):
            raise ValidationError("El identificador de venta ya existe.")
        self.sales.append(sale)
        self.sales.sort(key=lambda item: item.created_at)
