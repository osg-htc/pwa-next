# mod_wsgi entry point.  Settings come from the file named by PWA_CONFIG.
import os

os.environ.setdefault("PWA_CONFIG", "/etc/pwa/pwa.env")   # daemon runs as user "pwa"

from pwa.web import create_app

application = create_app()
