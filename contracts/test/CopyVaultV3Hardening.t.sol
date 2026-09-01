// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {CopyVaultV3, IWETH} from "../src/CopyVaultV3.sol";
import {ICopyRouter} from "../src/CopyVault.sol";
import {IDlnSource} from "../src/IDlnSource.sol";
import {IERC20} from "../src/MiniERC20.sol";
import {MockDlnSource, MockERC20, MockRouter, MockWETH, HostileERC20} from "../src/mocks/Mocks.sol";

/// Regression tests for the pre-deploy hardening pass. Each test pins a specific
/// finding from the security review so it can't silently regress.
contract CopyVaultV3HardeningTest is Test {
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
    address carol = makeAddr("carol");

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
        vault.setAllowedToken(address(meme), true);
        meme.mint(address(router), 1_000_000e18);
        usdc.mint(address(router), 1_000_000e6);
        router.setRate(address(usdc), address(meme), 1e30);
        router.setRate(address(meme), address(usdc), 1e6);

        usdc.mint(alice, 1_000_000e6);
        usdc.mint(bob, 1_000_000e6);
        vm.prank(alice);
        usdc.approve(address(vault), type(uint256).max);
        vm.prank(bob);
        usdc.approve(address(vault), type(uint256).max);
    }

    function seed(uint256 amt) internal {
        vm.prank(alice);
        vault.deposit(amt, 0);
        vm.prank(keeper);
        vault.postNav(amt, 0);
    }

    // ---- C-05: withdraw-delay follows the shares (no fresh-address bypass) ----

    function test_withdrawDelayFollowsTransferredShares() public {
        seed(1_000e6); // alice holds 1,000e18 shares, delay clock started now
        vm.prank(alice);
        vault.balanceOf(alice); // (no-op read for clarity)

        // move shares to a brand-new address that never deposited
        vm.prank(alice);
        IERC20(address(vault)).transfer(carol, 500e18);

        // carol cannot redeem immediately: receiving shares started her own clock
        vm.prank(carol);
        vm.expectRevert(CopyVaultV3.WithdrawLocked.selector);
        vault.redeemInKind(500e18, carol);

        // after the delay, the same redemption works
        vm.warp(block.timestamp + vault.withdrawDelay() + 1);
        vm.prank(carol);
        vault.redeemInKind(500e18, carol);
        assertGt(usdc.balanceOf(carol), 0);
    }

    // ---- C-03: a manipulated low NAV cannot mint more shares than real backing ----

    function test_depositFloorBlocksNavManipulationMint() public {
        seed(1_000e6); // 1,000 USDG on hand, 1,000e18 shares
        // keeper posts a near-zero NAV (bounds are off by default on a bare vault)
        vm.prank(keeper);
        vault.postNav(1, 0);

        // bob deposits 1 USDG: without the floor this mints ~1e27 shares and steals
        // the vault; the floor prices against the real 1,000 USDG on hand instead.
        vm.prank(bob);
        uint256 shares = vault.deposit(1e6, 0);
        assertLt(shares, 2e18);            // ~1e18, proportional to real backing
        assertLt(shares, vault.balanceOf(alice)); // bob nowhere near owning the pool
    }

    // ---- C-02: trade cap is anchored to the real balance, not self-posted NAV ----

    function test_mirrorTradeCapAnchoredToBalanceNotNav() public {
        seed(1_000e6); // real USDG balance 1,000e6 -> 5% cap = 50e6
        // keeper inflates NAV; the cap must NOT move with it
        vm.prank(keeper);
        vault.postNav(1_000_000e6, 0);

        vm.prank(keeper);
        vm.expectRevert(CopyVaultV3.TradeTooLarge.selector);
        vault.mirrorTrade(address(usdc), address(meme), 100e6, 1); // > 5% of real balance

        vm.prank(keeper);
        vault.mirrorTrade(address(usdc), address(meme), 50e6, 1); // exactly at the real-balance cap
    }

    function test_mirrorTradeRejectsZeroMinOut() public {
        seed(1_000e6);
        vm.prank(keeper);
        vm.expectRevert(bytes("minOut"));
        vault.mirrorTrade(address(usdc), address(meme), 10e6, 0);
    }

    // ---- C-01: cross-chain funding must promise a real return ----

    function test_fundDestinationEnforcesReturnFloor() public {
        seed(1_000e6);
        MockDlnSource dln = new MockDlnSource();
        vault.setDlnSource(IDlnSource(address(dln)));
        vault.setDestination(BASE, EVM_RECV, EVM_USDC, 5000);
        vm.deal(keeper, 1 ether);
        uint256 fee = dln.FEE();

        // takeAmountMin below 90% of the give amount is rejected
        vm.prank(keeper);
        vm.expectRevert(bytes("return floor"));
        vault.fundDestination{value: fee}(BASE, 100e6, 50e6);

        // at/above the floor it goes through
        vm.prank(keeper);
        vault.fundDestination{value: fee}(BASE, 100e6, 90e6);
        assertEq(usdc.balanceOf(address(vault)), 900e6);
    }

    // ---- C-04: repatriation reconciles awayNav so the stale window closes ----

    function test_noteReturnReducesAwayNav() public {
        seed(1_000e6);
        MockDlnSource dln = new MockDlnSource();
        vault.setDlnSource(IDlnSource(address(dln)));
        vault.setDestination(BASE, EVM_RECV, EVM_USDC, 5000);
        vm.deal(keeper, 1 ether);
        uint256 fee = dln.FEE();

        vm.prank(keeper);
        vault.fundDestination{value: fee}(BASE, 400e6, 400e6);
        vm.prank(keeper);
        vault.postNav(1_000e6, 400e6); // 400 marked away
        assertEq(vault.homeNav(), 600e6);

        // capital comes home: awayNav must drop in step, not wait for the next postNav
        vm.prank(keeper);
        vault.noteDestinationReturn(BASE, 400e6);
        assertEq(vault.awayNav(), 0);
        assertEq(vault.homeNav(), 1_000e6);
    }

    // ---- H-01: one hostile held token cannot brick redemptions ----

    function test_hostileHeldTokenDoesNotBrickRedemption() public {
        HostileERC20 evil = new HostileERC20();
        evil.mint(address(router), 1_000_000e18);
        router.setRate(address(usdc), address(evil), 1e30);
        vault.setAllowedToken(address(evil), true);

        seed(1_000e6);
        vm.prank(keeper);
        vault.mirrorTrade(address(usdc), address(meme), 30e6, 1); // vault now holds meme
        vm.prank(keeper);
        vault.mirrorTrade(address(usdc), address(evil), 30e6, 1); // ...and the hostile token

        evil.setHostile(true); // token turns hostile: its transfer now reverts
        vm.warp(block.timestamp + vault.withdrawDelay() + 1);

        // redemption succeeds anyway: good tokens pay out, the hostile slice is skipped
        vm.prank(alice);
        vault.redeemInKind(400e18, alice);
        assertGt(usdc.balanceOf(alice), 999_000e6); // got USDG
        assertGt(meme.balanceOf(alice), 0);          // got the good token
        assertGt(evil.balanceOf(address(vault)), 0); // hostile slice retained, not lost to a revert
    }

    function test_ownerCanQuarantineHostileToken() public {
        HostileERC20 evil = new HostileERC20();
        evil.mint(address(router), 1_000_000e18);
        router.setRate(address(usdc), address(evil), 1e30);
        vault.setAllowedToken(address(evil), true);

        seed(1_000e6);
        vm.prank(keeper);
        vault.mirrorTrade(address(usdc), address(evil), 50e6, 1);
        assertEq(vault.heldTokensLength(), 1);

        vault.quarantineHeldToken(address(evil)); // owner ejects it from the payout set
        assertEq(vault.heldTokensLength(), 0);
    }

    // ---- temporary per-address deposit cap (beta training-wheel) ----

    function test_depositCapEnforcedPerAddress() public {
        vault.setMaxDepositPerAddress(500e6); // $500
        // first deposit under the cap is fine
        vm.prank(alice);
        vault.deposit(400e6, 0);
        vm.prank(keeper);
        vault.postNav(400e6, 0);
        // a second deposit that would push alice over $500 reverts
        vm.prank(alice);
        vm.expectRevert(bytes("deposit cap"));
        vault.deposit(200e6, 0);
        // ...but exactly up to the cap is allowed
        vm.prank(alice);
        vault.deposit(100e6, 0);
        assertEq(vault.depositedAssets(alice), 500e6);
    }

    function test_depositCapIsPerAddressNotGlobal() public {
        vault.setMaxDepositPerAddress(500e6);
        vm.prank(alice);
        vault.deposit(500e6, 0);
        vm.prank(keeper);
        vault.postNav(500e6, 0);
        // bob is a different address: his own $500 is unaffected by alice's
        vm.prank(bob);
        vault.deposit(500e6, 0);
        assertEq(vault.depositedAssets(bob), 500e6);
    }

    function test_redeemFreesDepositCapRoom() public {
        vault.setMaxDepositPerAddress(500e6);
        vm.prank(alice);
        vault.deposit(500e6, 0); // at the cap
        vm.prank(keeper);
        vault.postNav(500e6, 0);
        vm.warp(block.timestamp + vault.withdrawDelay() + 1);

        // fully exit -> tracked principal returns to ~0
        uint256 aliceShares = vault.balanceOf(alice); // hoist: a view here would consume the prank
        vm.prank(alice);
        vault.redeemInKind(aliceShares, alice);
        assertEq(vault.depositedAssets(alice), 0);

        // alice can deposit again up to the cap
        vm.prank(alice);
        vault.deposit(500e6, 0);
        assertEq(vault.depositedAssets(alice), 500e6);
    }

    function test_ownerCanLiftDepositCap() public {
        vault.setMaxDepositPerAddress(500e6);
        vault.setMaxDepositPerAddress(0); // lifted
        vm.prank(alice);
        vault.deposit(10_000e6, 0); // well over the old cap, now fine
        assertEq(vault.balanceOf(alice), 10_000e18);
    }

    // ---- M-01: gas-sweep cooldown cannot be set to zero ----

    function test_gasSweepCooldownFloor() public {
        vm.expectRevert(bytes("cooldown"));
        vault.setGasSweep(50, 10 minutes);
        vault.setGasSweep(50, 15 minutes); // at the floor, ok
        assertEq(vault.gasSweepCooldown(), 15 minutes);
    }

    // ---- L-02: stray-fund rescue, but never managed funds ----

    function test_rescueStrayTokenbutNotManaged() public {
        // a token nobody registered lands in the vault
        MockERC20 stray = new MockERC20("Stray", "STRY", 18);
        stray.mint(address(vault), 5e18);
        vault.rescue(address(stray), owner);
        assertEq(stray.balanceOf(owner), 5e18);

        // the asset itself can never be rescued
        vm.expectRevert(bytes("managed"));
        vault.rescue(address(usdc), owner);

        // nor a token currently held as a position
        seed(1_000e6);
        vm.prank(keeper);
        vault.mirrorTrade(address(usdc), address(meme), 50e6, 1);
        vm.expectRevert(bytes("managed"));
        vault.rescue(address(meme), owner);
    }

    function test_rescueStrayEth() public {
        (bool ok,) = address(vault).call{value: 1 ether}("");
        assertTrue(ok);
        uint256 before = owner.balance;
        vault.rescue(address(0), owner);
        assertEq(owner.balance - before, 1 ether);
    }

    // owner needs to be able to receive ETH for the rescue test
    receive() external payable {}
}
