"""
app.py
-------
Entry point for LOCAL DEVELOPMENT. From the flask_webapp_dss folder,
with your virtual environment activated:

    python app.py

Then open http://127.0.0.1:5000 in your browser.

=====================================================================
READ THIS BEFORE POINTING A WEB SERVER AT THIS FILE
=====================================================================
This file is called app.py and it sits next to a PACKAGE that is also
called app (the app/ folder). Python resolves that clash in one
direction only, always:

    import app   ->  app/__init__.py   (the package)
    import app   ->  never app.py      (this file)

A directory containing __init__.py wins over a same-named .py file in
the same folder. That is measured behaviour, not a guess.

So `python app.py` works -- you are running this file by PATH, not
importing it by name -- but nothing can ever `import app` and reach
what is written here. Which means:

    gunicorn app:app     WRONG. Imports the PACKAGE, finds no object
                         called `app` inside it, and dies with
                         "Failed to find attribute 'app' in 'app'".

    gunicorn wsgi:app    CORRECT. wsgi.py has no name clash, and it
                         builds the application through the factory
                         with the PRODUCTION config. That is what
                         Render runs -- see the Procfile and
                         DEPLOYMENT.md.

If the ambiguity ever bothers you, rename THIS file to main.py rather
than renaming the app/ package: every import in the project points at
the package, and there are hundreds of them.
=====================================================================
"""

import importlib.util
import os

from dotenv import load_dotenv

load_dotenv()  # reads .env into environment variables before the app reads config


def _assert_app_means_the_package():
    """Fail with an explanation instead of a circular-import traceback.

    The name `app` MUST resolve to the app/ package, because every
    import inside that package (`from app.extensions import db`, and a
    hundred others) depends on it. It normally does: a folder holding
    __init__.py is a REGULAR package, and a regular package outranks a
    module of the same name.

    But a folder WITHOUT __init__.py is a PEP 420 namespace package,
    and a namespace package ranks BELOW a same-named module. So if
    app/__init__.py is ever missing -- most easily by not being
    committed -- `app` silently starts meaning THIS FILE, this file
    imports itself, and Python reports a circular import that says
    nothing about the actual cause. That exact failure cost a deploy
    once; this turns it into one readable sentence.
    """
    spec = importlib.util.find_spec("app")
    if spec is not None and spec.submodule_search_locations is not None:
        return  # `app` is the package, as it should be
    raise ImportError(
        "The name 'app' resolved to this file (app.py) instead of the app/ package.\n"
        "That means app/__init__.py is missing or was not committed. Without it, app/ is a\n"
        "namespace package, which Python ranks BELOW a module of the same name.\n"
        "Fix: make sure app/__init__.py exists and is in your repository\n"
        "     (git add -f app/__init__.py && git commit && git push)."
    )


_assert_app_means_the_package()

from app import create_app  # noqa: E402  -- resolves to the app/ PACKAGE; see above

app = create_app(os.environ.get("FLASK_CONFIG", "development"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=app.config.get("DEBUG", True))
