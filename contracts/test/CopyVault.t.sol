// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {CopyVault, ICopyRouter} from "../src/CopyVault.sol";
import {IDlnSource} from "../src/IDlnSource.sol";
import {IERC20} from "../src/MiniERC20.sol";
import {MockDlnSource, MockERC20, MockRouter} from "../src/mocks/Mocks.sol";

contract CopyVaultTest is Test {
    CopyVault vault;
    MockERC20 usdc;
    MockERC20 meme1;
    MockERC20 meme2;
    MockRouter router;

    address keeper = makeAddr("keeper");
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");

    uint256 constant RATE_1TO1_6TO18 = 1e30; // $1 (1e6) -> 1 token (1e18)
    uint256 constant RATE_1TO1_18TO6 = 1e6; //  1 token (1e18) -> $1 (1e6)

    function setUp() public {
        usdc = new MockERC20("USD Coin", "USDC", 6);
        meme1 = new MockERC20("Chill", "CHILL", 18);
        meme2 = new MockERC20("Boomer", "BOOMER", 18);
        router = new MockRouter();
        vault = new CopyVault(IERC20(address(usdc)), keeper, ICopyRouter(address(router)));

        vault.setAllowedToken(address(meme1), true);
        vault.setAllowedToken(address(meme2), true);

        // router reserves + 1:1 dollar rates both ways
        meme1.mint(address(router), 1_000_000e18);
        meme2.mint(address(router), 1_000_000e18);
        usdc.mint(address(router), 1_000_000e6);
        router.setRate(address(usdc), address(meme1), RATE_1TO1_6TO18);
        router.setRate(address(usdc), address(meme2), RATE_1TO1_6TO18);
        router.setRate(address(meme1), address(usdc), RATE_1TO1_18TO6);
        router.setRate(address(meme2), address(usdc), RATE_1TO1_18TO6);

        usdc.mint(alice, 10_000e6);
        usdc.mint(bob, 10_000e6);
        vm.prank(alice);
        usdc.approve(address(vault), type(uint256).max);
        vm.prank(bob);
        usdc.approve(address(vault), type(uint256).max);
    }

    function deposit(address who, uint256 amount) internal returns (uint256) {
        vm.prank(who);
        return vault.deposit(amount);
    }

    // ---------------------------------------------------------------- shares

    function test_bootstrapDeposit() public {
        uint256 shares = deposit(alice, 1_000e6);
        assertEq(shares, 1_000e18);
        assertEq(vault.totalNavAsset(), 1_000e6);
    }

    function test_depositAtPostedNav() public {
        deposit(alice, 1_000e6);
        // vault value doubled since -> bob's $1000 buys half as many shares
        vm.prank(keeper);
        vault.postNav(2_000e6);
        uint256 shares = deposit(bob, 1_000e6);
        assertEq(shares, 500e18);
        assertEq(vault.totalNavAsset(), 3_000e6);
    }

    function test_staleNavBlocksDeposit() public {
        deposit(alice, 1_000e6);
        vm.prank(keeper);
        vault.postNav(1_000e6);
        vm.warp(block.timestamp + vault.navTtl() + 1);
        vm.prank(bob);
        vm.expectRevert(CopyVault.StaleNav.selector);
        vault.deposit(1_000e6);
    }

    // ---------------------------------------------------------------- trades

    function buyMeme1(uint256 usdcIn) internal {
        vm.prank(keeper);
        vault.mirrorTrade(address(usdc), address(meme1), usdcIn, 0);
    }

    function test_mirrorTradeBuyAndSell() public {
        deposit(alice, 1_000e6);
        vm.prank(keeper);
        vault.postNav(1_000e6);

        buyMeme1(50e6);
        assertEq(meme1.balanceOf(address(vault)), 50e18);
        assertEq(vault.heldTokensLength(), 1);

        vm.prank(keeper);
        vault.mirrorTrade(address(meme1), address(usdc), 50e18, 0);
        assertEq(meme1.balanceOf(address(vault)), 0);
        assertEq(vault.heldTokensLength(), 0);
        assertEq(usdc.balanceOf(address(vault)), 1_000e6);
    }

    function test_onlyExecutorTrades() public {
        deposit(alice, 1_000e6);
        vm.prank(alice);
        vm.expectRevert(CopyVault.NotExecutor.selector);
        vault.mirrorTrade(address(usdc), address(meme1), 10e6, 0);
    }

    function test_buyNeedsAllowlist() public {
        deposit(alice, 1_000e6);
        vm.prank(keeper);
        vault.postNav(1_000e6);
        MockERC20 rug = new MockERC20("Rug", "RUG", 18);
        vm.prank(keeper);
        vm.expectRevert(abi.encodeWithSelector(CopyVault.TokenNotAllowed.selector, address(rug)));
        vault.mirrorTrade(address(usdc), address(rug), 10e6, 0);
    }

    function test_buyCappedByMaxTradeBps() public {
        deposit(alice, 1_000e6);
        vm.prank(keeper);
        vault.postNav(1_000e6);
        // default cap 5% of NAV = $50
        vm.prank(keeper);
        vm.expectRevert(CopyVault.TradeTooLarge.selector);
        vault.mirrorTrade(address(usdc), address(meme1), 50e6 + 1, 0);
        buyMeme1(50e6); // at the cap passes
    }

    function test_sellRequiresHeldPosition() public {
        deposit(alice, 1_000e6);
        vm.prank(keeper);
        vm.expectRevert(abi.encodeWithSelector(CopyVault.TokenNotAllowed.selector, address(meme1)));
        vault.mirrorTrade(address(meme1), address(usdc), 1e18, 0);
    }

    function test_slippageBound() public {
        deposit(alice, 1_000e6);
        vm.prank(keeper);
        vault.postNav(1_000e6);
        vm.prank(keeper);
        vm.expectRevert(bytes("slippage"));
        vault.mirrorTrade(address(usdc), address(meme1), 50e6, 51e18);
    }

    // ---------------------------------------------------------------- redeem

    function test_redeemInKindProRata() public {
        deposit(alice, 1_000e6);
        deposit(bob, 1_000e6); // same block, nav still fresh from bootstrap path
        vm.prank(keeper);
        vault.postNav(2_000e6);
        buyMeme1(60e6);
        vm.prank(keeper);
        vault.mirrorTrade(address(usdc), address(meme2), 40e6, 0);

        vm.warp(block.timestamp + vault.withdrawDelay() + 1);
        uint256 aliceShares = vault.balanceOf(alice);
        vm.prank(alice);
        vault.redeemInKind(aliceShares, alice);

        // alice held half the shares -> half of every balance
        assertEq(usdc.balanceOf(alice), 10_000e6 - 1_000e6 + 950e6);
        assertEq(meme1.balanceOf(alice), 30e18);
        assertEq(meme2.balanceOf(alice), 20e18);
        assertEq(vault.balanceOf(alice), 0);
        // vault keeps bob's half
        assertEq(usdc.balanceOf(address(vault)), 950e6);
        assertEq(meme1.balanceOf(address(vault)), 30e18);
        assertEq(vault.totalNavAsset(), 1_000e6);

        // bob exits fully -> vault empty, held list cleared
        uint256 bobShares = vault.balanceOf(bob);
        vm.prank(bob);
        vault.redeemInKind(bobShares, bob);
        assertEq(vault.totalSupply(), 0);
        assertEq(vault.heldTokensLength(), 0);
        assertEq(usdc.balanceOf(address(vault)), 0);
    }

    function test_withdrawDelay() public {
        deposit(alice, 1_000e6);
        vm.prank(alice);
        vm.expectRevert(CopyVault.WithdrawLocked.selector);
        vault.redeemInKind(1e18, alice);
        vm.warp(block.timestamp + vault.withdrawDelay() + 1);
        vm.prank(alice);
        vault.redeemInKind(1e18, alice);
    }

    function test_redeemNeverNeedsFreshNav() public {
        deposit(alice, 1_000e6);
        buyMeme1AfterNav();
        vm.warp(block.timestamp + 30 days); // nav long stale
        uint256 shares = vault.balanceOf(alice);
        vm.prank(alice);
        vault.redeemInKind(shares, alice);
        assertEq(vault.totalSupply(), 0);
    }

    function buyMeme1AfterNav() internal {
        vm.prank(keeper);
        vault.postNav(1_000e6);
        buyMeme1(50e6);
    }

    // ---------------------------------------------------------------- sleeve

    bytes constant SLEEVE = hex"1f2e3d4c5b6a79880102030405060708090a0b0c0d0e0f10111213141516aa01";
    bytes constant SOL_USDC = hex"c6fa7af3bedbad3a3d65f36aabc97431b1bbe4c2d2f6e0e47ca60203452f5d61";

    function setupSleeve() internal returns (MockDlnSource dln) {
        dln = new MockDlnSource();
        vault.setSleeve(IDlnSource(address(dln)), SLEEVE, SOL_USDC, 3000);
        deposit(alice, 1_000e6);
        vm.prank(keeper);
        vault.postNav(1_000e6);
        vm.deal(keeper, 1 ether);
    }

    function test_fundSleevePinsReceiver() public {
        MockDlnSource dln = setupSleeve();
        uint256 fee = dln.FEE();
        vm.prank(keeper);
        bytes32 id = vault.fundSleeve{value: fee}(200e6, 199e6);
        assertTrue(id != bytes32(0));
        (address giveToken, uint256 giveAmount,,, uint256 takeChainId,,,,,,) = dln.lastOrder();
        assertEq(giveToken, address(usdc));
        assertEq(giveAmount, 200e6);
        assertEq(takeChainId, vault.sleeveChainId());
        // the executor never supplies a receiver; the order carries the pinned one
        assertEq(usdc.balanceOf(address(dln)), 200e6);
        assertEq(vault.sleeveFundedAsset(), 200e6);
    }

    function test_fundSleeveCapEnforced() public {
        MockDlnSource dln = setupSleeve();
        uint256 fee = dln.FEE();
        vm.prank(keeper);
        vault.fundSleeve{value: fee}(200e6, 0); // 20% of NAV
        vm.prank(keeper);
        vm.expectRevert(CopyVault.SleeveCapExceeded.selector);
        vault.fundSleeve{value: fee}(101e6, 0); // would pass 30% cap
        vm.prank(keeper);
        vault.noteSleeveReturn(200e6); // sleeve bridged everything back
        vm.prank(keeper);
        vault.fundSleeve{value: fee}(300e6, 0); // full cap available again
        assertEq(vault.sleeveFundedAsset(), 300e6);
    }

    function test_fundSleeveOnlyExecutorAndConfigured() public {
        MockDlnSource dln = setupSleeve();
        vm.prank(alice);
        vm.expectRevert(CopyVault.NotExecutor.selector);
        vault.fundSleeve(10e6, 0);

        CopyVault bare = new CopyVault(IERC20(address(usdc)), keeper, ICopyRouter(address(router)));
        uint256 fee = dln.FEE();
        vm.prank(keeper);
        vm.expectRevert(CopyVault.SleeveNotConfigured.selector);
        bare.fundSleeve{value: fee}(10e6, 0);
    }

    function test_setSleeveValidatesPubkeys() public {
        MockDlnSource dln = new MockDlnSource();
        vm.expectRevert(bytes("pubkey"));
        vault.setSleeve(IDlnSource(address(dln)), hex"beef", SOL_USDC, 3000);
    }
}
