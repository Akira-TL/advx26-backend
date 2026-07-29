"""On-chain minting service using web3.py for Injective inEVM."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

try:
    from web3 import Web3
    from web3.exceptions import Web3Exception
except ImportError:
    Web3 = None
    Web3Exception = Exception

logger = logging.getLogger(__name__)

_ABI_PATH = Path(__file__).resolve().parent.parent / "injective_works" / "artifacts" / "contracts" / "SoundPolaNFT.sol" / "SoundPolaNFT.json"
_FALLBACK_ABI_PATH = Path(__file__).resolve().parent.parent / "injective_works" / "artifacts" / "contracts" / "MyFirstNFT.sol" / "MyFirstNFT.json"


class ChainService:
    def __init__(self, rpc_url: str, chain_id: int, contract_address: str, operator_key: str):
        if Web3 is None:
            raise ImportError("web3 package required for chain service: pip install web3")
        self._w3 = Web3(Web3.HTTPProvider(rpc_url))
        self._chain_id = chain_id
        self._operator_key = operator_key
        self._contract_address = Web3.to_checksum_address(contract_address) if contract_address else ""
        self._contract = None
        if self._contract_address:
            abi = self._load_abi()
            self._contract = self._w3.eth.contract(address=self._contract_address, abi=abi)

    def _load_abi(self) -> list:
        for path in (_ABI_PATH, _FALLBACK_ABI_PATH):
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data["abi"]
        raise FileNotFoundError(f"No contract ABI found at {_ABI_PATH} or {_FALLBACK_ABI_PATH}")

    @property
    def connected(self) -> bool:
        try:
            return self._w3.is_connected()
        except Exception:
            return False

    @property
    def contract_address(self) -> str:
        return self._contract_address

    def build_mint_tx(self, to_address: str, token_uri: str, sender_address: str) -> dict[str, Any]:
        """Build an unsigned safeMint transaction for client-side signing."""
        to = Web3.to_checksum_address(to_address)
        sender = Web3.to_checksum_address(sender_address)
        nonce = self._w3.eth.get_transaction_count(sender)
        gas = self._contract.functions.safeMint(to, token_uri).estimate_gas({"from": sender})
        gas_price = self._w3.eth.gas_price

        tx = self._contract.functions.safeMint(to, token_uri).build_transaction({
            "from": sender,
            "nonce": nonce,
            "gas": int(gas * 1.2),
            "gasPrice": gas_price,
            "chainId": self._chain_id,
            "value": 0,
        })
        return {
            "to": tx["to"],
            "data": tx["data"] if isinstance(tx["data"], str) else tx["data"].hex(),
            "nonce": tx["nonce"],
            "gas": tx["gas"],
            "gas_price": str(gas_price),
            "chain_id": self._chain_id,
            "value": 0,
        }

    def send_raw_tx(self, raw_tx_hex: str) -> str:
        """Broadcast a signed transaction. Returns tx hash hex with 0x prefix."""
        raw = bytes.fromhex(raw_tx_hex.removeprefix("0x"))
        tx_hash = self._w3.eth.send_raw_transaction(raw)
        h = tx_hash.hex()
        return h if h.startswith("0x") else f"0x{h}"

    def mint_server_side(self, to_address: str, token_uri: str, signing_key: str) -> tuple[int, str]:
        """Sign, send, and confirm a safeMint tx. Returns (token_id, tx_hash)."""
        if not signing_key:
            raise ValueError("No signing key available: user has no stored key and operator key is not configured")
        account = self._w3.eth.account.from_key(signing_key)
        to = Web3.to_checksum_address(to_address)
        nonce = self._w3.eth.get_transaction_count(account.address)
        balance_before = self._contract.functions.balanceOf(to).call()
        gas = self._contract.functions.safeMint(to, token_uri).estimate_gas({"from": account.address})
        gas_price = self._w3.eth.gas_price

        tx = self._contract.functions.safeMint(to, token_uri).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gas": int(gas * 1.2),
            "gasPrice": gas_price,
            "chainId": self._chain_id,
            "value": 0,
        })
        signed = account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex() if tx_hash.hex().startswith("0x") else f"0x{tx_hash.hex()}"

        receipt = self._wait_confirmed(account.address, nonce, tx_hash_hex)
        if receipt is not None:
            token_id = self._extract_token_id(receipt)
        else:
            balance_after = self._contract.functions.balanceOf(to).call()
            if balance_after <= balance_before:
                raise RuntimeError(f"Transaction {tx_hash_hex} did not mint (balance unchanged)")
            token_id = balance_after - 1
        return token_id, tx_hash_hex

    def _wait_confirmed(self, sender: str, expected_nonce: int, tx_hash_hex: str, timeout: int = 90) -> dict | None:
        """Wait for tx confirmation. Returns receipt or None if receipt unavailable but nonce advanced."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                receipt = self._w3.eth.get_transaction_receipt(tx_hash_hex)
                if receipt is not None:
                    if receipt["status"] != 1:
                        raise RuntimeError(f"Transaction {tx_hash_hex} reverted")
                    return receipt
            except RuntimeError:
                raise
            except Exception:
                pass
            current_nonce = self._w3.eth.get_transaction_count(sender)
            if current_nonce > expected_nonce:
                try:
                    receipt = self._w3.eth.get_transaction_receipt(tx_hash_hex)
                    if receipt and receipt["status"] == 1:
                        return receipt
                except Exception:
                    pass
                return None
            time.sleep(2)
        current_nonce = self._w3.eth.get_transaction_count(sender)
        if current_nonce > expected_nonce:
            return None
        raise TimeoutError(f"Transaction {tx_hash_hex} not confirmed within {timeout}s")

    def wait_for_receipt(self, tx_hash: str, timeout: int = 90) -> dict:
        """Poll for transaction receipt."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                receipt = self._w3.eth.get_transaction_receipt(tx_hash)
            except Exception:
                receipt = None
            if receipt is not None:
                if receipt["status"] != 1:
                    raise RuntimeError(f"Transaction {tx_hash} reverted")
                return receipt
            time.sleep(2)
        raise TimeoutError(f"Transaction {tx_hash} not confirmed within {timeout}s")

    def _extract_token_id(self, receipt: dict) -> int:
        """Extract tokenId from Transfer event (from=0x0 means mint)."""
        transfer_topic = self._w3.keccak(text="Transfer(address,address,uint256)").hex()
        for log_entry in receipt["logs"]:
            topics = log_entry.get("topics", [])
            if len(topics) >= 4 and topics[0].hex() == transfer_topic:
                from_addr = "0x" + topics[1].hex()[-40:]
                if from_addr == "0x" + "0" * 40:
                    return int(topics[3].hex(), 16)
        raise ValueError("No mint Transfer event found in receipt")

    def get_token_id_from_receipt(self, tx_hash: str) -> int:
        """Fetch receipt and extract token ID."""
        receipt = self.wait_for_receipt(tx_hash)
        return self._extract_token_id(receipt)
