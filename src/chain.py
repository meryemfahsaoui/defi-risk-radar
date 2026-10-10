import os

from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

w3 = Web3(Web3.HTTPProvider(os.environ["RPC_URL"], request_kwargs={"timeout": 30}))

MORPHO_BLUE = Web3.to_checksum_address("0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb")
ZERO = "0x0000000000000000000000000000000000000000"


def cs(address):
    return Web3.to_checksum_address(address)


def _abi(name, inputs, outputs):
    return [{
        "type": "function",
        "name": name,
        "stateMutability": "view",
        "inputs": [{"name": f"i{k}", "type": t} for k, t in enumerate(inputs)],
        "outputs": [{"name": f"o{k}", "type": t} for k, t in enumerate(outputs)],
    }]


def call(address, name, outputs=("address",), inputs=(), args=()):
    c = w3.eth.contract(address=cs(address), abi=_abi(name, inputs, outputs))
    return getattr(c.functions, name)(*args).call()


def try_call(address, name, outputs=("address",), inputs=(), args=()):
    try:
        return call(address, name, outputs, inputs, args)
    except Exception:
        return None


def is_contract(address):
    if not address or address.lower() == ZERO:
        return False
    try:
        return len(w3.eth.get_code(cs(address))) > 0
    except Exception:
        return False


def try_address(address, name):
    out = try_call(address, name, ("address",))
    return out if is_contract(out) else None


def symbol(address):
    s = try_call(address, "symbol", ("string",))
    if s:
        return s
    b = try_call(address, "symbol", ("bytes32",))
    if b:
        return b.rstrip(b"\x00").decode(errors="ignore")
    return address[:8]


def decimals(address):
    d = try_call(address, "decimals", ("uint8",))
    return d if d is not None else 18
