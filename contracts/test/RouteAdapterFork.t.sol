// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {CopyVault, ICopyRouter} from "../src/CopyVault.sol";
import {IERC20} from "../src/MiniERC20.sol";
import {ISwapRouter02} from "../src/UniswapV3Adapter.sol";
import {IPoolManager, PoolKey, RouteAdapter} from "../src/RouteAdapter.sol";

interface IWETH9F {
    function deposit() external payable;
}

interface IV3Factory {
    function getPool(address, address, uint24) external view returns (address);
}

/// Fork tests on Robinhood Chain mainnet for the mixed V3+V4 route adapter.
/// SIT's only liquidity is a hooked, dynamic-fee V4 pool quoted in AI — the
/// exact case the old V3-only adapter could not reach.
contract RouteAdapterForkTest is Test {
    address constant SWAP_ROUTER = 0xCaf681a66D020601342297493863E78C959E5cb2;
    address constant POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address constant V3_FACTORY = 0x1f7d7550B1b028f7571E69A784071F0205FD2EfA;
    address constant WETH = 0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73;
    address constant USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    address constant AI = 0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18;
    address constant SIT = 0x89dA5167eb1A0067f9B3e39A544EF8d4b9c41e18;
    address constant SIT_AI_HOOK = 0x4e3468951D49f2EEa976eD0D6e75fFCb44a9a544;

    RouteAdapter adapter;
    uint24 fUW; // USDG/WETH
    uint24 fAW; // AI/WETH

    function setUp() public {
        vm.createSelectFork("https://rpc.mainnet.chain.robinhood.com");
        adapter = new RouteAdapter(ISwapRouter02(SWAP_ROUTER), IPoolManager(POOL_MANAGER));
        fUW = _findFee(USDG, WETH);
        fAW = _findFee(AI, WETH);
    }

    function _findFee(address a, address b) internal view returns (uint24 best) {
        uint24[4] memory tiers = [uint24(100), 500, 3000, 10000];
        uint256 bestDepth;
        for (uint256 i = 0; i < 4; i++) {
            address pool = IV3Factory(V3_FACTORY).getPool(a, b, tiers[i]);
            if (pool == address(0)) continue;
            uint256 depth = IERC20(WETH).balanceOf(pool);
            if (depth > bestDepth) {
                bestDepth = depth;
                best = tiers[i];
            }
        }
        require(bestDepth > 0, "no pool");
    }

    function _sitKey() internal pure returns (PoolKey memory) {
        return PoolKey({currency0: AI, currency1: SIT, fee: 8388608, tickSpacing: 8, hooks: SIT_AI_HOOK});
    }

    function _emptyKey() internal pure returns (PoolKey memory) {
        return PoolKey(address(0), address(0), 0, 0, address(0));
    }

    function _setSitRoutes() internal {
        RouteAdapter.Leg[] memory buy = new RouteAdapter.Leg[](2);
        buy[0] = RouteAdapter.Leg({kind: 0, v3Path: abi.encodePacked(USDG, fUW, WETH, fAW, AI),
                                   key: _emptyKey(), zeroForOne: false});
        buy[1] = RouteAdapter.Leg({kind: 1, v3Path: "", key: _sitKey(), zeroForOne: true});
        adapter.setRoute(USDG, SIT, buy);

        RouteAdapter.Leg[] memory sell = new RouteAdapter.Leg[](2);
        sell[0] = RouteAdapter.Leg({kind: 1, v3Path: "", key: _sitKey(), zeroForOne: false});
        sell[1] = RouteAdapter.Leg({kind: 0, v3Path: abi.encodePacked(AI, fAW, WETH, fUW, USDG),
                                    key: _emptyKey(), zeroForOne: false});
        adapter.setRoute(SIT, USDG, sell);
    }

    function _getUsdg(uint256 ethIn) internal returns (uint256) {
        vm.deal(address(this), ethIn);
        IWETH9F(WETH).deposit{value: ethIn}();
        RouteAdapter.Leg[] memory legs = new RouteAdapter.Leg[](1);
        legs[0] = RouteAdapter.Leg({kind: 0, v3Path: abi.encodePacked(WETH, fUW, USDG),
                                    key: _emptyKey(), zeroForOne: false});
        adapter.setRoute(WETH, USDG, legs);
        IERC20(WETH).approve(address(adapter), ethIn);
        return adapter.swap(WETH, USDG, ethIn, 0, address(this));
    }

    function test_mixedV3V4RouteSitRoundTrip() public {
        _setSitRoutes();
        uint256 usdg = _getUsdg(0.05 ether); // ~$200
        IERC20(USDG).approve(address(adapter), usdg);
        uint256 sit = adapter.swap(USDG, SIT, usdg, 0, address(this));
        assertGt(sit, 0, "received SIT via v4");
        assertEq(IERC20(SIT).balanceOf(address(this)), sit);
        assertEq(IERC20(AI).balanceOf(address(adapter)), 0, "no AI stuck in adapter");

        IERC20(SIT).approve(address(adapter), sit);
        uint256 back = adapter.swap(SIT, USDG, sit, 0, address(this));
        assertGt(back, usdg * 80 / 100, "round trip sane (fees+hook+impact)");
    }

    function test_badRouteRejected() public {
        RouteAdapter.Leg[] memory legs = new RouteAdapter.Leg[](1);
        // discontinuous: claims USDG->SIT but path goes WETH->AI
        legs[0] = RouteAdapter.Leg({kind: 0, v3Path: abi.encodePacked(WETH, fAW, AI),
                                    key: _emptyKey(), zeroForOne: false});
        vm.expectRevert(RouteAdapter.BadRoute.selector);
        adapter.setRoute(USDG, SIT, legs);
    }

    function test_vaultMirrorTradeThroughV4() public {
        _setSitRoutes();
        CopyVault vault = new CopyVault(IERC20(USDG), address(this), ICopyRouter(address(adapter)));
        vault.setAllowedToken(SIT, true);

        uint256 usdg = _getUsdg(0.05 ether);
        IERC20(USDG).approve(address(vault), usdg);
        vault.deposit(usdg);
        vault.postNav(usdg);

        vault.mirrorTrade(USDG, SIT, usdg * 4 / 100, 0);
        uint256 sitHeld = IERC20(SIT).balanceOf(address(vault));
        assertGt(sitHeld, 0, "vault holds SIT bought through v4");

        vault.mirrorTrade(SIT, USDG, sitHeld, 0);
        assertEq(IERC20(SIT).balanceOf(address(vault)), 0);
        assertGt(IERC20(USDG).balanceOf(address(vault)), usdg * 90 / 100);
    }
}
