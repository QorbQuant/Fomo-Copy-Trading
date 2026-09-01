// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {CopyVaultV3, IWETH} from "../src/CopyVaultV3.sol";
import {ICopyRouter} from "../src/CopyVault.sol";
import {IERC20} from "../src/MiniERC20.sol";

/// Robinhood Chain MAINNET deployment of CopyVault v3.
///
/// Reuses the EXISTING RouteAdapter (same mixed V3/V4 router v2 uses) — v3 only
/// replaces the vault, not the execution plumbing. EXECUTOR, GUARDIAN, TREASURY
/// and OWNER are ALL required from env and must be distinct from the deployer:
/// a single-key deploy would collapse v3's role separation, so it reverts here
/// rather than warning. Ownership is handed to the cold OWNER key (2-step:
/// OWNER must call acceptOwnership()).
///
/// Ships with NAV bounds ON (20%) so a bad NAV post can't move the mark freely.
/// Left dormant and turned on later once observed and audited:
///   setFees(depositFeeBps, mgmtFeeBps, perfFeeBps)   // e.g. (50, 200, 1000)
///   setNavBounds(maxNavDeviationBps)                 // tune from the 20% default
///   setGasSweep(maxGasSweepBps, cooldown)            // cooldown >= 15 min
///   setMinReturnBps(bps)                             // cross-chain return floor (default 90%)
/// Runtime keeper wiring is post-deploy (matches v2) and must be done by the cold
/// OWNER after acceptOwnership: setSleeve, setDestination(BASE, ...),
/// setAllowedToken per held token. Then flip deployments.json vault +
/// "vault_version": 3 and migrate funds (unwind v2 destinations first — see
/// the review's migration finding).
contract DeployV3Mainnet is Script {
    address constant USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    address constant WETH = 0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73;
    address constant ROUTE_ADAPTER = 0x13c2aeD11ec90f6B8b5Ca8D7Ae1050C2e55195fD;

    function run() external {
        // Distinct keys are MANDATORY — no silent fallback to the deployer. A
        // single-key deploy would collapse V3's whole role separation, which is
        // the point of V3. Missing env vars revert the deploy.
        address executor = vm.envAddress("EXECUTOR");   // hot keeper key
        address guardian = vm.envAddress("GUARDIAN");   // pause key
        address treasury = vm.envAddress("TREASURY");   // fee recipient
        address coldOwner = vm.envAddress("OWNER");     // cold key / multisig that owns config

        require(
            executor != msg.sender && guardian != msg.sender && treasury != msg.sender && coldOwner != msg.sender,
            "role == deployer"
        );
        require(executor != coldOwner && executor != guardian && executor != treasury, "executor not distinct");

        vm.startBroadcast();
        CopyVaultV3 vault = new CopyVaultV3(
            IERC20(USDG), IWETH(WETH), executor, guardian, treasury, ICopyRouter(ROUTE_ADAPTER)
        );
        // 5% per-trade cap, 6h withdraw delay, 30min NAV freshness for deposits
        vault.setParams(500, 6 hours, 30 minutes);
        // Ship NAV bounds ON: reject any single postNav that moves NAV > 20%.
        vault.setNavBounds(2000);
        // Temporary beta training-wheels (USDG, 6dp). Bound the downside while
        // un-audited; owner lifts each with setMax...(0) once audited and proven.
        vault.setMaxDepositPerAddress(500e6);    // $500 per address
        vault.setMaxTotalDeposits(25_000e6);     // $25k whole-vault ceiling (TUNE before deploy)
        // Anti-ratchet: min 2 min between keeper NAV posts (well under the 30-min TTL).
        vault.setNavPostCooldown(2 minutes);
        // Hand config ownership to the cold key. Two-step: coldOwner must call
        // acceptOwnership() before it controls the vault; until then the deployer
        // is still owner, so do this LAST in the broadcast.
        vault.transferOwnership(coldOwner);
        vm.stopBroadcast();

        console.log("vault v3 ", address(vault));
        console.log("asset    ", USDG);
        console.log("weth     ", WETH);
        console.log("router   ", ROUTE_ADAPTER);
        console.log("executor ", executor);
        console.log("guardian ", guardian);
        console.log("treasury ", treasury);
        console.log("owner -> ", coldOwner);
        console.log("ACTION: cold owner must call acceptOwnership() to finish the handoff.");
    }
}
