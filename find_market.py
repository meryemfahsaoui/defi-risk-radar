import json
import os
import re
from datetime import datetime, timezone

import requests

URL = "https://api.morpho.org/graphql"

VARIANTS = [
    ("marketId", "oracle { address }", True),
    ("uniqueKey", "oracleAddress", True),
    ("marketId", "oracle { address }", False),
    ("uniqueKey", "oracleAddress", False),
]


def build_query(id_field, oracle_field, with_where):
    where = ", where: {chainId_in: [1]}" if with_where else ""
    return (
        f"query($skip: Int!) {{ markets(first: 100, skip: $skip{where}) {{ items {{ "
        f"{id_field} lltv {oracle_field} "
        f"loanAsset {{ address symbol decimals }} "
        f"collateralAsset {{ address symbol decimals }} "
        f"state {{ supplyAssetsUsd borrowAssetsUsd }} }} }} }}"
    )


def fetch_all(query):
    out = []
    for skip in range(0, 1500, 100):
        r = requests.post(URL, json={"query": query, "variables": {"skip": skip}}, timeout=30)
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(body["errors"][0].get("message"))
        items = body["data"]["markets"]["items"]
        out.extend(items)
        if len(items) < 100:
            break
    return out


def to_ratio(x):
    v = float(x or 0)
    return v / 1e18 if v > 1 else v


def pt_expiry(symbol):
    m = re.search(r"(\d{1,2}[A-Z]{3}\d{4})", symbol.upper())
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d%b%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_markets():
    for id_field, oracle_field, with_where in VARIANTS:
        try:
            raw = fetch_all(build_query(id_field, oracle_field, with_where))
            print(f"schema: {id_field} / {oracle_field} / chain filter={with_where} -> {len(raw)} markets")
            return raw, id_field
        except Exception as e:
            print(f"variant {id_field}/{with_where} failed: {e}")
    raise SystemExit("no query variant worked")


def main():
    raw, id_field = load_markets()
    now = datetime.now(timezone.utc)
    rows = []
    for m in raw:
        coll = m.get("collateralAsset") or {}
        loan = m.get("loanAsset") or {}
        sym = coll.get("symbol") or ""
        if not sym.upper().startswith("PT-"):
            continue
        exp = pt_expiry(sym)
        if exp is None or exp <= now:
            continue
        state = m.get("state") or {}
        rows.append({
            "market_id": m.get(id_field),
            "collateral_symbol": sym,
            "collateral_address": coll.get("address"),
            "collateral_decimals": coll.get("decimals"),
            "loan_symbol": loan.get("symbol") or "?",
            "loan_address": loan.get("address"),
            "loan_decimals": loan.get("decimals"),
            "lltv": to_ratio(m.get("lltv")),
            "oracle": (m.get("oracle") or {}).get("address") or m.get("oracleAddress"),
            "supply_usd": state.get("supplyAssetsUsd") or 0,
            "borrow_usd": state.get("borrowAssetsUsd") or 0,
            "expiry": exp.strftime("%Y-%m-%d"),
            "days_to_expiry": (exp - now).days,
        })
    rows.sort(key=lambda r: r["borrow_usd"], reverse=True)
    os.makedirs("data", exist_ok=True)
    with open("data/pt_markets.json", "w") as f:
        json.dump(rows, f, indent=2)
    print(f"\n{len(rows)} active PT markets (sorted by borrow USD)\n")
    for i, r in enumerate(rows[:15], 1):
        print(
            f"{i:>2}. {r['collateral_symbol']:<30} -> {r['loan_symbol']:<6} "
            f"LLTV {r['lltv']:.1%}  borrow ${r['borrow_usd'] / 1e6:6.1f}M  "
            f"supply ${r['supply_usd'] / 1e6:6.1f}M  exp {r['expiry']} ({r['days_to_expiry']}d)"
        )


if __name__ == "__main__":
    main()
