from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from backend.application.sale_gate import SaleGate
from backend.domain.errors import ValidationError
from backend.ticket.formatter import format_ticket


class PosWindow:
    def __init__(self, root: tk.Tk, service, store_name: str | None = None):
        self.root = root
        self.service = service
        self.store_name = store_name
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pos-sale")
        self._sale_gate = SaleGate()
        self._sale_future = None
        self._closing = False
        root.title("Punto de venta")
        root.geometry("1120x720")
        root.minsize(900, 600)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self.refresh_all()

    def _build(self):
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=12)
        self.inventory_tab = ttk.Frame(self.tabs, padding=12)
        self.sale_tab = ttk.Frame(self.tabs, padding=12)
        self.history_tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(self.inventory_tab, text="Inventario")
        self.tabs.add(self.sale_tab, text="Punto de venta")
        self.tabs.add(self.history_tab, text="Historial")
        self._build_inventory()
        self._build_sales()
        self._build_history()

    @staticmethod
    def _tree(parent, columns, headings, widths, height=12):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=height, selectmode="browse")
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, anchor="w" if column in ("name", "product") else "e")
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=yscroll.set)
        tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        return frame, tree

    def _build_inventory(self):
        form = ttk.LabelFrame(self.inventory_tab, text="Registrar producto", padding=10)
        form.pack(fill="x", pady=(0, 10))
        self.new_name = tk.StringVar()
        self.new_price = tk.StringVar()
        self.new_stock = tk.StringVar(value="0")
        for index, (label, var, width) in enumerate((("Nombre", self.new_name, 28), ("Precio unitario", self.new_price, 16), ("Stock inicial", self.new_stock, 12))):
            ttk.Label(form, text=label).grid(row=0, column=index * 2, sticky="w", padx=(0, 6))
            ttk.Entry(form, textvariable=var, width=width).grid(row=0, column=index * 2 + 1, sticky="w", padx=(0, 14))
        ttk.Button(form, text="Guardar producto", command=self.register_product).grid(row=0, column=6, padx=6)

        tools = ttk.Frame(self.inventory_tab)
        tools.pack(fill="x", pady=(0, 8))
        self.search_text = tk.StringVar()
        ttk.Label(tools, text="Buscar por nombre").pack(side="left")
        entry = ttk.Entry(tools, textvariable=self.search_text, width=40)
        entry.pack(side="left", padx=8)
        self.search_text.trace_add("write", lambda *_: self.refresh_inventory())
        ttk.Button(tools, text="Reabastecer seleccionado", command=self.restock_selected).pack(side="right")
        ttk.Button(tools, text="Ver detalle", command=self.show_product_detail).pack(side="right", padx=8)
        frame, self.inventory_tree = self._tree(self.inventory_tab,
            ("name", "stock", "price", "registered"), ("Producto", "Stock", "Precio unitario", "Fecha de registro"),
            (350, 100, 150, 220), 18)
        frame.pack(fill="both", expand=True)
        self.inventory_tree.bind("<<TreeviewSelect>>", self._select_inventory_product)

    def _build_sales(self):
        ttk.Label(self.sale_tab, text="Catálogo disponible").pack(anchor="w")
        frame, self.catalog_tree = self._tree(self.sale_tab, ("name", "stock", "price"),
            ("Producto", "Disponible", "Precio unitario"), (470, 120, 170), 10)
        frame.pack(fill="both", expand=True, pady=(4, 8))
        controls = ttk.Frame(self.sale_tab)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Label(controls, text="Cantidad").pack(side="left")
        self.sale_quantity = tk.StringVar(value="1")
        ttk.Spinbox(controls, from_=1, to=10_000, textvariable=self.sale_quantity, width=8).pack(side="left", padx=8)
        ttk.Button(controls, text="Agregar al carrito", command=self.add_selected_to_cart).pack(side="left")
        ttk.Button(controls, text="Cambiar cantidad de línea", command=self.change_selected_cart_line).pack(side="left", padx=8)
        ttk.Button(controls, text="Quitar línea", command=self.remove_selected_cart_line).pack(side="left")
        ttk.Button(controls, text="Vaciar carrito", command=self.clear_cart).pack(side="right")
        cart_frame, self.cart_tree = self._tree(self.sale_tab, ("name", "price", "quantity", "subtotal"),
            ("Producto", "Precio", "Cantidad", "Subtotal"), (440, 140, 100, 150), 8)
        cart_frame.pack(fill="both", expand=True)
        bottom = ttk.Frame(self.sale_tab)
        bottom.pack(fill="x", pady=(8, 0))
        self.total_label = ttk.Label(bottom, text="Total: 0", font=("Segoe UI", 14, "bold"))
        self.total_label.pack(side="left")
        self.charge_button = ttk.Button(bottom, text="Concretar venta", command=self.complete_sale)
        self.charge_button.pack(side="right")

    def _build_history(self):
        frame, self.history_tree = self._tree(self.history_tab, ("id", "date", "items", "total"),
            ("ID de venta", "Fecha y hora", "Artículos", "Total"), (300, 220, 420, 130), 22)
        frame.pack(fill="both", expand=True)
        ttk.Button(self.history_tab, text="Ver ticket seleccionado", command=self.show_selected_ticket).pack(anchor="e", pady=8)

    @staticmethod
    def _parse_stock(raw: str) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValidationError("El stock debe ser un número entero.") from None
        if value < 0:
            raise ValidationError("El stock no puede ser negativo.")
        return value

    @staticmethod
    def _parse_sale_quantity(raw: str) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValidationError("La cantidad debe ser un número entero.") from None
        if value <= 0:
            raise ValidationError("La cantidad debe ser mayor que cero.")
        return value

    def _guard(self, callback):
        try:
            return callback()
        except (ValidationError, LookupError, OSError) as exc:
            messagebox.showerror("No se pudo completar la operación", str(exc), parent=self.root)
            return None

    def register_product(self):
        def action():
            self.service.register_product(self.new_name.get(), Decimal(self.new_price.get()), self._parse_stock(self.new_stock.get()))
            self.new_name.set("")
            self.new_price.set("")
            self.new_stock.set("0")
            self.refresh_all()
        try:
            self._guard(action)
        except (InvalidOperation, ValueError):
            messagebox.showerror("Dato inválido", "El precio debe ser un decimal válido.", parent=self.root)

    def refresh_inventory(self):
        if not hasattr(self, "inventory_tree"):
            return
        rows = self.service.search_products(self.search_text.get())
        self.inventory_tree.delete(*self.inventory_tree.get_children())
        for product in rows:
            self.inventory_tree.insert("", "end", iid=product.name,
                values=(product.name, product.stock, str(product.unit_price), product.registered_at.strftime("%Y-%m-%d %H:%M:%S")))

    def refresh_catalog(self):
        self.catalog_tree.delete(*self.catalog_tree.get_children())
        for product in self.service.list_products():
            self.catalog_tree.insert("", "end", iid=product.name,
                values=(product.name, product.stock, str(product.unit_price)))

    def refresh_cart(self):
        self.cart_tree.delete(*self.cart_tree.get_children())
        for line in self.service.cart_lines():
            self.cart_tree.insert("", "end", iid=line.product_name,
                values=(line.product_name, str(line.unit_price), line.quantity, str(line.subtotal)))
        self.total_label.configure(text=f"Total: {self.service.cart_total()}")

    def refresh_history(self):
        self.history_tree.delete(*self.history_tree.get_children())
        for sale in self.service.list_sales():
            items = ", ".join(f"{line.product_name} × {line.quantity}" for line in sale.details)
            self.history_tree.insert("", "end", iid=sale.id, values=(sale.id, sale.created_at.strftime("%Y-%m-%d %H:%M:%S"), items, str(sale.total)))

    def refresh_all(self):
        self.refresh_inventory()
        self.refresh_catalog()
        self.refresh_cart()
        self.refresh_history()

    def _select_inventory_product(self, _event=None):
        selected = self.inventory_tree.selection()
        if selected:
            self.catalog_tree.selection_set(selected[0]) if selected[0] in self.catalog_tree.get_children() else None

    def _selected(self, tree):
        selection = tree.selection()
        if not selection:
            raise ValidationError("Selecciona un producto primero.")
        return selection[0]

    def show_product_detail(self):
        name = self._guard(lambda: self._selected(self.inventory_tree))
        if name:
            product = self.service.get_product(name)
            messagebox.showinfo("Detalle del producto",
                f"Nombre: {product.name}\nPrecio unitario: {product.unit_price}\nStock actual: {product.stock}\nFecha de registro: {product.registered_at:%Y-%m-%d %H:%M:%S}", parent=self.root)

    def restock_selected(self):
        name = self._guard(lambda: self._selected(self.inventory_tree))
        if not name:
            return
        raw = simpledialog.askstring("Reabastecer", "Cantidad a agregar:", parent=self.root)
        if raw is None:
            return
        def action():
            amount = self._parse_stock(raw)
            if amount <= 0:
                raise ValidationError("La cantidad a reabastecer debe ser mayor que cero.")
            self.service.restock(name, amount)
            self.refresh_all()
        self._guard(action)

    def add_selected_to_cart(self):
        name = self._guard(lambda: self._selected(self.catalog_tree))
        if name:
            self._guard(lambda: self.service.add_to_cart(name, self._parse_sale_quantity(self.sale_quantity.get())))
            self.refresh_cart()

    def change_selected_cart_line(self):
        name = self._guard(lambda: self._selected(self.cart_tree))
        if name:
            self._guard(lambda: self.service.change_cart_quantity(name, self._parse_sale_quantity(self.sale_quantity.get())))
            self.refresh_cart()

    def remove_selected_cart_line(self):
        name = self._guard(lambda: self._selected(self.cart_tree))
        if name:
            self._guard(lambda: self.service.remove_from_cart(name))
            self.refresh_cart()

    def clear_cart(self):
        self.service.clear_cart()
        self.refresh_cart()

    def complete_sale(self):
        if not self._sale_gate.start(time.monotonic()):
            return
        self.charge_button.state(["disabled"])
        self.root.after(1500, self._cooldown_elapsed)
        try:
            self._sale_future = self.executor.submit(self.service.complete_sale)
        except Exception as exc:
            self._sale_gate.finish()
            messagebox.showerror("No se pudo completar la venta", str(exc), parent=self.root)
            self._update_charge_button()
            return
        self.root.after(50, self._poll_sale)

    def _cooldown_elapsed(self):
        self._update_charge_button()

    def _poll_sale(self):
        if self._sale_future is None:
            return
        if not self._sale_future.done():
            self.root.after(50, self._poll_sale)
            return
        future, self._sale_future = self._sale_future, None
        self._sale_gate.finish()
        try:
            sale = future.result()
        except Exception as exc:
            messagebox.showerror("No se pudo completar la venta", str(exc), parent=self.root)
            self.refresh_all()
        else:
            self.refresh_all()
            self._show_ticket(sale)
        self._update_charge_button()

    def _update_charge_button(self):
        if self._sale_gate.can_start(time.monotonic()):
            self.charge_button.state(["!disabled"])
        else:
            self.charge_button.state(["disabled"])

    def _show_ticket(self, sale):
        window = tk.Toplevel(self.root)
        window.title("Ticket de venta")
        text = tk.Text(window, width=62, height=max(12, len(sale.details) + 7), font=("Consolas", 11), wrap="none")
        text.pack(fill="both", expand=True, padx=12, pady=12)
        text.insert("1.0", format_ticket(sale, self.store_name))
        text.configure(state="disabled")

    def show_selected_ticket(self):
        sale_id = self._guard(lambda: self._selected(self.history_tree))
        if sale_id:
            sale = next(s for s in self.service.list_sales() if s.id == sale_id)
            self._show_ticket(sale)

    def close(self):
        if self._sale_future is not None:
            messagebox.showwarning("Operación en curso", "Espera a que termine el cobro antes de cerrar.", parent=self.root)
            return
        try:
            self.service.save_on_close()
        except OSError as exc:
            messagebox.showerror("No se guardaron los datos", str(exc), parent=self.root)
            return
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

