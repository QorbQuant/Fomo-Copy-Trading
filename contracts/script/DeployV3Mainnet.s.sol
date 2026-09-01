// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {CopyVaultV3, IWETH} from "../src/CopyVaultV3.sol";
import {ICopyRouter} from "../src/CopyVault.sol";
import {IERC20} from "../src/MiniERC20.sol";

/// Robinhood Chain MAINNET deployment of CopyVault v3.
///
/// Reuses the EXISTING RouteAdapter (same mixed V3/V4 router v2 uses) — v3 only
/// replaces the vault, not the execution plumbing. The keeper (msg.sender) is
/// the executor. GUARDIAN and TREASURY come from env so role separation is real
/// from block one; if unset they fall back to the deployer with a loud warning
/// (a single-key deploy is NOT the intended public posture).
///
/// Deliberately dormant at deploy: fees (0), NAV bounds (0 = off), and gas-sweep
/// (0 = disabled) are all left at their safe defaults. Turn each on afterwards
/// once observed and audited:
///   setFees(depositFeeBps, perfFeeBps)         // e.g. (50, 1000)
///   setNavBounds(maxNavDeviationBps)           // e.g. 3000, after watching vol
///   setGasSweep(maxGasSweepBps, cooldown)      // e.g. (50, 1 hours)
/// Runtime keeper wiring is also post-deploy (matches v2): setSleeve,
/// setDestination(BASE, ...), setAllowedToken per held token. Then flip
/// deployments.json vault + "vault_version": 3 and migrate funds.
contract DeployV3Mainnet is Script {
    address constant USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    address constant WETH = 0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73;
    address constant ROUTE_ADAPTER = 0x13c2aeD11ec90f6B8b5Ca8D7Ae1050C2e55195fD;

    function run() external {
        address guardian = vm.envOr("GUARDIAN", msg.sender);
        address treasury = vm.envOr("TREASURY", msg.sender);
        if (guardian == msg.sender) {
            console.log("WARNING: GUARDIAN unset -> deployer is guardian. Set a SEPARATE pause key.");
        }
        if (treasury == msg.sender) {
            console.log("WARNING: TREASURY unset -> deployer is treasury.");
        }

        vm.startBroadcast();
        CopyVaultV3 vault = new CopyVaultV3(
            IERC20(USDG), IWETH(WETH), msg.sender, guardian, treasury, ICopyRouter(ROUTE_ADAPTER)
        );
        // 5% per-trade cap, 6h withdraw delay, 30min NAV freshness for deposits
        vault.setParams(500, 6 hours, 30 minutes);
        vm.stopBroadcast();

        console.log("vault v3 ", address(vault));
        console.log("asset    ", USDG);
        console.log("weth     ", WETH);
        console.log("router   ", ROUTE_ADAPTER);
        console.log("executor ", msg.sender);
        console.log("guardian ", guardian);
        console.log("treasury ", treasury);
    }
}
