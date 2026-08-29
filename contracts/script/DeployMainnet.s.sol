// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {CopyVault, ICopyRouter} from "../src/CopyVault.sol";
import {IERC20} from "../src/MiniERC20.sol";
import {ISwapRouter02, UniswapV3Adapter} from "../src/UniswapV3Adapter.sol";

/// Robinhood Chain MAINNET deployment: real USDG as the vault asset, real
/// Uniswap V3 execution through the adapter. No mocks. The Solana sleeve is
/// left unconfigured in v1 (set it later with setSleeve once a real DLN
/// deployment on this chain is verified).
///
/// Token allowlisting + adapter paths are runtime keeper actions, not
/// deploy-time: setAllowedToken on the vault, setPath on the adapter.
contract DeployMainnet is Script {
    address constant SWAP_ROUTER = 0xCaf681a66D020601342297493863E78C959E5cb2;
    address constant USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;

    function run() external {
        vm.startBroadcast();
        UniswapV3Adapter adapter = new UniswapV3Adapter(ISwapRouter02(SWAP_ROUTER));
        CopyVault vault = new CopyVault(IERC20(USDG), msg.sender, ICopyRouter(address(adapter)));
        // 5% per-trade cap, 6h withdraw delay, 30min NAV freshness for deposits
        vault.setParams(500, 6 hours, 30 minutes);
        vm.stopBroadcast();

        console.log("adapter ", address(adapter));
        console.log("vault   ", address(vault));
        console.log("asset   ", USDG);
    }
}
