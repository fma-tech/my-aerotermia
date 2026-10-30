"""
Fase 1 - Prueba de conexion con la Aquarea (Panasonic Comfort Cloud).

Que hace:
  1. Inicia sesion en la nube de Panasonic (con soporte de doble factor / MFA).
  2. Guarda el refresh token en un fichero local para no pedir MFA cada vez.
  3. Lista los equipos Aquarea de la cuenta y muestra su estado.

Es SOLO LECTURA: no cambia nada en la bomba de calor.

Uso:
  pip install aioaquarea-ng==1.5.0
  (1.5.0 o superior: elige sola la version de app que acepta Panasonic)
  python aquarea_test.py

Credenciales (por orden de prioridad):
  - Variables de entorno AQUAREA_USER y AQUAREA_PASS
  - Si no existen, se piden por teclado (la contrasena no se muestra)
"""

import asyncio
import getpass
import os
import stat
import sys
from pathlib import Path

import aiohttp
from aioaquarea import (
    AgreementNotAcceptedError,
    AuthenticationError,
    Client,
    MfaRequiredError,
)

TOKEN_FILE = Path(__file__).with_name(".aquarea_token")


def leer_token() -> str | None:
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        return token or None
    return None


def guardar_token(token: str) -> None:
    """Guarda el refresh token con permisos solo para el propietario (es un secreto)."""
    if not token:
        return
    TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # En Windows chmod tiene efecto limitado


def pedir_credenciales() -> tuple[str, str]:
    user = os.environ.get("AQUAREA_USER") or input("Usuario Panasonic ID (email): ").strip()
    password = os.environ.get("AQUAREA_PASS") or getpass.getpass("Contrasena: ")
    return user, password


async def iniciar_sesion(client: Client) -> None:
    try:
        await client.login()
    except MfaRequiredError as err:
        reto = err.challenge
        if reto is None:
            sys.exit("Panasonic pide un tipo de verificacion no soportado (p. ej. push). "
                     "Cambia el MFA de la cuenta a SMS o app autenticadora.")
        destino = f" ({reto.destination})" if reto.destination else ""
        print(f"\nPanasonic pide verificacion en dos pasos por {reto.factor}{destino}.")
        for intento in range(3):
            codigo = input("Introduce el codigo: ").strip()
            try:
                await client.complete_mfa(codigo)
                return
            except AuthenticationError as e:
                print(f"Codigo no valido o caducado ({e}). Intentos restantes: {2 - intento}")
        sys.exit("No se pudo completar la verificacion.")


def fmt(valor, unidad: str = "") -> str:
    return "-" if valor is None else f"{valor}{unidad}"


def mostrar_dispositivo(device) -> None:
    print("\n" + "=" * 50)
    print(f"Equipo:        {device.device_name}")
    print(f"Modelo:        {fmt(device.model)}")
    print(f"Firmware:      {fmt(device.firmware_version)}")
    print(f"Long ID:       {device.long_id}")
    print(f"Estado:        {device.operation_status.name}")
    print(f"Modo:          {device.mode.name}")
    print(f"T. exterior:   {fmt(device.temperature_outdoor, ' C')}")
    print(f"Presion agua:  {fmt(device.water_pressure, ' bar')}")
    print(f"Enfria:        {'si' if device.support_cooling else 'no'}")
    print(f"Error activo:  {'si (' + str(device.current_error) + ')' if device.is_on_error else 'no'}")

    print("\nZonas:")
    for zone_id, zona in device.zones.items():
        objetivo = getattr(zona, "heat_target_temperature", None)
        print(f"  [{zone_id}] {zona.name}: {zona.operation_status.name}, "
              f"actual {fmt(zona.temperature, ' C')}, objetivo calor {fmt(objetivo, ' C')}")

    if device.has_tank and device.tank:
        t = device.tank
        print("\nDeposito ACS:")
        print(f"  Estado: {t.operation_status.name}, actual {fmt(t.temperature, ' C')}, "
              f"objetivo {fmt(t.target_temperature, ' C')} (rango {t.heat_min}-{t.heat_max} C)")


async def main() -> None:
    user, password = pedir_credenciales()

    async with aiohttp.ClientSession() as session:
        client = Client(
            session=session,
            username=user,
            password=password,
            refresh_token=leer_token(),
            refresh_token_callback=guardar_token,
        )

        try:
            await iniciar_sesion(client)
            guardar_token(getattr(client, "refresh_token", None) or "")
            print("\nSesion iniciada correctamente.")

            dispositivos = await client.get_devices()
            if not dispositivos:
                print("La cuenta no tiene ningun equipo Aquarea asociado.")
                return

            print(f"Equipos encontrados: {len(dispositivos)}")
            for info in dispositivos:
                device = await client.get_device(device_info=info)
                mostrar_dispositivo(device)

        except AgreementNotAcceptedError:
            sys.exit("Panasonic ha publicado nuevas condiciones. Aceptalas en la app "
                     "Comfort Cloud con esta cuenta y vuelve a ejecutar el script.")
        except AuthenticationError as e:
            version = getattr(getattr(client, "_app_version", None), "version", "?")
            sys.exit(f"Error de autenticacion: {e}\n(version de app enviada: {version})")


if __name__ == "__main__":
    asyncio.run(main())
