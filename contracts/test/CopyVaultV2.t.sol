// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {CopyVaultV2} from "../src/CopyVaultV2.sol";
import {ICopyRouter} from "../src/CopyVault.sol";
import {IDlnSource} from "../src/IDlnSource.sol";
import {IERC20} from "../src/MiniERC20.sol";
import {MockDlnSource, MockERC20, MockRouter} from "../src/mocks/Mocks.sol";

contract CopyVaultV2Test is Test {
    CopyVaultV2 vault;
    MockERC20 usdc;
    MockERC20 meme;
    MockRouter router;

    address keeper = makeAddr("keeper");
    address alice = makeAddr("alice");

    uint256 constant SOLANA = 7565164;
    uint256 constant BASE = 8453;

    bytes SOL_RECV = hex"1f2e3d4c5b6a79880102030405060708090a0b0c0d0e0f10111213141516aa01"; // 32b
    bytes SOL_USDC = hex"c6fa7af3bedbad3a3d65f36aabc97431b1bbe4c2d2f6e0e47ca60203452f5d61"; // 32b
    bytes EVM_RECV = hex"27813048104759935DD6D505e8cddda1a5f4EFA1"; // 20b keeper
    bytes EVM_USDC = hex"833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"; // 20b Base USDC

    function setUp() public {
        usdc = new MockERC20("USD Coin", "USDC", 6);
        meme = new MockERC20("Chill", "CHILL", 18);
        router = new MockRouter();
        vault = new CopyVaultV2(IERC20(address(usdc)), keeper, ICopyRouter(address(router)));
        vault.setAllowedToken(address(meme), true);
        meme.mint(address(router), 1_000_000e18);
        usdc.mint(address(router), 1_000_000e6);
        router.setRate(address(usdc), address(meme), 1e30);
        router.setRate(address(meme), address(usdc), 1e6);
        usdc.mint(alice, 100_000e6);
        vm.prank(alice);
        usdc.approve(address(vault), type(uint256).max);
    }

    function seed(uint256 amt) internal {
        vm.prank(alice);
        vault.deposit(amt);
        vm.prank(keeper);
        vault.postNav(amt);
    }

    // ---- core behavior unchanged from v1 ----

    function test_depositMirrorRedeem() public {
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

    // ---- new: multi-destination funding ----

    function test_fundMultipleDestinations() public {
        MockDlnSource dln = new MockDlnSource();
        vault.setDlnSource(IDlnSource(address(dln)));
        vault.setDestination(SOLANA, SOL_RECV, SOL_USDC, 3000);
        vault.setDestination(BASE, EVM_RECV, EVM_USDC, 3000);
        seed(1_000e6);
        vm.deal(keeper, 1 ether);
        uint256 fee = dln.FEE();

        // fund Solana destination
        vm.prank(keeper);
        vault.fundDestination{value: fee}(SOLANA, 100e6, 99e6);
        // fund Base destination — independent cap tracker
        vm.prank(keeper);
        vault.fundDestination{value: fee}(BASE, 200e6, 199e6);

        (, , , uint256 solFunded,) = vault.destination(SOLANA);
        (bytes memory br, bytes memory bt, , uint256 baseFunded, bool set) = vault.destination(BASE);
        assertEq(solFunded, 100e6);
        assertEq(baseFunded, 200e6);
        assertEq(br, EVM_RECV); // receiver pinned to the keeper on Base
        assertEq(bt, EVM_USDC);
        assertTrue(set);
        assertEq(usdc.balanceOf(address(dln)), 300e6);
        // last order carried Base's chainId + receiver
        (, , , , uint256 takeChainId, , , , , , ) = dln.lastOrder();
        assertEq(takeChainId, BASE);
    }

    function test_perDestinationCapEnforced() public {
        MockDlnSource dln = new MockDlnSource();
        vault.setDlnSource(IDlnSource(address(dln)));
        vault.setDestination(BASE, EVM_RECV, EVM_USDC, 3000); // 30% of NAV
        seed(1_000e6);
        vm.deal(keeper, 1 ether);
        uint256 fee = dln.FEE();
        vm.prank(keeper);
        vm.expectRevert(CopyVaultV2.DestCapExceeded.selector);
        vault.fundDestination{value: fee}(BASE, 301e6, 0); // >30%
        vm.prank(keeper);
        vault.fundDestination{value: fee}(BASE, 300e6, 0); // at cap ok
        vm.prank(keeper);
        vault.noteDestinationReturn(BASE, 300e6); // bridged back frees the cap
        vm.prank(keeper);
        vault.fundDestination{value: fee}(BASE, 300e6, 0);
    }

    function test_unconfiguredDestinationReverts() public {
        MockDlnSource dln = new MockDlnSource();
        vault.setDlnSource(IDlnSource(address(dln)));
        seed(1_000e6);
        vm.deal(keeper, 1 ether);
        uint256 fee = dln.FEE();
        vm.prank(keeper);
        vm.expectRevert(CopyVaultV2.DestNotConfigured.selector);
        vault.fundDestination{value: fee}(BASE, 10e6, 0);
    }

    function test_receiverLengthValidated() public {
        vm.expectRevert(bytes("receiver"));
        vault.setDestination(BASE, hex"beef", EVM_USDC, 3000); // 2 bytes, invalid
    }

    // ---- sleeve wrappers still work (backward compat) ----

    function test_sleeveWrappersMapToSolanaDestination() public {
        MockDlnSource dln = new MockDlnSource();
        vault.setSleeve(IDlnSource(address(dln)), SOL_RECV, SOL_USDC, 3000);
        seed(1_000e6);
        vm.deal(keeper, 1 ether);
        uint256 fee = dln.FEE();
        vm.prank(keeper);
        vault.fundSleeve{value: fee}(100e6, 99e6);
        assertEq(vault.sleeveFundedAsset(), 100e6);
        assertEq(vault.sleeveReceiver(), SOL_RECV);
        assertEq(vault.sleeveCapBps(), 3000);
        (, , , uint256 solFunded,) = vault.destination(SOLANA);
        assertEq(solFunded, 100e6); // wrapper and generic view agree
        vm.prank(keeper);
        vault.noteSleeveReturn(100e6);
        assertEq(vault.sleeveFundedAsset(), 0);
    }
}
