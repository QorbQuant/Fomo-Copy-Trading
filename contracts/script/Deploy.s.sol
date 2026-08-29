// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {CopyVault, ICopyRouter} from "../src/CopyVault.sol";
import {IDlnSource} from "../src/IDlnSource.sol";
import {IERC20} from "../src/MiniERC20.sol";
import {MockDlnSource, MockERC20, MockRouter} from "../src/mocks/Mocks.sol";

/// Testnet/local deployment: mock USDC + two stand-in meme tokens + a
/// fixed-rate mock DEX + the vault. The deployer key doubles as the executor.
contract Deploy is Script {
    function run() external {
        vm.startBroadcast();
        address deployer = msg.sender;

        MockERC20 usdc = new MockERC20("Mock USD Coin", "mUSDC", 6);
        MockERC20 chill = new MockERC20("Mock Chill", "mCHILL", 18);
        MockERC20 boomer = new MockERC20("Mock Boomer", "mBOOMER", 18);
        MockRouter router = new MockRouter();
        CopyVault vault = new CopyVault(IERC20(address(usdc)), deployer, ICopyRouter(address(router)));

        vault.setAllowedToken(address(chill), true);
        vault.setAllowedToken(address(boomer), true);
        // prototype-friendly params: 5% cap, 10 min withdraw delay, 1h NAV TTL
        vault.setParams(500, 10 minutes, 1 hours);

        // seed the mock DEX: deep reserves, $1-per-token rates both ways
        chill.mint(address(router), 100_000_000e18);
        boomer.mint(address(router), 100_000_000e18);
        usdc.mint(address(router), 100_000_000e6);
        router.setRate(address(usdc), address(chill), 1e30);
        router.setRate(address(usdc), address(boomer), 1e30);
        router.setRate(address(chill), address(usdc), 1e6);
        router.setRate(address(boomer), address(usdc), 1e6);

        // pocket money for the deployer to demo deposits
        usdc.mint(deployer, 1_000_000e6);

        // Solana sleeve wiring: mock DLN on testnet, real DlnSource on chains
        // where deBridge is live. Receiver/take-token from env (32-byte hex).
        MockDlnSource dln = new MockDlnSource();
        vault.setSleeve(
            IDlnSource(address(dln)),
            abi.encodePacked(vm.envBytes32("SLEEVE_RECEIVER32")),
            abi.encodePacked(vm.envBytes32("SLEEVE_TAKE_TOKEN32")),
            3000
        );

        vm.stopBroadcast();

        console.log("mUSDC   ", address(usdc));
        console.log("mCHILL  ", address(chill));
        console.log("mBOOMER ", address(boomer));
        console.log("router  ", address(router));
        console.log("dln     ", address(dln));
        console.log("vault   ", address(vault));
    }
}
