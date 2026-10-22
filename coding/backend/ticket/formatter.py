import textwrap

from backend.domain.entities import Sale


def format_ticket(sale: Sale, store_name: str | None = None) -> str:
    """Render a plain-text receipt. Currency/rounding are intentionally unspecified."""
    heading = (store_name or "").strip() or "[Nombre de tienda pendiente de configurar]"
    lines = [heading, f"Fecha: {sale.created_at.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    name_width = max(12, min(30, max(len(item.product_name) for item in sale.details)))
    quantity_width = max(5, max(len(str(item.quantity)) for item in sale.details))
    price_width = max(len("Precio"), max(len(str(item.unit_price)) for item in sale.details))
    subtotal_width = max(len("Subtotal"), len(str(sale.total)), max(len(str(item.subtotal)) for item in sale.details))
    lines.append(f"{'Producto':<{name_width}} {'Cant.':>{quantity_width}} {'Precio':>{price_width}} {'Subtotal':>{subtotal_width}}")
    for item in sale.details:
        parts = textwrap.wrap(item.product_name, width=name_width, break_long_words=True,
                              break_on_hyphens=False, replace_whitespace=False, drop_whitespace=False) or [""]
        lines.append(f"{parts[0]:<{name_width}} {item.quantity:>{quantity_width}} {str(item.unit_price):>{price_width}} {str(item.subtotal):>{subtotal_width}}")
        lines.extend(f"{part:<{name_width}}" for part in parts[1:])
    lines.extend(["", f"TOTAL: {sale.total}"])
    return "\n".join(lines)
