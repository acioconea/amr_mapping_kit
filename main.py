import argparse
import logging
import sys
import uvicorn
import asyncio

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Configurare de bază pentru logging la nivelul întregului sistem
# Se aplică înainte ca orice alt modul să fie importat
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)  # Output standard pentru Docker/Systemd
    ]
)

logger = logging.getLogger("amr_main")


def parse_arguments():
    """
    Parsează argumentele din linia de comandă pentru o configurare flexibilă a mediului.
    """
    parser = argparse.ArgumentParser(
        description="DAFI AMR Mapping System - Edge Controller",
        epilog="Exemplu: python main.py --host http://127.0.0.1/ --port 8080 --env production"
    )

    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Interfața de rețea pe care va rula API-ul (default: http://127.0.0.1/)"
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Portul pe care va rula serverul (default: 8000)"
    )

    parser.add_argument(
        "--env",
        type=str,
        choices=["development", "production"],
        default="production",
        help="Mediul de rulare (afectează auto-reload și log level)"
    )

    return parser.parse_args()


def main():
    """
    Funcția principală de Bootstrap a sistemului.
    """
    args = parse_arguments()

    logger.info("=" * 50)
    logger.info("Pornire Sistem AMR Mapping (Edge Controller)")
    logger.info(f"Mediu: {args.env.upper()}")
    logger.info(f"Host: {args.host} | Port: {args.port}")
    logger.info("=" * 50)

    # Setăm auto-reload doar pentru dezvoltare (în producție consumă CPU inutil)
    is_reload = True if args.env == "development" else False

    # Ajustăm nivelul de logare în funcție de mediu
    if args.env == "development":
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug mode activat. Jurnalele detaliate vor fi afișate.")
    else:
        logging.getLogger().setLevel(logging.INFO)

    try:
        # Lansăm serverul ASGI (Uvicorn) indicând calea către aplicația FastAPI
        # Formatul este "pachet.modul:instanță_app"
        from api.server import app
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            # reload=is_reload,
            log_level="debug" if args.env == "development" else "info"
        )
    except KeyboardInterrupt:
        logger.info("Oprire manuală a sistemului (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Eroare fatală la pornirea sistemului: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()