import requests
from requests_oauthlib import OAuth1

class MaasAPIClient:
    def __init__(self, base_url, api_key, timeout=10):
        self.base_url = base_url.rstrip('/') + '/api/2.0'
        self.timeout = timeout
        
        try:
            consumer_key, token_key, token_secret = api_key.split(':')
        except ValueError:
            raise ValueError("Invalid MAAS_API_KEY format. Expected two ':' characters.")

        self.auth = OAuth1(
            client_key=consumer_key,
            client_secret='',
            resource_owner_key=token_key,
            resource_owner_secret=token_secret,
            signature_method='PLAINTEXT'
        )
        
        self.session = requests.Session()
        self.session.auth = self.auth

    def get_machines(self):
        url = f"{self.base_url}/machines/"
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def get_machine(self, system_id):
        url = f"{self.base_url}/machines/{system_id}/"
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def update_machine(self, system_id, params):
        url = f"{self.base_url}/machines/{system_id}/"
        response = self.session.put(url, data=params, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def perform_machine_action(self, system_id, action, params=None):
        url = f"{self.base_url}/machines/{system_id}/"
        payload = {"op": action}
        if params:
            payload.update(params)
            
        response = self.session.post(url, data=payload, timeout=self.timeout)
        response.raise_for_status()
        return response.json()