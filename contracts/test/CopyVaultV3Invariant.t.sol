// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {CopyVaultV3, IWETH} from "../src/CopyVaultV3.sol";
import {ICopyRouter} from "../src/CopyVault.sol";
import {IERC20} from "../src/MiniERC20.sol";
import {MockERC20, MockRouter, MockWETH} from "../src/mocks/Mocks.sol";

/// Fuzzes random deposit / redeem / share-transfer sequences across many actors
/// and asserts the accounting invariants hold no matter the ordering. Focused on
/// the bespoke share + cap accounting, which is exactly where a subtle bug hides.
contract Handler is Test {
    CopyVaultV3 public vault;
    MockERC20 public usdc;
    address public keeper;
    address[] public actors;
    uint256 public deposits; // successful-op counters, so we can prove the campaign wasn't vacuous
    uint256 public redeems;

    constructor(CopyVaultV3 v, MockERC20 u, address k, address[] memory a) {
        vault = v;
        usdc = u;
        keeper = k;
        actors = a;
    }

    // keep NAV fresh so deposits/redeems don't just revert on staleness
    function _refreshNav() internal {
        if (vault.totalSupply() == 0) return;
        uint256 n = vault.totalNavAsset(); // hoist views before the prank
        uint256 aw = vault.awayNav();
        vm.prank(keeper);
        vault.postNav(n, aw);
    }

    function deposit(uint256 actorSeed, uint256 amt) public {
        address a = actors[actorSeed % actors.length];
        amt = bound(amt, 1e6, 600e6); // spans the $500 per-address cap on purpose
        vm.warp(block.timestamp + 30);
        _refreshNav();
        usdc.mint(a, amt);
        vm.startPrank(a);
        usdc.approve(address(vault), amt);
        try vault.deposit(amt, 0) {
            deposits++;
        } catch {}
        vm.stopPrank();
    }

    function redeem(uint256 actorSeed, uint256 shareSeed) public {
        address a = actors[actorSeed % actors.length];
        uint256 bal = vault.balanceOf(a);
        if (bal == 0) return;
        uint256 sh = bound(shareSeed, 1, bal);
        vm.warp(block.timestamp + vault.withdrawDelay() + 1);
        _refreshNav();
        vm.prank(a);
        try vault.redeemInKind(sh, a) {
            redeems++;
        } catch {}
    }

    function transferShares(uint256 fromSeed, uint256 toSeed, uint256 amt) public {
        address f = actors[fromSeed % actors.length];
        address t = actors[toSeed % actors.length];
        uint256 bal = vault.balanceOf(f);
        if (bal == 0) return;
        amt = bound(amt, 1, bal);
        vm.prank(f);
        try IERC20(address(vault)).transfer(t, amt) {} catch {}
    }

    function actorCount() external view returns (uint256) {
        return actors.length;
    }
}

contract CopyVaultV3InvariantTest is Test {
    CopyVaultV3 vault;
    MockERC20 usdc;
    MockRouter router;
    MockWETH weth;
    Handler handler;

    address keeper = makeAddr("keeper");
    address[] actors;

    uint256 constant PER_ADDR_CAP = 500e6;
    uint256 constant TVL_CAP = 5_000e6;

    function setUp() public {
        usdc = new MockERC20("USD Coin", "USDG", 6);
        router = new MockRouter();
        weth = new MockWETH();
        vault = new CopyVaultV3(
            IERC20(address(usdc)),
            IWETH(address(weth)),
            keeper,
            makeAddr("guardian"),
            makeAddr("treasury"),
            ICopyRouter(address(router))
        );
        vault.setMaxDepositPerAddress(PER_ADDR_CAP);
        vault.setMaxTotalDeposits(TVL_CAP);

        for (uint256 i = 0; i < 12; i++) actors.push(vm.addr(i + 1));
        handler = new Handler(vault, usdc, keeper, actors);
        targetContract(address(handler));
    }

    // ERC20 supply conservation: no shares are minted or burned into thin air.
    function invariant_supplyEqualsSumOfBalances() public view {
        uint256 sum;
        for (uint256 i = 0; i < actors.length; i++) sum += vault.balanceOf(actors[i]);
        assertEq(vault.totalSupply(), sum);
    }

    // The caps are never breached under any interleaving of deposits/redeems.
    function invariant_perAddressCapNeverExceeded() public view {
        for (uint256 i = 0; i < actors.length; i++) {
            assertLe(vault.depositedAssets(actors[i]), PER_ADDR_CAP);
        }
    }

    // Real-time TVL cap: live NAV never exceeds the ceiling (the fuzz re-posts NAV
    // flat, so it only grows via capped deposits and shrinks on redeem).
    function invariant_tvlCapNeverExceeded() public view {
        assertLe(vault.totalNavAsset(), TVL_CAP);
    }

    // Prove the campaign actually exercised the deposit and redeem paths, so the
    // invariants above aren't passing vacuously on empty state.
    function afterInvariant() public view {
        assertGt(handler.deposits(), 0, "fuzz never deposited");
        assertGt(handler.redeems(), 0, "fuzz never redeemed");
    }
}
