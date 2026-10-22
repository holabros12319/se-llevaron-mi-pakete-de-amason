from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from threading import RLock

from backend.domain.entities import AppState, Cart, Product, Sale
from backend.domain.errors import EmptyCartError, NotFoundError, ValidationError


class PosService:
    """Coordinates domain operations and commits candidate states to a repository."""

    def __init__(self, repository, initial_state: AppState | None = None):
        self.repository = repository
        self.state = initial_state or AppState()
        self.cart = Cart()  # Session-only state; it is deliberately not persisted.
        self._lock = RLock()

    @classmethod
    def from_repository(cls, repository) -> PosService:
        loaded = repository.load()
        return cls(repository, loaded if loaded is not None else AppState())

    def _commit(self, candidate: AppState) -> None:
        self.repository.save(candidate)
        self.state = candidate

    def register_product(self, name: str, unit_price, stock: int) -> Product:
        with self._lock:
            product = Product.create(name, unit_price, stock)
            candidate = deepcopy(self.state)
            candidate.inventory.register(product)
            self._commit(candidate)
            return product

    def list_products(self) -> list[Product]:
        with self._lock:
            return self.state.inventory.list_products()

    def search_products(self, text: str) -> list[Product]:
        with self._lock:
            return self.state.inventory.search(text)

    def get_product(self, name: str) -> Product:
        with self._lock:
            return self.state.inventory.get(name)

    def restock(self, name: str, amount: int) -> Product:
        with self._lock:
            candidate = deepcopy(self.state)
            candidate.inventory.restock(name, amount)
            updated = candidate.inventory.get(name)
            self._commit(candidate)
            return updated

    def add_to_cart(self, name: str, amount: int) -> None:
        with self._lock:
            self.cart.add(self.state.inventory.get(name), amount)

    def change_cart_quantity(self, name: str, amount: int) -> None:
        with self._lock:
            self.cart.change_quantity(self.state.inventory.get(name), amount)

    def remove_from_cart(self, name: str) -> None:
        with self._lock:
            self.cart.remove(name)

    def clear_cart(self) -> None:
        with self._lock:
            self.cart.clear()

    def cart_lines(self):
        with self._lock:
            return self.cart.lines

    def cart_total(self):
        with self._lock:
            return self.cart.total()

    def complete_sale(self) -> Sale:
        with self._lock:
            lines = self.cart.lines
            if not lines:
                raise EmptyCartError("No se puede completar una venta con el carrito vacío.")
            candidate = deepcopy(self.state)
            requested = {line.product_name: line.quantity for line in lines}
            # Validate all lines before changing the candidate inventory.
            candidate.inventory.validate_discount_batch(requested)
            sale = Sale.complete(lines)
            candidate.inventory.discount_batch(requested)
            candidate.add_sale(sale)
            self._commit(candidate)  # State and cart remain untouched if persistence fails.
            self.cart.clear()
            return sale

    def list_sales(self) -> list[Sale]:
        with self._lock:
            return sorted(self.state.sales, key=lambda item: item.created_at)

    def save_on_close(self) -> None:
        with self._lock:
            self.repository.save(deepcopy(self.state))

