from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from backend.domain.entities import AppState, Inventory, Product, Sale, SaleLine, SaleStatus
from backend.domain.errors import ValidationError


class CorruptStateError(ValueError):
    """Existing state file cannot be trusted or loaded."""


class PersistenceError(OSError):
    """A state could not be safely read or persisted."""


class JsonRepository:
    VERSION = 1

    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()

    def load(self) -> AppState | None:
        if not self.path.exists():
            return None
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                raw = json.load(stream)
            return self._decode(raw)
        except OSError as exc:
            raise PersistenceError(f"No se pudo leer {self.path}: {exc}") from exc
        except (json.JSONDecodeError, UnicodeError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise CorruptStateError(f"No se pudo cargar {self.path}: {exc}") from exc

    def save(self, state: AppState) -> None:
        temp_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = self._encode(state)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix=f".{self.path.name}.", suffix=".tmp",
                                             delete=False, newline="\n") as stream:
                temp_path = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
            # Flush directory metadata where supported, without turning a successful replace into failure.
            if hasattr(os, "O_DIRECTORY"):
                try:
                    fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                except OSError:
                    pass
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            raise PersistenceError(f"No se pudieron guardar los datos en {self.path}: {exc}") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def backup_corrupt(self) -> Path:
        if not self.path.exists():
            raise PersistenceError("No existe un archivo que se pueda respaldar.")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        base = self.path.with_name(f"{self.path.name}.corrupto_{stamp}.bak")
        target = base
        counter = 1
        while target.exists():
            target = base.with_name(f"{base.name}_{counter}")
            counter += 1
        try:
            # 'xb' makes non-overwrite a filesystem-enforced guarantee.
            with self.path.open("rb") as source, target.open("xb") as backup:
                while chunk := source.read(1024 * 1024):
                    backup.write(chunk)
                backup.flush()
                os.fsync(backup.fileno())
            return target
        except OSError as exc:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise PersistenceError(f"No se pudo crear el respaldo del archivo dañado: {exc}") from exc

    @staticmethod
    def _encode(state: AppState) -> dict:
        stamp = lambda value: value.strftime("%Y-%m-%d %H:%M:%S")
        return {
            "version": JsonRepository.VERSION,
            "inventory": [{
                "name": p.name, "unit_price": str(p.unit_price), "stock": p.stock,
                "registered_at": stamp(p.registered_at),
            } for p in state.inventory.list_products()],
            "sales": [{
                "id": sale.id, "created_at": stamp(sale.created_at),
                "status": sale.status.value, "total": str(sale.total),
                "details": [{"product_name": line.product_name,
                             "unit_price": str(line.unit_price), "quantity": line.quantity,
                             "subtotal": str(line.subtotal)} for line in sale.details],
            } for sale in sorted(state.sales, key=lambda item: item.created_at)],
        }

    @staticmethod
    def _decode(raw) -> AppState:
        if not isinstance(raw, dict) or isinstance(raw.get("version"), bool) or raw.get("version") != JsonRepository.VERSION:
            raise CorruptStateError("Versión o raíz JSON no reconocida.")
        if set(raw) != {"version", "inventory", "sales"}:
            raise CorruptStateError("La estructura del estado contiene campos faltantes o desconocidos.")
        if not isinstance(raw["inventory"], list) or not isinstance(raw["sales"], list):
            raise CorruptStateError("Inventario e historial deben ser listas.")
        if len(raw["inventory"]) > Inventory.MAX_PRODUCTS:
            raise CorruptStateError("El archivo excede el máximo de 10,000 productos.")
        products = []
        for row in raw["inventory"]:
            if not isinstance(row, dict) or set(row) != {"name", "unit_price", "stock", "registered_at"}:
                raise CorruptStateError("Registro de producto inválido.")
            if not isinstance(row["unit_price"], str) or isinstance(row["stock"], bool) or not isinstance(row["stock"], int):
                raise CorruptStateError("Tipo inválido en precio o stock del producto.")
            if not isinstance(row["registered_at"], str):
                raise CorruptStateError("Fecha de producto inválida.")
            products.append(Product.create(row["name"], Decimal(row["unit_price"]), row["stock"],
                                           datetime.strptime(row["registered_at"], "%Y-%m-%d %H:%M:%S")))
        inventory = Inventory(products)
        sales = []
        ids = set()
        for row in raw["sales"]:
            if not isinstance(row, dict) or set(row) != {"id", "created_at", "status", "total", "details"}:
                raise CorruptStateError("Registro de venta inválido.")
            if row["id"] in ids:
                raise CorruptStateError("Hay identificadores de venta duplicados.")
            ids.add(row["id"])
            if row["status"] != SaleStatus.COMPLETED.value or not isinstance(row["total"], str) or not isinstance(row["details"], list):
                raise CorruptStateError("Estado, total o detalles de venta inválidos.")
            details = []
            for item in row["details"]:
                if not isinstance(item, dict) or set(item) != {"product_name", "unit_price", "quantity", "subtotal"}:
                    raise CorruptStateError("Detalle de venta inválido.")
                if not isinstance(item["unit_price"], str) or not isinstance(item["subtotal"], str):
                    raise CorruptStateError("Importes de detalle deben ser cadenas decimales.")
                details.append(SaleLine(item["product_name"], Decimal(item["unit_price"]),
                                         item["quantity"], Decimal(item["subtotal"])))
            if not isinstance(row["created_at"], str):
                raise CorruptStateError("Fecha de venta inválida.")
            sales.append(Sale(row["id"], datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S"), tuple(details),
                              Decimal(row["total"]), SaleStatus.COMPLETED))
        if sales != sorted(sales, key=lambda item: item.created_at):
            raise CorruptStateError("El historial no está ordenado cronológicamente.")
        return AppState(inventory, sales)

