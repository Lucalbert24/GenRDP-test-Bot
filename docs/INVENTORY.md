# Inventory format

The bot loads inventory from the file configured by:

```env
INVENTORY_FILE=data/inventory.json
```

JSON and CSV are supported.

## Required fields

| Field | Description |
|---|---|
| `sku` | Unique inventory identifier |
| `label` | Human-readable inventory name |
| `ip_version` | `ipv4` or `ipv6` |
| `carrier` | Carrier/operator shown to customers |
| `conn_id` | iProxy connection ID |
| `price_24h` | 24h test price |
| `price_7d` | 7-day test price |
| `status` | `available`, `reserved`, `sold`, or `disabled` |
| `notes` | Optional internal note |

## Important

The included `inventory.sample.json` and `inventory.sample.csv` are dummy data.

Before production use, replace all fake `conn_id` values with real iProxy connection IDs.

If `FORCE_TEST_PRICES=1`, the bot ignores prices in inventory and uses the prices from environment variables:

```env
TEST_PRICE_24H_IPV4=10
TEST_PRICE_7D_IPV4=30
TEST_PRICE_24H_IPV6=10
TEST_PRICE_7D_IPV6=35
```
