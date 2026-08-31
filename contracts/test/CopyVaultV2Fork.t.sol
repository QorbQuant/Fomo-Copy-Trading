// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {CopyVaultV2} from "../src/CopyVaultV2.sol";
import {ICopyRouter} from "../src/CopyVault.sol";
import {IDlnSource} from "../src/IDlnSource.sol";
import {IERC20} from "../src/MiniERC20.sol";

/// Fork test on Robinhood Chain mainnet: prove v2 creates a REAL deBridge DLN
/// order to an EVM destination (Base, 20-byte receiver). Production has only
/// ever bridged to Solana's 32-byte receiver, so this validates the EVM path
/// against the live DlnSource before the migration.
contract CopyVaultV2ForkTest is Test {
    address constant USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    address constant DLN = 0xeF4fB24aD0916217251F553c0596F8Edc630EB66;
    uint256 constant BASE = 8453;
    address constant KEEPER = 0x27813048104759935DD6D505e8cddda1a5f4EFA1;
    address constant BASE_USDC = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;

    function test_realDlnOrderToEvmDestination() public {
        vm.createSelectFork("https://rpc.mainnet.chain.robinhood.com");
        CopyVaultV2 vault =
            new CopyVaultV2(IERC20(USDG), address(this), ICopyRouter(address(0xdead)));
        vault.setDlnSource(IDlnSource(DLN));
        vault.setDestination(BASE, abi.encodePacked(KEEPER), abi.encodePacked(BASE_USDC), 5000);

        // seed the vault with USDG and post a NAV
        deal(USDG, address(vault), 100e6);
        vault.postNav(100e6);

        uint88 fee = IDlnSource(DLN).globalFixedNativeFee();
        vm.deal(address(this), fee);

        // create a real order: give 20 USDG on Robinhood, receive USDC on Base
        bytes32 orderId = vault.fundDestination{value: fee}(BASE, 20e6, 19e6);
        assertTrue(orderId != bytes32(0), "real DLN accepted EVM-destination order");

        (, , , uint256 funded,) = vault.destination(BASE);
        assertEq(funded, 20e6);
        assertEq(IERC20(USDG).balanceOf(address(vault)), 80e6); // 20 left the vault
    }
}
