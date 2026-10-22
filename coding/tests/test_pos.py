from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from backend.application.service import PosService
from backend.application.sale_gate import SaleGate
from backend.domain.entities import AppState, Cart, Inventory, Product, Sale, SaleLine
from backend.domain.errors import EmptyCartError, InsufficientStockError, ValidationError
from backend.persistence.json_repository import CorruptStateError, JsonRepository, PersistenceError
from backend.ticket.formatter import format_ticket


class MemoryRepository:
    def __init__(self, state=None, fail=False):
        self.state = state
        self.fail = fail
        self.saves = 0

    def load(self):
        return self.state

    def save(self, state):
        if self.fail:
            raise PersistenceError("simulated disk failure")
        self.state = state
        self.saves += 1


class DomainTests(unittest.TestCase):
    def test_price_and_quantity_validation(self):
        for bad in ("0", "-1", "NaN", "Infinity", 1.2, True):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                Product.create("Item", bad, 0)
        for bad in (-1, 1.5, True):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                Product.create("Item", "1.00", bad)
        for bad in (0, -1, 1.5, True):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                Product.create("Item", "1.00", 1).validate_discount(bad)

    def test_inventory_invariants_and_capacity(self):
        product = Product.create("Item", "3.20", 2)
        with self.assertRaises(InsufficientStockError):
            product.discounted(3)
        self.assertEqual(product.stock, 2)
        self.assertEqual(product.discounted(2).stock, 0)
        with self.assertRaises(ValidationError):
            Inventory([Product.create(str(i), "1", 0) for i in range(10_001)])

    def test_search_at_capacity_is_under_200ms_in_domain(self):
        inventory = Inventory([Product.create(f"Producto {i:05}", "1", 0) for i in range(10_000)])
        started = time.perf_counter()
        matches = inventory.search("Producto 099")
        elapsed = time.perf_counter() - started
        self.assertTrue(matches)
        self.assertLess(elapsed, 0.2, f"búsqueda de dominio tardó {elapsed:.3f}s")

    def test_cart_totals_and_invalid_lines(self):
        item = Product.create("Cable", "15.50", 6)
        cart = Cart()
        cart.add(item, 2)
        self.assertEqual(cart.total(), Decimal("31.00"))
        with self.assertRaises(InsufficientStockError):
            cart.add(item, 5)
        with self.assertRaises(InsufficientStockError):
            cart.change_quantity(item, 7)
        cart.change_quantity(item, 3)
        self.assertEqual(cart.total(), Decimal("46.50"))

    def test_empty_sale_rejected_and_snapshot_retained(self):
        with self.assertRaises(EmptyCartError):
            Sale.complete(())
        line = SaleLine("Cable", Decimal("2.50"), 2, Decimal("5.00"))
        sale = Sale("id", datetime(2026, 1, 1), (line,), Decimal("5.00"))
        self.assertEqual(sale.details[0].unit_price, Decimal("2.50"))
        ticket = format_ticket(sale)
        self.assertIn("[Nombre de tienda pendiente de configurar]", ticket)
        self.assertIn("2.50", ticket)


class ApplicationTests(unittest.TestCase):
    def service(self, products=(), fail=False):
        repository = MemoryRepository(AppState(Inventory(list(products)), []), fail=fail)
        return PosService(repository, repository.state), repository

    def test_sale_success_discount_history_and_cart_empty(self):
        service, repository = self.service([Product.create("A", "2.25", 5), Product.create("B", "3", 2)])
        service.add_to_cart("A", 2)
        service.add_to_cart("B", 1)
        sale = service.complete_sale()
        self.assertEqual(service.get_product("A").stock, 3)
        self.assertEqual(service.get_product("B").stock, 1)
        self.assertEqual(sale.total, Decimal("7.50"))
        self.assertEqual(service.list_sales(), [sale])
        self.assertEqual(service.cart_lines(), ())
        self.assertEqual(repository.state.inventory.get("A").stock, 3)

    def test_insufficient_stock_on_final_confirmation_is_all_or_nothing(self):
        service, _ = self.service([Product.create("A", "2", 3), Product.create("B", "5", 2)])
        service.add_to_cart("A", 2)
        service.add_to_cart("B", 2)
        # Simulate stock depletion outside this session after cart validation.
        service.state.inventory._products["B"] = Product.create("B", "5", 1)
        with self.assertRaises(InsufficientStockError):
            service.complete_sale()
        self.assertEqual(service.get_product("A").stock, 3)
        self.assertEqual(service.get_product("B").stock, 1)
        self.assertEqual(service.list_sales(), [])
        self.assertEqual(len(service.cart_lines()), 2)

    def test_empty_cart_and_second_confirmation_do_not_create_duplicate(self):
        service, _ = self.service([Product.create("A", "2", 3)])
        with self.assertRaises(EmptyCartError):
            service.complete_sale()
        service.add_to_cart("A", 1)
        first = service.complete_sale()
        with self.assertRaises(EmptyCartError):
            service.complete_sale()
        self.assertEqual(len(service.list_sales()), 1)
        self.assertEqual(service.list_sales()[0].id, first.id)

    def test_persistence_failure_keeps_state_and_cart_for_retry(self):
        service, repository = self.service([Product.create("A", "2", 4)], fail=True)
        service.add_to_cart("A", 2)
        before = service.state
        with self.assertRaises(PersistenceError):
            service.complete_sale()
        self.assertIs(service.state, before)
        self.assertEqual(service.get_product("A").stock, 4)
        self.assertEqual(service.list_sales(), [])
        self.assertEqual(len(service.cart_lines()), 1)
        repository.fail = False
        service.complete_sale()
        self.assertEqual(service.get_product("A").stock, 2)

    def test_register_and_restock_persist_before_memory_commit(self):
        service, repo = self.service()
        service.register_product("A", "1.25", 2)
        self.assertEqual(repo.saves, 1)
        service.restock("A", 3)
        self.assertEqual(service.get_product("A").stock, 5)
        self.assertEqual(repo.saves, 2)

    def test_failed_mutation_does_not_update_memory(self):
        service, _ = self.service([Product.create("A", "1", 2)], fail=True)
        original = service.state
        with self.assertRaises(PersistenceError):
            service.restock("A", 1)
        self.assertIs(service.state, original)
        self.assertEqual(service.get_product("A").stock, 2)


class JsonRepositoryTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[2] / ".test-tmp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.path = Path(self.temp.name) / "datos.json"
        self.repo = JsonRepository(self.path)

    def tearDown(self):
        self.temp.cleanup()
        try:
            (Path(__file__).resolve().parents[2] / ".test-tmp").rmdir()
        except OSError:
            pass

    def test_missing_file_and_valid_round_trip(self):
        self.assertIsNone(self.repo.load())
        service = PosService(self.repo)
        service.register_product("A", "1.20", 3)
        service.add_to_cart("A", 2)
        sale = service.complete_sale()
        loaded = self.repo.load()
        self.assertEqual(loaded.inventory.get("A").stock, 1)
        self.assertEqual(loaded.sales[0].id, sale.id)
        self.assertEqual(loaded.sales[0].details[0].unit_price, Decimal("1.20"))

    def test_load_10k_products_under_three_seconds(self):
        data = {
            "version": 1,
            "inventory": [{"name": f"Producto {i:05}", "unit_price": "12.50", "stock": i,
                           "registered_at": "2026-10-21 12:00:00"} for i in range(10_000)],
            "sales": [],
        }
        self.path.write_text(json.dumps(data), encoding="utf-8")
        started = time.perf_counter()
        state = self.repo.load()
        elapsed = time.perf_counter() - started
        self.assertEqual(len(state.inventory.list_products()), 10_000)
        self.assertLess(elapsed, 3.0, f"carga de JSON de 10,000 productos tardó {elapsed:.3f}s")

    def test_corrupt_json_is_rejected_and_backup_preserves_original(self):
        self.path.write_text('{broken json', encoding="utf-8")
        original = self.path.read_bytes()
        with self.assertRaises(CorruptStateError):
            self.repo.load()
        backup = self.repo.backup_corrupt()
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(self.path.read_bytes(), original)
        second = self.repo.backup_corrupt()
        self.assertNotEqual(second, backup)
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_schema_is_not_silently_replaced(self):
        self.path.write_text(json.dumps({"version": 1, "inventory": [], "sales": [], "extra": 1}), encoding="utf-8")
        original = self.path.read_bytes()
        with self.assertRaises(CorruptStateError):
            self.repo.load()
        self.assertEqual(self.path.read_bytes(), original)

    def test_atomic_replace_failure_leaves_original_and_no_success(self):
        self.path.write_text("original", encoding="utf-8")
        with patch("backend.persistence.json_repository.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(PersistenceError):
                self.repo.save(AppState())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_read_permission_failure_is_not_treated_as_corrupt(self):
        self.path.write_text("{}", encoding="utf-8")
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(PersistenceError):
                self.repo.load()

    def test_history_keeps_sale_price_after_restock(self):
        service = PosService(self.repo)
        service.register_product("A", "2.50", 4)
        service.add_to_cart("A", 1)
        sale = service.complete_sale()
        service.restock("A", 1)
        loaded = self.repo.load()
        self.assertEqual(loaded.inventory.get("A").stock, 4)
        self.assertEqual(loaded.sales[0].details[0].unit_price, Decimal("2.50"))
        self.assertEqual(loaded.sales[0].total, sale.total)


class SaleGateTests(unittest.TestCase):
    def test_gate_blocks_double_click_and_long_running_sale(self):
        gate = SaleGate(1.5)
        self.assertTrue(gate.start(10.0))
        self.assertFalse(gate.start(10.01))
        self.assertFalse(gate.can_start(11.6))  # Work is still running past cooldown.
        gate.finish()
        self.assertTrue(gate.can_start(11.6))
        self.assertTrue(gate.start(11.6))  # A later sale is allowed after the first finishes.
        self.assertFalse(gate.start(11.61))
        gate.finish()
        self.assertFalse(gate.can_start(12.0))
        self.assertTrue(gate.can_start(13.2))


if __name__ == "__main__":
    unittest.main()
