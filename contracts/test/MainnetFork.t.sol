// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {CopyVault, ICopyRouter} from "../src/CopyVault.sol";
import {IERC20} from "../src/MiniERC20.sol";
import {ISwapRouter02, UniswapV3Adapter} from "../src/UniswapV3Adapter.sol";

interface IWETH9 {
    function deposit() external payable;
}

interface IUniswapV3Factory {
    function getPool(address, address, uint24) external view returns (address);
}

/// Fork tests against Robinhood Chain MAINNET: real Uniswap V3 pools, real
/// USDG/WETH/PONS. Proves the vault + adapter execute on the live venue with
/// zero real funds. Requires network access to the public RPC.
contract MainnetForkTest is Test {
    address constant SWAP_ROUTER = 0xCaf681a66D020601342297493863E78C959E5cb2;
    address constant V3_FACTORY = 0x1f7d7550B1b028f7571E69A784071F0205FD2EfA;
    address constant WETH = 0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73;
    address constant USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    address constant PONS = 0x39dBED3a2bd333467115dE45665cC57F813C4571;

    UniswapV3Adapter adapter;
    uint24 feeUsdgWeth;
    uint24 feePonsWeth;

    function setUp() public {
        vm.createSelectFork("https://rpc.mainnet.chain.robinhood.com");
        adapter = new UniswapV3Adapter(ISwapRouter02(SWAP_ROUTER));
        feeUsdgWeth = _findFee(USDG, WETH);
        feePonsWeth = _findFee(PONS, WETH);
    }

    function _findFee(address a, address b) internal view returns (uint24 best) {
        // deepest pool by WETH balance, not first-existing (dust pools abound)
        uint24[4] memory tiers = [uint24(100), 500, 3000, 10000];
        uint256 bestDepth;
        for (uint256 i = 0; i < 4; i++) {
            address pool = IUniswapV3Factory(V3_FACTORY).getPool(a, b, tiers[i]);
            if (pool == address(0)) continue;
            uint256 depth = IERC20(WETH).balanceOf(pool);
            if (depth > bestDepth) {
                bestDepth = depth;
                best = tiers[i];
            }
        }
        require(bestDepth > 0, "no pool");
    }

    function _getUsdg(address to, uint256 ethIn) internal returns (uint256 usdgOut) {
        vm.deal(address(this), ethIn);
        IWETH9(WETH).deposit{value: ethIn}();
        adapter.setPath(WETH, USDG, abi.encodePacked(WETH, feeUsdgWeth, USDG));
        IERC20(WETH).approve(address(adapter), ethIn);
        usdgOut = adapter.swap(WETH, USDG, ethIn, 0, to);
    }

    function test_adapterSwapsRealPools() public {
        uint256 usdg = _getUsdg(address(this), 1 ether);
        assertGt(usdg, 1000e6, "1 ETH should be > $1000 of USDG"); // USDG 6d

        // USDG -> WETH -> PONS through the real multihop path
        adapter.setPath(USDG, PONS, abi.encodePacked(USDG, feeUsdgWeth, WETH, feePonsWeth, PONS));
        adapter.setPath(PONS, USDG, abi.encodePacked(PONS, feePonsWeth, WETH, feeUsdgWeth, USDG));
        IERC20(USDG).approve(address(adapter), usdg);
        uint256 pons = adapter.swap(USDG, PONS, usdg, 0, address(this));
        assertGt(pons, 0, "received PONS");

        IERC20(PONS).approve(address(adapter), pons);
        uint256 back = adapter.swap(PONS, USDG, pons, 0, address(this));
        // round trip through two 2-hop swaps loses only fees/impact
        assertGt(back, usdg * 90 / 100, "round trip sane");
    }

    function test_vaultMirrorTradeOnRealVenue() public {
        CopyVault vault = new CopyVault(IERC20(USDG), address(this), ICopyRouter(address(adapter)));
        vault.setAllowedToken(PONS, true);
        adapter.setPath(USDG, PONS, abi.encodePacked(USDG, feeUsdgWeth, WETH, feePonsWeth, PONS));
        adapter.setPath(PONS, USDG, abi.encodePacked(PONS, feePonsWeth, WETH, feeUsdgWeth, USDG));

        uint256 usdg = _getUsdg(address(this), 1 ether);
        IERC20(USDG).approve(address(vault), usdg);
        uint256 shares = vault.deposit(usdg);
        vault.postNav(usdg);

        uint256 buyAmount = usdg * 4 / 100; // inside the 5% cap
        vault.mirrorTrade(USDG, PONS, buyAmount, 0);
        uint256 ponsHeld = IERC20(PONS).balanceOf(address(vault));
        assertGt(ponsHeld, 0, "vault holds real PONS");
        assertEq(vault.heldTokensLength(), 1);

        vault.mirrorTrade(PONS, USDG, ponsHeld, 0);
        assertEq(IERC20(PONS).balanceOf(address(vault)), 0);
        assertGt(IERC20(USDG).balanceOf(address(vault)), usdg * 95 / 100);
        assertEq(shares, vault.balanceOf(address(this)));
    }
}
