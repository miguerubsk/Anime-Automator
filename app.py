"""
Punto de entrada para gunicorn (`app:app`).

Todo el código vive en el paquete `automator/` (ver su __init__.py para la
organización). Aquí solo se crea la app web y se arrancan los servicios.
"""

from automator import bootstrap
from automator.web import create_app

app = create_app()
bootstrap.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
