// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {CopyVaultV3, IWETH} from "../src/CopyVaultV3.sol";
import {ICopyRouter} from "../src/CopyVault.sol";
import {IDlnSource} from "../src/IDlnSource.sol";
import {IERC20} from "../src/MiniERC20.sol";
import {MockDlnSource, MockERC20, MockRouter, MockWETH} from "../src/mocks/Mocks.sol";

/// Direct-transfer ("donation") safety: what happens when someone sends tokens
/// straight to the vault (outside deposit/mirrorTrade) and a holder then redeems.
/// Redemption pays from real on-chain balances, so these pin the guarantee that a
/// donation can never be leveraged to steal from, or dilute, existing holders.
contract DonationTest is Test {
    CopyVaultV3 vault;
    MockERC20 usdc;
    MockERC20 meme;
    MockRouter router;
    MockWETH weth;

    address owner = address(this);
    address keeper = makeAddr("keeper");
    address guardian = makeAddr("guardian");
    address treasury = makeAddr("treasury");
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");
    address donor = makeAddr("donor");

    uint256 constant BASE = 8453;
    bytes EVM_RECV = hex"27813048104759935DD6D505e8cddda1a5f4EFA1";
    bytes EVM_USDC = hex"833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";

    function setUp() public {
        usdc = new MockERC20("USD Coin", "USDG", 6);
        meme = new MockERC20("Chill", "CHILL", 18);
        router = new MockRouter();
        weth = new MockWETH();
        vault = new CopyVaultV3(
            IERC20(address(usdc)), IWETH(address(weth)), keeper, guardian, treasury, ICopyRouter(address(router))
        );
        usdc.mint(alice, 1_000_000e6);
        usdc.mint(bob, 1_000_000e6);
        usdc.mint(donor, 1_000_000e6);
        vm.prank(alice);
        usdc.approve(address(vault), type(uint256).max);
        vm.prank(bob);
        usdc.approve(address(vault), type(uint256).max);
    }

    // Directly-sent USDG is socialized pro-rata; the sender gains nothing and
    // cannot pull it back (it's the asset, so rescue() is blocked).
    function test_donatedUsdgIsSocialized_noTheft() public {
        vm.prank(alice);
        vault.deposit(1_000e6, 0);
        vm.prank(bob);
        vault.deposit(1_000e6, 0);
        vm.prank(keeper);
        vault.postNav(2_000e6, 0); // supply 2,000e18, real balance 2,000e6

        // an outsider just transfers 1,000 USDG straight to the vault
        vm.prank(donor);
        usdc.transfer(address(vault), 1_000e6);
        assertEq(usdc.balanceOf(address(vault)), 3_000e6);
        assertEq(vault.balanceOf(donor), 0); // donor got no shares

        vm.warp(block.timestamp + vault.withdrawDelay() + 1);
        uint256 aliceBefore = usdc.balanceOf(alice);
        vm.prank(alice);
        vault.redeemInKind(1_000e18, alice);

        // alice (half the supply) receives half of the REAL balance = 1,500:
        // her 1,000 deposit + half the donation. bob keeps the other half.
        assertEq(usdc.balanceOf(alice) - aliceBefore, 1_500e6);
        assertEq(usdc.balanceOf(address(vault)), 1_500e6); // bob's 1,000 shares back 1,500
        vm.expectRevert(bytes("managed")); // donor cannot claw the asset back
        vault.rescue(address(usdc), donor);
    }

    // A random token nobody registered is NOT paid to redeemers at all, and can
    // only be pulled out by the owner via rescue().
    function test_randomTokenNotPaidToRedeemers() public {
        vm.prank(alice);
        vault.deposit(1_000e6, 0);
        vm.prank(keeper);
        vault.postNav(1_000e6, 0);

        MockERC20 rando = new MockERC20("Rando", "RND", 18);
        rando.mint(address(vault), 777e18); // shows up, but never traded in => not held

        vm.warp(block.timestamp + vault.withdrawDelay() + 1);
        vm.prank(alice);
        vault.redeemInKind(1_000e18, alice);
        assertEq(rando.balanceOf(alice), 0);               // redeemer never got it
        assertEq(rando.balanceOf(address(vault)), 777e18); // still sitting in the vault

        vault.rescue(address(rando), owner);               // owner recovers it
        assertEq(rando.balanceOf(owner), 777e18);
    }

    // A donation cannot be leveraged to dilute an honest holder even with
    // awayNav > 0 (the C-04-shaped surplus): the non-redeeming holder stays whole.
    function test_donationDoesNotDiluteHonestHolder_withAway() public {
        vm.prank(alice);
        vault.deposit(1_000e6, 0);
        vm.prank(bob);
        vault.deposit(1_000e6, 0);

        MockDlnSource dln = new MockDlnSource();
        vault.setDlnSource(IDlnSource(address(dln)));
        vault.setDestination(BASE, EVM_RECV, EVM_USDC, 5000);
        vm.deal(keeper, 1 ether);
        uint256 fee = dln.FEE();
        vm.prank(keeper);
        vault.fundDestination{value: fee}(BASE, 800e6, 760e6);
        vm.prank(keeper);
        vault.postNav(2_000e6, 800e6); // hNav 1,200; real home USDG 1,200

        vm.prank(donor);
        usdc.transfer(address(vault), 600e6); // outsider donates; real home 1,800

        vm.warp(block.timestamp + vault.withdrawDelay() + 1);
        vm.prank(alice);
        vault.redeemInKind(1_000e18, alice);

        // bob owns 1,000 shares; the real assets still backing the pool are the
        // on-chain USDG plus the 800 still away. He must be no worse than his deposit.
        uint256 realPoolAssets = usdc.balanceOf(address(vault)) + 800e6; // home + away
        uint256 bobClaim = realPoolAssets * vault.balanceOf(bob) / vault.totalSupply();
        assertGe(bobClaim, 1_000e6); // no dilution from the donation
    }
}
