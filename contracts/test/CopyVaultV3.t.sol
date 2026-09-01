// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {CopyVaultV3, IWETH} from "../src/CopyVaultV3.sol";
import {ICopyRouter} from "../src/CopyVault.sol";
import {IDlnSource} from "../src/IDlnSource.sol";
import {IERC20} from "../src/MiniERC20.sol";
import {MockDlnSource, MockERC20, MockRouter, MockWETH} from "../src/mocks/Mocks.sol";

contract CopyVaultV3Test is Test {
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

    // ---- v2 parity: deposit / mirror / redeem with no away, no fees ----

    function test_depositMirrorRedeemParity() public {
        seed(1_000e6);
        vm.prank(keeper);
        vault.mirrorTrade(address(usdc), address(meme), 50e6, 0);
        assertEq(meme.balanceOf(address(vault)), 50e18);
        vm.warp(block.timestamp + vault.withdrawDelay() + 1);
        uint256 sh = vault.balanceOf(alice);
        vm.prank(alice);
        vault.redeemInKind(sh, alice);
        assertEq(meme.balanceOf(alice), 50e18);
        assertEq(vault.totalSupply(), 0);
    }

    // ---- deposit slippage floor ----

    function test_minSharesOutReverts() public {
        seed(1_000e6);
        // NAV moved up: same USDG now buys fewer shares than alice demands
        vm.prank(keeper);
        vault.postNav(2_000e6, 0);
        vm.prank(bob);
        vm.expectRevert(bytes("minSharesOut"));
        vault.deposit(100e6, 100e18); // would only get ~50e18 shares at 2x NAV
    }

    function test_minSharesOutPasses() public {
        seed(1_000e6);
        vm.prank(bob);
        uint256 shares = vault.deposit(100e6, 99e18); // ~100e18 at 1x
        assertGe(shares, 99e18);
    }

    // ---- deposit fee ----

    function test_depositFeeToTreasury() public {
        vault.setFees(100, 0, 0); // 1% deposit fee
        vm.prank(alice);
        uint256 shares = vault.deposit(1_000e6, 0);
        assertEq(usdc.balanceOf(treasury), 10e6); // 1% skimmed
        assertEq(vault.totalNavAsset(), 990e6); // net credited
        assertEq(shares, 990e18); // shares on net
    }

    // ---- performance fee with high-water mark ----

    function test_perfFeeCrystallizesOnGain() public {
        vault.setFees(0, 0, 1000); // 10% perf fee
        seed(1_000e6); // hwm seeded at pps = 1e6
        vm.warp(block.timestamp + vault.withdrawDelay() + 1);

        vm.prank(keeper);
        vault.postNav(1_200e6, 0); // +20%
        // redeem triggers crystallization
        vm.prank(alice);
        vault.redeemInKind(100e18, alice);

        // treasury received fee shares for 10% of the 200e6 gain (~20e6 worth)
        uint256 tShares = vault.balanceOf(treasury);
        assertGt(tShares, 0);
        assertApproxEqAbs(tShares, 16.949e18, 0.05e18);
        assertEq(vault.hwm(), 1.2e6);

        // no double-tax: drop below HWM, crystallize again -> no new fee shares
        vm.prank(keeper);
        vault.postNav(1_000e6, 0);
        vm.prank(alice);
        vault.redeemInKind(100e18, alice);
        assertEq(vault.balanceOf(treasury), tShares); // unchanged
    }

    function test_noPerfFeeWhenFlat() public {
        vault.setFees(0, 0, 1000);
        seed(1_000e6);
        vm.warp(block.timestamp + vault.withdrawDelay() + 1);
        vm.prank(keeper);
        vault.postNav(1_000e6, 0); // flat
        vm.prank(alice);
        vault.redeemInKind(100e18, alice);
        assertEq(vault.balanceOf(treasury), 0);
    }

    // ---- management / AUM fee (continuous, streamed to treasury) ----

    function test_mgmtFeeAccruesOverTime() public {
        vault.setFees(0, 200, 0); // 2%/yr AUM fee, nothing else
        seed(1_000e6);            // lastAccrualTs starts at first deposit
        vm.warp(block.timestamp + 365 days);
        vault.accrue();           // permissionless crystallization
        uint256 tShares = vault.balanceOf(treasury);
        assertApproxEqAbs(tShares, 20.408e18, 0.05e18);
        // the treasury's shares are worth ~2% of the $1,000 NAV
        uint256 tVal = tShares * vault.pricePerShare() / 1e18;
        assertApproxEqAbs(tVal, 20e6, 0.1e6);
    }

    function test_mgmtFeeProRataByTime() public {
        vault.setFees(0, 200, 0);
        seed(1_000e6);
        vm.warp(block.timestamp + 182 days + 12 hours); // ~half a year
        vault.accrue();
        uint256 tVal = vault.balanceOf(treasury) * vault.pricePerShare() / 1e18;
        assertApproxEqAbs(tVal, 10e6, 0.1e6); // ~1% for half a year at 2%/yr
    }

    function test_mgmtFeeCapEnforced() public {
        vm.expectRevert(bytes("mgmt fee"));
        vault.setFees(0, 501, 0); // > 5%/yr
        vault.setFees(0, 500, 0); // at cap ok
        assertEq(vault.mgmtFeeBps(), 500);
    }

    function test_accrueIsPermissionless() public {
        vault.setFees(0, 200, 0);
        seed(1_000e6);
        vm.warp(block.timestamp + 365 days);
        vm.prank(alice); // not owner, not keeper
        vault.accrue();
        assertGt(vault.balanceOf(treasury), 0);
    }

    function test_mgmtFeeSurvivesFrequentAccrue() public {
        // Small vault + high rate: a 1s step truncates the per-call fee to 0.
        vault.setFees(0, 500, 0); // 5%/yr
        seed(300e6);              // $300 — feeAssets rounds to 0 for tiny dt
        // Hammer the permissionless accrue() every second. Under the buggy
        // always-advance clock this starves the fee to exactly zero; with the
        // remainder-preserving clock the elapsed time is charged as it rounds up.
        for (uint256 i = 0; i < 200; i++) {
            vm.warp(block.timestamp + 1);
            vault.accrue();
        }
        assertGt(vault.balanceOf(treasury), 0); // would be 0 if time were dropped
    }

    function test_mgmtAndPerfFeesStack() public {
        vault.setFees(0, 200, 1000); // 2%/yr AUM + 10% performance
        seed(1_000e6);               // hwm seeded at pps 1e6
        vm.warp(block.timestamp + 365 days);
        vm.prank(keeper);
        vault.postNav(1_500e6, 0);   // +50% gross over the year
        vault.accrue();
        // both fees minted: mgmt ~2% of 1,500 (~$30) plus perf ~10% of the gain
        // above HWM net of mgmt (~$48) => ~$77 of treasury value; well above the
        // ~$30 that mgmt alone would produce.
        uint256 tVal = vault.balanceOf(treasury) * vault.pricePerShare() / 1e18;
        assertGt(tVal, 70e6);
        assertLt(tVal, 85e6);
        assertGt(vault.hwm(), 1e6); // high-water mark advanced
    }

    // ---- away-aware in-kind redemption: the cross-chain haircut fix ----

    function test_awayRedemptionKeepsRedeemerWhole() public {
        // two equal depositors, then 30% of NAV bridged away
        vm.prank(alice);
        vault.deposit(1_000e6, 0);
        vm.prank(bob);
        vault.deposit(1_000e6, 0);
        vm.prank(keeper);
        vault.postNav(2_000e6, 0);

        // move 600e6 (30%) off-chain via a real DLN order, then re-mark with away
        MockDlnSource dln = new MockDlnSource();
        vault.setDlnSource(IDlnSource(address(dln)));
        vault.setDestination(BASE, EVM_RECV, EVM_USDC, 5000);
        vm.deal(keeper, 1 ether);
        uint256 fee = dln.FEE(); // hoist: a view call here would consume the prank
        vm.prank(keeper);
        vault.fundDestination{value: fee}(BASE, 600e6, 0);
        assertEq(usdc.balanceOf(address(vault)), 1_400e6); // home holdings
        vm.prank(keeper);
        vault.postNav(2_000e6, 600e6); // total unchanged, 600 now marked away

        vm.warp(block.timestamp + vault.withdrawDelay() + 1);
        uint256 ppsBefore = vault.pricePerShare();

        // alice redeems all her shares
        vm.prank(alice);
        vault.redeemInKind(1_000e18, alice);

        // she got her full home slice in USDG (700e6) and KEEPS her away slice
        assertEq(usdc.balanceOf(alice), 999_000e6 + 700e6); // started 1,000,000 - 1,000 deposit
        assertEq(vault.balanceOf(alice), 300e18); // away fraction retained as shares

        // total entitlement preserved: 700 USDG now + 300 shares worth 300e6 = 1,000
        assertEq(vault.balanceOf(alice) * vault.pricePerShare() / 1e18, 300e6);

        // PPS unchanged for everyone (bob not diluted, alice not haircut)
        assertEq(vault.pricePerShare(), ppsBefore);
        assertEq(vault.balanceOf(bob), 1_000e18);
    }

    function test_redeemRevertsWhenAllAway() public {
        seed(1_000e6);
        vm.prank(keeper);
        vault.postNav(1_000e6, 1_000e6); // everything away, nothing home
        vm.warp(block.timestamp + vault.withdrawDelay() + 1);
        vm.prank(alice);
        vm.expectRevert(bytes("no home liquidity"));
        vault.redeemInKind(100e18, alice);
    }

    // ---- pause / guardian ----

    function test_guardianPauseBlocksDepositNotRedeem() public {
        seed(1_000e6);
        vm.warp(block.timestamp + vault.withdrawDelay() + 1);

        vm.prank(guardian);
        vault.pause();

        vm.prank(bob);
        vm.expectRevert(CopyVaultV3.IsPaused.selector);
        vault.deposit(100e6, 0);

        vm.prank(keeper);
        vm.expectRevert(CopyVaultV3.IsPaused.selector);
        vault.mirrorTrade(address(usdc), address(meme), 10e6, 0);

        // redemption still works while paused — funds are never trapped
        vm.prank(alice);
        vault.redeemInKind(100e18, alice);
        assertGt(usdc.balanceOf(alice), 999_000e6);
    }

    function test_onlyOwnerUnpauses() public {
        vm.prank(guardian);
        vault.pause();
        vm.prank(keeper);
        vm.expectRevert(CopyVaultV3.NotOwner.selector);
        vault.unpause();
        vault.unpause(); // owner
        assertFalse(vault.paused());
    }

    // ---- NAV bounds ----

    function test_navBoundsRejectJump() public {
        seed(1_000e6);
        vault.setNavBounds(2000); // 20%
        vm.prank(keeper);
        vm.expectRevert(CopyVaultV3.NavBounds.selector);
        vault.postNav(1_300e6, 0); // +30%
        vm.prank(keeper);
        vault.postNav(1_150e6, 0); // +15% ok
        assertEq(vault.totalNavAsset(), 1_150e6);
        // owner override bypasses the bound for a genuine large move
        vault.postNavOverride(1_600e6, 0);
        assertEq(vault.totalNavAsset(), 1_600e6);
    }

    // ---- NAV-funded gas ----

    function test_sweepGasRefuelsKeeperFromNav() public {
        vm.warp(1_700_000_000); // realistic chain time: first-ever sweep clears the cooldown-from-genesis
        seed(1_000e6);
        // fund the mock WETH/router so a USDG->ETH route exists
        vm.deal(address(this), 10 ether);
        weth.deposit{value: 1 ether}();
        weth.transfer(address(router), 1 ether);
        router.setRate(address(usdc), address(weth), 3e28); // ~$3333/ETH (6dp USDG -> 18dp WETH)

        vault.setGasSweep(50, 1 hours); // 0.5% of NAV per sweep
        uint256 before = keeper.balance;

        vm.prank(keeper);
        vault.sweepGas(5e6, 0.1 ether); // 5 USDG -> ~0.15 ETH
        assertApproxEqAbs(keeper.balance - before, 0.15 ether, 0.001 ether);
        assertEq(vault.totalNavAsset(), 995e6); // gas expensed from NAV

        // cap enforced (NAV is now 995e6, so 0.5% cap is ~4.975e6)
        vm.prank(keeper);
        vm.expectRevert(bytes("too much"));
        vault.sweepGas(6e6, 0); // > 0.5% of NAV

        // cooldown enforced (4e6 is within the cap, so this reaches the cooldown check)
        vm.prank(keeper);
        vm.expectRevert(bytes("cooldown"));
        vault.sweepGas(4e6, 0);
        vm.warp(block.timestamp + 1 hours + 1);
        vm.prank(keeper);
        vault.postNav(995e6, 0); // keeper re-marks NAV each cycle; keeps it fresh for the sweep
        vm.prank(keeper);
        vault.sweepGas(4e6, 0.1 ether); // ok after cooldown
    }

    // ---- role separation & bounded params ----

    function test_keeperCannotSetRouter() public {
        vm.prank(keeper);
        vm.expectRevert(CopyVaultV3.NotOwner.selector);
        vault.setRouter(ICopyRouter(address(0xdead)));
    }

    function test_withdrawDelayBounded() public {
        vm.expectRevert(bytes("delay"));
        vault.setParams(500, 8 days, 15 minutes); // > MAX_WITHDRAW_DELAY
        vault.setParams(500, 7 days, 15 minutes); // at cap ok
        assertEq(vault.withdrawDelay(), 7 days);
    }

    function test_twoStepOwnership() public {
        vault.transferOwnership(alice);
        assertEq(vault.owner(), address(this)); // not yet
        vm.prank(bob);
        vm.expectRevert(bytes("not pending"));
        vault.acceptOwnership();
        vm.prank(alice);
        vault.acceptOwnership();
        assertEq(vault.owner(), alice);
    }
}
