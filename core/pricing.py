import json

from .config import MODEL_PRICES


class PricingCatalog:
    def __init__(self, path):
        self.path = path
        self.prices = self.load()

    def load(self):
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        name: {"input": prices[0], "output": prices[1]}
                        for name, prices in MODEL_PRICES.items()
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        try:
            saved_prices = json.loads(self.path.read_text(encoding="utf-8"))
            merged = {name.lower(): tuple(prices) for name, prices in MODEL_PRICES.items()}
            merged.update(
                {
                    name.lower(): (float(rate["input"]), float(rate["output"]))
                    for name, rate in saved_prices.items()
                    if float(rate["input"]) >= 0 and float(rate["output"]) >= 0
                }
            )
            return merged
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return {name.lower(): tuple(prices) for name, prices in MODEL_PRICES.items()}

    def for_model(self, model):
        return self.lookup(self.prices, model)

    @staticmethod
    def lookup(prices, model):
        normalized = (model or "").lower()
        for name, rate in prices.items():
            if normalized == name or normalized.startswith(name + "-"):
                return rate
        return None
