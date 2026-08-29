// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// Minimal deBridge DLN source interface (createOrder + fixed fee), matching
/// deployed DlnSource contracts. Mocked in tests; wired to the real deployment
/// on chains where DLN is live.
struct OrderCreation {
    address giveTokenAddress;
    uint256 giveAmount;
    bytes takeTokenAddress;
    uint256 takeAmount;
    uint256 takeChainId;
    bytes receiverDst;
    address givePatchAuthoritySrc;
    bytes orderAuthorityAddressDst;
    bytes allowedTakerDst;
    bytes externalCall;
    bytes allowedCancelBeneficiarySrc;
}

interface IDlnSource {
    function globalFixedNativeFee() external view returns (uint88);

    function createOrder(
        OrderCreation calldata orderCreation,
        bytes calldata affiliateFee,
        uint32 referralCode,
        bytes calldata permitEnvelope
    ) external payable returns (bytes32 orderId);
}
