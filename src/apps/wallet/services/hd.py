"""BIP44 EVM address derivation for Platform Wallet Infrastructure (RFC 004)."""

from __future__ import annotations

from eth_account import Account

from apps.wallet.exceptions import WalletSeedNotConfiguredError

# Polygon PoS uses the same EVM address bytes as ETH (coin type 60).
BIP44_EVM_ACCOUNT_PATH = "m/44'/60'/0'/0/{index}"

_Account = Account
_hd_enabled = False


def _ensure_hd_features() -> None:
    global _hd_enabled
    if not _hd_enabled:
        _Account.enable_unaudited_hdwallet_features()
        _hd_enabled = True


def normalize_evm_address(value: str) -> str:
    """Normalize to lowercase ``0x`` + 40 hex chars (matches Polygon provider)."""
    text = (value or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text:
        return "0x"
    return "0x" + text.zfill(40)[-40:]


def derive_evm_address(*, mnemonic: str, derivation_index: int) -> str:
    """Derive a receive address at ``m/44'/60'/0'/0/{derivation_index}``.

    Returns a lowercase checksum-stripped address suitable for Index Registry
    persistence. Does not touch private keys beyond ephemeral derivation.
    """
    seed = (mnemonic or "").strip()
    if not seed:
        raise WalletSeedNotConfiguredError("WALLET_HD_MNEMONIC is not configured")
    if derivation_index < 0:
        raise ValueError("derivation_index must be >= 0")

    _ensure_hd_features()
    path = BIP44_EVM_ACCOUNT_PATH.format(index=derivation_index)
    account = _Account.from_mnemonic(seed, account_path=path)
    return normalize_evm_address(account.address)
