from .baseapi import BaseAPI

class ProxyClient:
    def __init__(self, token, base_url, **kwargs):
        self._api = BaseAPI(token=f"Basic {token}", base_url=base_url)

    def sync_price(self, params={}):
        data = self.api.post("proxyinventories/sync-price", params=params)
        return data

    def adjustorset(self, params={}):
        data = self.api.post("proxyinventories/adjustorset", params=params)
        return data

    def inventories(self, params={}):
        data = self.api.post("proxyinventories/inventories", params=params)
        return data

    def products(self, params={}):
        data = self.api.get("proxyinventories/products", params=params)
        return data

    @property
    def api(self):
        return self._api
