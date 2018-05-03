import requests

headers = {"Authorization": "Bearer xxxxxxxxxxxxxxxx"}		# FIXME
requests.post("https://lichess.org/api/bot/account/upgrade", headers = headers)
