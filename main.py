import os

from app.api import create_app
from app.service import Gateway

app = create_app(Gateway(os.environ.get("IOT_GATEWAY_STORE")))
