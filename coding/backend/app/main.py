from __future__ import annotations

import argparse
import os
from pathlib import Path
import tkinter as tkcd
from tkinter import messagebox

from backend.application.service import PosService
from backend.persistence.json_repository import CorruptStateError, JsonRepository, PersistenceError
from frontend.window import PosWindow


def default_data_path() -> Path:
    # Anchor to the project/application directory, never to the process working directory.
    return Path(__file__).resolve().parents[2] / "data" / "datos.json"


def build_service(path: Path) -> tuple[PosService | None, str | None]:
    repository = JsonRepository(path)
    try:
        return PosService.from_repository(repository), None
    except PersistenceError:
        raise
    except CorruptStateError as exc:
        try:
            backup = repository.backup_corrupt()
        except OSError as backup_error:
            raise PersistenceError(f"{exc}\n\nNo se pudo respaldar el archivo; no se continuará para proteger los datos.\n{backup_error}") from backup_error
        return None, f"{exc}\n\nSe creó una copia de seguridad en:\n{backup}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Punto de venta local con persistencia JSON.")
    parser.add_argument("--data-file", type=Path, default=default_data_path(), help="Ruta del archivo de datos JSON.")
    parser.add_argument("--store-name", default=os.environ.get("POS_STORE_NAME"), help="Nombre de la tienda para el ticket.")
    args = parser.parse_args()

    root = tkcd.Tk()
    root.withdraw()
    try:
        service, recovery_message = build_service(args.data_file)
    except PersistenceError as exc:
        messagebox.showerror("No se pudieron leer los datos", str(exc), parent=root)
        root.destroy()
        return
    if service is None:
        proceed = messagebox.askyesno("Archivo de datos inválido",
            recovery_message + "\n\n¿Deseas continuar con un inventario vacío? El respaldo se conservará.", parent=root)
        if not proceed:
            root.destroy()
            return
        service = PosService(JsonRepository(args.data_file))
    root.deiconify()
    PosWindow(root, service, args.store_name)
    root.mainloop()


if __name__ == "__main__":
    main()
