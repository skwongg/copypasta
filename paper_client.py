"""Explicit local market fixtures. No credentials or network are available."""
import json
from pathlib import Path


class PaperClient:
    mode = 'dry_run'
    account_number = None

    def __init__(self, fixture):
        if not isinstance(fixture, dict) or set(fixture) != {'contracts', 'quotes'}:
            raise ValueError('market fixture requires contracts and quotes')
        if not isinstance(fixture['contracts'], list) or not isinstance(fixture['quotes'], dict):
            raise ValueError('invalid market fixture')
        self.fixture = fixture

    @classmethod
    def from_file(cls, path):
        return cls(json.loads(Path(path).read_text()))

    def find_option_contracts(self, underlying, expiry, strike, option_type):
        return [c for c in self.fixture['contracts'] if isinstance(c, dict)
                and c.get('underlying') == underlying and c.get('expiry') == expiry
                and c.get('strike') == strike and c.get('option_type') == option_type]

    def get_option_quote(self, contract_symbol):
        return self.fixture['quotes'].get(contract_symbol)

    def review_option_order(self, contract_symbol, side, qty, order_type, limit_price=None):
        return {'approved': True}  # local simulation only

    def get_orders(self, status=None):
        return []

    def get_positions(self):
        return []

    def place_option_order(self, *args, **kwargs):
        raise RuntimeError('paper mode must never dispatch broker orders')
