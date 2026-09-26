"""
wsgi.py
--------
The entry point a real web server uses, as opposed to app.py which is
the entry point YOU use while developing.

It exists BECAUSE of the name clash app.py has: `app` always resolves
to the app/ package, so `gunicorn app:app` cannot work. `wsgi` clashes
with nothing, so `gunicorn wsgi:app` does. See the banner in app.py.

    app.py   -> Flask's built-in development server. Single-threaded,
                auto-reloads, prints a warning telling you not to use it
                in production. Correct for `python app.py` on localhost.
    wsgi.py  -> imported by gunicorn (see Procfile). No server is
                started here; gunicorn imports the module, takes the
                `app` object, and serves it itself.

Why the split matters: the development server handles one request at a
time and has no process supervision, so a single slow page (this app has
one -- the first city-wide trend sweep) blocks every other visitor.
Gunicorn runs the app under a proper worker model and restarts a worker
that dies.

Defaults to the PRODUCTION config rather than development, because
anything importing this file is, by definition, being served rather than
developed. That also means ProductionConfig.validate() runs, so a
deployment missing its SECRET_KEY fails loudly at boot instead of
serving forgeable login cookies -- see app/config.py.
"""

import os

from dotenv import load_dotenv

# Harmless on a host that has no .env file (Render, Railway and friends
# inject real environment variables instead) -- load_dotenv() simply does
# nothing when the file is absent. Kept so the same command also works
# for a local production-mode dry run, where .env IS where the settings
# live.
load_dotenv()

from app import create_app  # noqa: E402

app = create_app(os.environ.get("FLASK_CONFIG", "production"))
