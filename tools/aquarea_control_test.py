"""
Fase 2 - Prueba de CONTROL de la Aquarea (Panasonic Comfort Cloud).

Que hace (pidiendo confirmacion antes de cambiar nada):
  1. Lee y guarda el estado actual.
  2. Enciende la zona en modo FRIO.
  3. Apaga la zona dejando el ACS (agua caliente) encendido.
  4. Deja el equipo como estaba al principio (tambien si hay un error o cancelas).

Entre cada paso espera a que pulses Intro, para que lo compruebes en la app
Comfort Cloud. Tambien mide cuanto tarda cada orden (Alexa corta a los ~8 s).

Uso (en la misma carpeta y entorno que aquarea_test.py):
  python aquarea_control_test.py
"""

import asyncio
import getpass
import os
import re
import stat
import sys
import time
from pathlib import Path

import aiohttp
import aioaquarea
from aioaquarea import (
    AgreementNotAcceptedError,
    AuthenticationError,
    Client,
    MfaRequiredError,
    OperationStatus,
    UpdateOperationMode,
)

TOKEN_FILE = Path(__file__).with_name(".aquarea_token")
ESPERA_TRAS_ORDEN = 15  # segundos para que la bomba aplique el cambio antes de releer

# ---------------------------------------------------------------------------
# Parche para aioaquarea-ng 1.4.x (octubre 2026): Panasonic exige la version de
# la app de Android. Desde la 1.5.0 la libreria lo resuelve sola y no se aplica.
# ---------------------------------------------------------------------------
if not hasattr(aioaquarea, "AppVersionRejectedError"):
    from aioaquarea import auth as _aq_auth

    _PLAY_URL = ("https://play.google.com/store/apps/details"
                 "?id=com.panasonic.ACCsmart&hl=fr&gl=FR")

    async def _refresh_version_android(self) -> None:
        forzada = os.environ.get("AQUAREA_APP_VERSION")
        if forzada:
            self.version = forzada
            return
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(_PLAY_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        m = re.search(r'\[\[\["(\d+\.\d+\.\d+)"\]\]', await r.text())
                        if m:
                            self.version = m.group(1)
                            return
        except Exception:
            pass
        if self.version == _aq_auth.DEFAULT_X_APP_VERSION:
            self.version = "5.1.0"

    _aq_auth.CCAppVersion.refresh = _refresh_version_android


# Modo leido del equipo -> modo que hay que enviar para volver a el
MODO_A_ENVIO = {
    "HEAT": UpdateOperationMode.HEAT,
    "COOL": UpdateOperationMode.COOL,
    "AUTO_HEAT": UpdateOperationMode.AUTO,
    "AUTO_COOL": UpdateOperationMode.AUTO,
}


# ------------------------------- sesion ------------------------------------
def leer_token() -> str | None:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip() or None
    return None


def guardar_token(token: str) -> None:
    if not token:
        return
    TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


async def iniciar_sesion(client: Client) -> None:
    try:
        await client.login()
    except MfaRequiredError as err:
        reto = err.challenge
        if reto is None:
            sys.exit("Panasonic pide un tipo de verificacion no soportado (p. ej. push).")
        print(f"\nPanasonic pide verificacion en dos pasos por {reto.factor}.")
        for intento in range(3):
            try:
                await client.complete_mfa(input("Introduce el codigo: ").strip())
                return
            except AuthenticationError as e:
                print(f"Codigo no valido ({e}). Intentos restantes: {2 - intento}")
        sys.exit("No se pudo completar la verificacion.")


# ------------------------------- utilidades --------------------------------
def foto(device) -> dict:
    """Estado relevante del equipo en un diccionario."""
    return {
        "equipo": device.operation_status.name,
        "modo": device.mode.name,
        "zonas": {zid: z.operation_status.name for zid, z in device.zones.items()},
        "acs": device.tank.operation_status.name if device.has_tank and device.tank else "-",
        "acs_temp": device.tank.temperature if device.has_tank and device.tank else None,
    }


def mostrar(titulo: str, f: dict) -> None:
    zonas = ", ".join(f"zona {k}: {v}" for k, v in f["zonas"].items())
    print(f"\n[{titulo}]")
    print(f"  Equipo: {f['equipo']} | Modo: {f['modo']} | {zonas} | "
          f"ACS: {f['acs']} ({f['acs_temp']} C)")


async def ejecutar(nombre: str, coro, device) -> dict:
    print(f"\n>>> {nombre}...")
    t0 = time.monotonic()
    await coro
    print(f"    Orden aceptada por Panasonic en {time.monotonic() - t0:.1f} s")
    print(f"    Esperando {ESPERA_TRAS_ORDEN} s a que la bomba lo aplique...")
    await asyncio.sleep(ESPERA_TRAS_ORDEN)
    await device.refresh_data()
    f = foto(device)
    mostrar(f"Despues de: {nombre}", f)
    return f


class Cancelado(Exception):
    pass


def pausa(texto: str) -> None:
    r = input(f"\n{texto}\nPulsa Intro para continuar (o escribe N para cancelar y restaurar): ")
    if r.strip().upper() == "N":
        raise Cancelado()


async def restaurar(device, inicial: dict, zona_id: int) -> None:
    """Deja la zona y el modo como estaban al empezar (el ACS no se toca)."""
    await device.refresh_data()
    if foto(device)["zonas"] == inicial["zonas"] and device.mode.name == inicial["modo"]:
        print("\nEl equipo ya esta como al principio. Nada que restaurar.")
        return
    print("\n>>> Restaurando el estado inicial...")
    if inicial["zonas"].get(zona_id) == "ON":
        modo = MODO_A_ENVIO.get(inicial["modo"], UpdateOperationMode.COOL)
        await device.set_mode(modo, zona_id)
    else:
        await device.set_mode(UpdateOperationMode.OFF, zona_id)
    await asyncio.sleep(ESPERA_TRAS_ORDEN)
    await device.refresh_data()
    mostrar("Estado final", foto(device))


# ------------------------------- prueba ------------------------------------
async def main() -> None:
    user = os.environ.get("AQUAREA_USER") or input("Usuario Panasonic ID (email): ").strip()
    password = os.environ.get("AQUAREA_PASS") or getpass.getpass("Contrasena: ")

    async with aiohttp.ClientSession() as session:
        client = Client(session=session, username=user, password=password,
                        refresh_token=leer_token(), refresh_token_callback=guardar_token)
        try:
            await iniciar_sesion(client)
            guardar_token(getattr(client, "refresh_token", None) or "")

            dispositivos = await client.get_devices()
            if not dispositivos:
                sys.exit("La cuenta no tiene ningun equipo Aquarea.")
            device = await client.get_device(device_info=dispositivos[0])
            zona_id = next(iter(device.zones))

            inicial = foto(device)
            mostrar("Estado inicial (se restaurara al final)", inicial)

            if not device.support_cooling:
                sys.exit("Este equipo no admite frio; la prueba no aplica.")

            print("\nEsta prueba VA A CAMBIAR tu aerotermia durante unos minutos:")
            print(f"  1) Encender la zona {zona_id} en modo FRIO")
            print(f"  2) Apagar la zona {zona_id} dejando el ACS encendido")
            print("  3) Dejarlo todo como al principio")
            if input("\nEscribe SI para empezar: ").strip().upper() != "SI":
                print("Cancelado. No se ha cambiado nada.")
                return

            try:
                f1 = await ejecutar("Encender zona en FRIO",
                                    device.set_mode(UpdateOperationMode.COOL, zona_id), device)
                ok1 = f1["zonas"].get(zona_id) == "ON" and f1["modo"] in ("COOL", "AUTO_COOL")
                print("    Resultado:", "OK" if ok1 else "NO SE APLICO como se esperaba")
                pausa("Comprueba en la app Comfort Cloud que la zona esta encendida en frio.")

                f2 = await ejecutar("Apagar zona (ACS sigue encendido)",
                                    device.set_mode(UpdateOperationMode.OFF, zona_id), device)
                ok2 = f2["zonas"].get(zona_id) == "OFF" and f2["acs"] == inicial["acs"]
                print("    Resultado:", "OK" if ok2 else "NO SE APLICO como se esperaba")
                if f2["acs"] != inicial["acs"]:
                    print("    ATENCION: el estado del ACS ha cambiado respecto al inicial.")
            except Cancelado:
                print("\nCancelado por ti.")
            finally:
                await restaurar(device, inicial, zona_id)

            print("\nPrueba terminada. Pasame todo lo que ha salido en pantalla.")

        except AgreementNotAcceptedError:
            sys.exit("Acepta las nuevas condiciones en la app Comfort Cloud y repite.")
        except AuthenticationError as e:
            sys.exit(f"Error de autenticacion: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrumpido.")
