// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {CopyVault, ICopyRouter} from "../src/CopyVault.sol";
import {IERC20} from "../src/MiniERC20.sol";
import {MockERC20, MockRouter} from "../src/mocks/Mocks.sol";

/// Self-contained end-to-end demo: deploy, deposit, mirror two trades,
/// redeem in kind. Run against anvil or the Robinhood Chain testnet.
contract Demo is Script {
    function run() external {
        vm.startBroadcast();
        address me = msg.sender;

        MockERC20 usdc = new MockERC20("Mock USD Coin", "mUSDC", 6);
        MockERC20 chill = new MockERC20("Mock Chill", "mCHILL", 18);
        MockERC20 boomer = new MockERC20("Mock Boomer", "mBOOMER", 18);
        MockRouter router = new MockRouter();
        CopyVault vault = new CopyVault(IERC20(address(usdc)), me, ICopyRouter(address(router)));
        vault.setAllowedToken(address(chill), true);
        vault.setAllowedToken(address(boomer), true);
        vault.setParams(500, 0, 1 hours); // demo: no withdraw delay

        chill.mint(address(router), 1_000_000e18);
        boomer.mint(address(router), 1_000_000e18);
        usdc.mint(address(router), 1_000_000e6);
        router.setRate(address(usdc), address(chill), 1e30);
        router.setRate(address(usdc), address(boomer), 1e30);
        router.setRate(address(chill), address(usdc), 1e6);
        router.setRate(address(boomer), address(usdc), 1e6);

        // 1. deposit $10,000 -> mint shares (the vault token)
        usdc.mint(me, 10_000e6);
        usdc.approve(address(vault), type(uint256).max);
        uint256 shares = vault.deposit(10_000e6);
        console.log("deposited $10,000 -> shares (18d):", shares);

        // 2. keeper posts NAV, mirrors two trader buys
        vault.postNav(10_000e6);
        vault.mirrorTrade(address(usdc), address(chill), 400e6, 390e18);
        vault.mirrorTrade(address(usdc), address(boomer), 300e6, 290e18);
        console.log("vault mUSDC  :", usdc.balanceOf(address(vault)));
        console.log("vault mCHILL :", chill.balanceOf(address(vault)));
        console.log("vault mBOOMER:", boomer.balanceOf(address(vault)));

        // 3. redeem half the shares IN KIND
        uint256 chillBefore = chill.balanceOf(me);
        vault.redeemInKind(shares / 2, me);
        console.log("--- redeemed 50% of shares in kind ---");
        console.log("received mUSDC  :", usdc.balanceOf(me));
        console.log("received mCHILL :", chill.balanceOf(me) - chillBefore);
        console.log("received mBOOMER:", boomer.balanceOf(me));
        console.log("shares left     :", vault.balanceOf(me));
        console.log("vault:", address(vault));

        vm.stopBroadcast();
    }
}
