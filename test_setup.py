import os
import requests
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()


def check(name, fn):
    try:
        print(f"[OK]   {name}: {fn()}")
    except Exception as e:
        print(f"[FAIL] {name}: {e}")


def rpc():
    w3 = Web3(Web3.HTTPProvider(os.environ["RPC_URL"]))
    return f"block {w3.eth.block_number}"


def morpho():
    q = {"query": "{ markets(first: 3) { items { lltv collateralAsset { symbol } loanAsset { symbol } } } }"}
    r = requests.post("https://api.morpho.org/graphql", json=q, timeout=20)
    r.raise_for_status()
    items = r.json()["data"]["markets"]["items"]
    return [f'{(m["collateralAsset"] or {}).get("symbol", "-")}/{m["loanAsset"]["symbol"]}' for m in items]


def pendle():
    r = requests.get("https://api-v2.pendle.finance/core/v2/markets/all", params={"limit": 3}, timeout=20)
    r.raise_for_status()
    return f"keys {list(r.json().keys())}"


def llama_protocols():
    r = requests.get("https://api.llama.fi/protocols", timeout=30)
    r.raise_for_status()
    return f"{len(r.json())} protocols"


def llama_hacks():
    r = requests.get("https://api.llama.fi/hacks", timeout=30)
    r.raise_for_status()
    return f"{len(r.json())} hacks"


if __name__ == "__main__":
    check("RPC Alchemy", rpc)
    check("Morpho API", morpho)
    check("Pendle API", pendle)
    check("DefiLlama protocols", llama_protocols)
    check("DefiLlama hacks", llama_hacks)
