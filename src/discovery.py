import json
import re
import sys
from datetime import datetime, timezone

import networkx as nx
import requests
from web3 import Web3

from src.chain import MORPHO_BLUE, call, decimals, is_contract, symbol, try_address, try_call

MORPHO_API = "https://api.morpho.org/graphql"
PENDLE_API = "https://api-v2.pendle.finance/core/v2/markets/all"
FEED_GETTERS = ["BASE_FEED_1", "BASE_FEED_2", "QUOTE_FEED_1", "QUOTE_FEED_2"]
VAULT_GETTERS = ["BASE_VAULT", "QUOTE_VAULT"]
ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")


def load_market(index):
    with open("data/pt_markets.json") as f:
        return json.load(f)[index - 1]


def top_borrowers(market_id, n=20):
    variants = [
        ("marketUniqueKey_in", "orderBy: BorrowShares, orderDirection: Desc, "),
        ("marketId_in", "orderBy: BorrowShares, orderDirection: Desc, "),
        ("marketUniqueKey_in", ""),
        ("marketId_in", ""),
    ]
    for field, order in variants:
        q = (
            f'{{ marketPositions(first: {n}, {order}where: {{{field}: ["{market_id}"]}}) '
            f"{{ items {{ user {{ address }} }} }} }}"
        )
        try:
            body = requests.post(MORPHO_API, json={"query": q}, timeout=30).json()
            if body.get("errors"):
                continue
            items = body["data"]["marketPositions"]["items"]
            if items:
                return [i["user"]["address"] for i in items]
        except Exception:
            continue
    return []


def read_position(mid_bytes, user, totals):
    _, borrow_shares, collateral = call(
        MORPHO_BLUE, "position", ("uint256", "uint128", "uint128"),
        ("bytes32", "address"), (mid_bytes, Web3.to_checksum_address(user)),
    )
    total_borrow_assets, total_borrow_shares = totals[2], totals[3]
    borrow_assets = borrow_shares * total_borrow_assets // total_borrow_shares if total_borrow_shares else 0
    return borrow_assets, collateral


def classify_feed(addr):
    info = {"address": addr.lower(), "label": f"Price feed {addr[:8]}", "feed_type": "unknown", "reacts_to_market": None}
    disc = try_call(addr, "baseDiscountPerYear", ("uint256",))
    mkt = try_address(addr, "market")
    twap = try_call(addr, "twapDuration", ("uint32",))
    desc = try_call(addr, "description", ("string",))
    if disc is not None:
        info.update(label=f"Pendle linear discount ({disc / 1e18:.1%}/yr)", feed_type="pendle_linear_discount", reacts_to_market=False)
    elif mkt:
        info.update(label=f"Pendle AMM TWAP ({twap}s)" if twap else "Pendle AMM TWAP", feed_type="pendle_twap", reacts_to_market=True, market=mkt.lower())
    elif desc:
        info.update(label=f"Feed: {desc}", feed_type="chainlink_like", reacts_to_market=True)
    return info


def classify_vault(addr):
    return {"address": addr.lower(), "label": f"Vault rate {symbol(addr)}", "feed_type": "vault_rate", "reacts_to_market": False}


def find_pendle_market(pt_address):
    pt = pt_address.lower()
    skip, limit, total = 0, 100, 1
    while skip < total and skip < 3000:
        r = requests.get(PENDLE_API, params={"skip": skip, "limit": limit}, timeout=30)
        r.raise_for_status()
        body = r.json()
        total = body.get("total", 0)
        for m in body.get("results", []):
            if pt in json.dumps(m).lower():
                found = ADDR_RE.findall(str(m.get("address", "")))
                return (found[0] if found else None), m
        skip += limit
    return None, None


def underlying_chain(start, max_depth=4):
    out, seen, tok = [], set(), start
    while tok and tok.lower() not in seen and len(out) < max_depth:
        seen.add(tok.lower())
        nxt = try_address(tok, "asset")
        out.append({"address": tok.lower(), "symbol": symbol(tok), "is_vault": nxt is not None})
        tok = nxt
    return out


def discover(index):
    mk = load_market(index)
    mid = mk["market_id"]
    mid_bytes = Web3.to_bytes(hexstr=mid)
    loan, coll, oracle, irm, lltv_raw = call(
        MORPHO_BLUE, "idToMarketParams",
        ("address", "address", "address", "address", "uint256"), ("bytes32",), (mid_bytes,),
    )
    totals = call(MORPHO_BLUE, "market", ("uint128",) * 6, ("bytes32",), (mid_bytes,))
    lltv = lltv_raw / 1e18
    loan_sym, coll_sym = symbol(loan), symbol(coll)
    loan_dec, coll_dec = decimals(loan), decimals(coll)
    oracle_price = call(oracle, "price", ("uint256",)) / 10 ** (36 + loan_dec - coll_dec)

    borrower, best = None, (0, 0)
    for user in top_borrowers(mid):
        try:
            b, c = read_position(mid_bytes, user, totals)
        except Exception:
            continue
        if c > 0 and b > best[0]:
            borrower, best = user, (b, c)

    if borrower:
        debt = best[0] / 10 ** loan_dec
        collateral = best[1] / 10 ** coll_dec
    else:
        collateral = 1_000_000 / oracle_price
        debt = collateral * oracle_price * (lltv - 0.05)
    coll_value = collateral * oracle_price
    expiry_ts = try_call(coll, "expiry", ("uint256",))
    now = datetime.now(timezone.utc)
    expiry = datetime.fromtimestamp(expiry_ts, timezone.utc) if expiry_ts else None

    position = {
        "market_index": index,
        "market_id": mid,
        "borrower": borrower,
        "hypothetical": borrower is None,
        "collateral_token": coll.lower(),
        "collateral_symbol": coll_sym,
        "loan_token": loan.lower(),
        "loan_symbol": loan_sym,
        "oracle": oracle.lower(),
        "irm": irm.lower(),
        "lltv": lltv,
        "collateral_units": collateral,
        "debt_units": debt,
        "oracle_price": oracle_price,
        "collateral_value": coll_value,
        "equity": coll_value - debt,
        "ltv": debt / coll_value if coll_value else 0,
        "health_factor": lltv * coll_value / debt if debt else None,
        "price_drop_to_liquidation": 1 - debt / (lltv * coll_value) if coll_value else None,
        "expiry": expiry.strftime("%Y-%m-%d") if expiry else None,
        "days_to_expiry": (expiry - now).days if expiry else None,
        "market_total_supply": totals[0] / 10 ** loan_dec,
        "market_total_borrow": totals[2] / 10 ** loan_dec,
    }

    G = nx.DiGraph()

    def node(key, label, kind, **attrs):
        if key not in G:
            G.add_node(key, label=label, kind=kind, **attrs)
        return key

    pos = node("position", f"Position {coll_sym} -> {loan_sym}", "position")
    G.add_edge(pos, node("ethereum", "Ethereum", "chain"), rel="settles_on")
    mkt = node(f"morpho:{mid.lower()}", f"Morpho market {coll_sym}/{loan_sym}", "lending_market", protocol="morpho", lltv=lltv)
    G.add_edge(pos, mkt, rel="held_in")
    G.add_edge(mkt, node(MORPHO_BLUE.lower(), "Morpho Blue core", "lending_protocol", protocol="morpho"), rel="runs_on")
    G.add_edge(mkt, node(irm.lower(), "Interest rate model", "irm", protocol="morpho"), rel="rate_from")
    G.add_edge(mkt, node(loan.lower(), loan_sym, "asset"), rel="debt_in")
    pt_n = node(coll.lower(), coll_sym, "pt", protocol="pendle", expiry=position["expiry"])
    G.add_edge(mkt, pt_n, rel="collateral")

    orc = node(oracle.lower(), "Morpho oracle", "oracle", protocol="morpho")
    G.add_edge(mkt, orc, rel="priced_by")
    feeds, twap_markets = [], []
    for g in FEED_GETTERS:
        a = try_address(oracle, g)
        if a:
            feeds.append(dict(classify_feed(a), slot=g))
    for g in VAULT_GETTERS:
        a = try_address(oracle, g)
        if a:
            feeds.append(dict(classify_vault(a), slot=g))
    if not feeds:
        info = classify_feed(oracle)
        G.nodes[orc].update(label=f"Oracle: {info['label']}", feed_type=info["feed_type"], reacts_to_market=info["reacts_to_market"])
        if info.get("market"):
            twap_markets.append((orc, info["market"]))
    for f in feeds:
        fn = node(f["address"], f["label"], "price_feed", feed_type=f["feed_type"], reacts_to_market=f["reacts_to_market"], slot=f["slot"])
        G.add_edge(orc, fn, rel="reads")
        if f.get("market"):
            twap_markets.append((fn, f["market"]))

    sy = try_address(coll, "SY")
    if sy:
        sy_n = node(sy.lower(), symbol(sy), "sy", protocol="pendle")
        G.add_edge(pt_n, sy_n, rel="redeems_into")
        start = try_address(sy, "yieldToken")
        chain = underlying_chain(start) if start else []
        info = try_call(sy, "assetInfo", ("uint8", "address", "uint8"))
        if info and is_contract(info[1]) and info[1].lower() not in {c["address"] for c in chain}:
            chain.append({"address": info[1].lower(), "symbol": symbol(info[1]), "is_vault": False})
        prev = sy_n
        for c in chain:
            cn = node(c["address"], c["symbol"], "yield_source" if c["is_vault"] else "asset")
            G.add_edge(prev, cn, rel="underlying")
            prev = cn

    amm_addr, amm_raw = None, None
    try:
        amm_addr, amm_raw = find_pendle_market(coll)
    except Exception as e:
        print(f"Pendle API lookup failed: {e}")
    if not amm_addr and twap_markets:
        amm_addr = twap_markets[0][1]
    if amm_addr:
        tokens = try_call(amm_addr, "readTokens", ("address", "address", "address"))
        confirmed = bool(tokens) and tokens[1].lower() == coll.lower()
        amm = node(amm_addr.lower(), f"Pendle AMM {coll_sym}", "amm", protocol="pendle", confirmed_onchain=confirmed)
        G.add_edge(pos, amm, rel="exit_via")
        for src, m_addr in twap_markets:
            if m_addr == amm_addr.lower():
                G.add_edge(src, amm, rel="twap_from")

    return G, position, amm_raw


def main():
    index = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    G, p, amm_raw = discover(index)
    with open("data/graph.json", "w") as f:
        json.dump(nx.node_link_data(G), f, indent=2, default=str)
    with open("data/position.json", "w") as f:
        json.dump(p, f, indent=2, default=str)
    if amm_raw:
        with open("data/pendle_market.json", "w") as f:
            json.dump(amm_raw, f, indent=2, default=str)

    who = "hypothetical $1M position" if p["hypothetical"] else p["borrower"]
    print(f"\nMarket   {p['collateral_symbol']} -> {p['loan_symbol']}   LLTV {p['lltv']:.1%}   expiry {p['expiry']} ({p['days_to_expiry']}d)")
    print(f"Position {who}")
    print(f"         collateral {p['collateral_units']:,.0f} PT   debt {p['debt_units']:,.0f} {p['loan_symbol']}")
    print(f"         oracle price {p['oracle_price']:.4f}   LTV {p['ltv']:.1%}   HF {p['health_factor']:.3f}   "
          f"liquidated if price drops {p['price_drop_to_liquidation']:.2%}")
    print(f"\nDependency graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges\n")
    for _, d in G.nodes(data=True):
        print(f"  [{d['kind']:<16}] {d['label']}")
    print()
    for a, b, d in G.edges(data=True):
        print(f"  {G.nodes[a]['label']}  --{d['rel']}-->  {G.nodes[b]['label']}")


if __name__ == "__main__":
    main()
