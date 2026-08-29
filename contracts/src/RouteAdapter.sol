// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ICopyRouter} from "./CopyVault.sol";
import {IERC20} from "./MiniERC20.sol";
import {ISwapRouter02} from "./UniswapV3Adapter.sol";

struct PoolKey {
    address currency0;
    address currency1;
    uint24 fee;
    int24 tickSpacing;
    address hooks;
}

interface IPoolManager {
    struct SwapParams {
        bool zeroForOne;
        int256 amountSpecified;
        uint160 sqrtPriceLimitX96;
    }

    function unlock(bytes calldata data) external returns (bytes memory);
    function swap(PoolKey memory key, SwapParams memory params, bytes calldata hookData)
        external
        returns (int256 delta);
    function sync(address currency) external;
    function settle() external payable returns (uint256);
    function take(address currency, address to, uint256 amount) external;
}

/// Execution adapter that routes one CopyVault trade through a sequence of
/// legs, mixing Uniswap V3 (SwapRouter02 multihop paths) and Uniswap V4
/// (single PoolManager pools, hooked/dynamic-fee pools included) — needed
/// because newer Robinhood Chain memecoins hold their liquidity on V4,
/// often quoted in another memecoin (e.g. SIT/AI).
///
/// Routes are owner-set per (tokenIn, tokenOut) and validated leg-by-leg for
/// continuity, so a misconfigured route cannot silently trade the wrong
/// tokens. minOut is enforced on the route's final output.
contract RouteAdapter is ICopyRouter {
    struct Leg {
        uint8 kind; // 0 = v3 path via SwapRouter02, 1 = v4 single pool
        bytes v3Path; // kind 0 only
        PoolKey key; // kind 1 only
        bool zeroForOne; // kind 1 only
    }

    address public owner;
    ISwapRouter02 public immutable swapRouter;
    IPoolManager public immutable poolManager;
    mapping(address => mapping(address => bytes)) internal routeBlobs; // abi.encode(Leg[])

    uint160 internal constant MIN_SQRT_PRICE = 4295128739;
    uint160 internal constant MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342;

    bool internal locked;

    error NotOwner();
    error NoRoute(address tokenIn, address tokenOut);
    error BadRoute();
    error NotPoolManager();
    error Reentrancy();

    constructor(ISwapRouter02 swapRouter_, IPoolManager poolManager_) {
        owner = msg.sender;
        swapRouter = swapRouter_;
        poolManager = poolManager_;
    }

    modifier nonReentrant() {
        if (locked) revert Reentrancy();
        locked = true;
        _;
        locked = false;
    }

    // ---------------------------------------------------------------- config

    function setRoute(address tokenIn, address tokenOut, Leg[] calldata legs) external {
        if (msg.sender != owner) revert NotOwner();
        if (legs.length == 0) revert BadRoute();
        address expect = tokenIn;
        for (uint256 i = 0; i < legs.length; i++) {
            if (_legInput(legs[i]) != expect) revert BadRoute();
            expect = _legOutput(legs[i]);
            if (legs[i].kind == 1) {
                // native-ETH v4 currencies unsupported in this adapter
                if (legs[i].key.currency0 == address(0) || legs[i].key.currency1 == address(0)) {
                    revert BadRoute();
                }
            } else if (legs[i].v3Path.length < 43 || (legs[i].v3Path.length - 20) % 23 != 0) {
                revert BadRoute();
            }
        }
        if (expect != tokenOut) revert BadRoute();
        routeBlobs[tokenIn][tokenOut] = abi.encode(legs);
    }

    function routeLength(address tokenIn, address tokenOut) external view returns (uint256) {
        bytes memory blob = routeBlobs[tokenIn][tokenOut];
        if (blob.length == 0) return 0;
        return abi.decode(blob, (Leg[])).length;
    }

    // ---------------------------------------------------------------- swap

    function swap(address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut, address to)
        external
        nonReentrant
        returns (uint256 amountOut)
    {
        bytes memory blob = routeBlobs[tokenIn][tokenOut];
        if (blob.length == 0) revert NoRoute(tokenIn, tokenOut);
        Leg[] memory legs = abi.decode(blob, (Leg[]));

        require(IERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn), "pull");
        uint256 amt = amountIn;
        for (uint256 i = 0; i < legs.length; i++) {
            if (legs[i].kind == 0) {
                address legIn = _legInput(legs[i]);
                IERC20(legIn).approve(address(swapRouter), amt);
                amt = swapRouter.exactInput(
                    ISwapRouter02.ExactInputParams({
                        path: legs[i].v3Path,
                        recipient: address(this),
                        amountIn: amt,
                        amountOutMinimum: 0 // route-level minOut enforced below
                    })
                );
            } else {
                amt = _v4Swap(legs[i].key, legs[i].zeroForOne, amt);
            }
        }
        require(amt >= minOut, "slippage");
        require(IERC20(tokenOut).transfer(to, amt), "payout");
        return amt;
    }

    // ---------------------------------------------------------------- v4

    function _v4Swap(PoolKey memory key, bool zeroForOne, uint256 amountIn)
        internal
        returns (uint256 amountOut)
    {
        bytes memory result = poolManager.unlock(abi.encode(key, zeroForOne, amountIn));
        amountOut = abi.decode(result, (uint256));
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        if (msg.sender != address(poolManager)) revert NotPoolManager();
        (PoolKey memory key, bool zeroForOne, uint256 amountIn) =
            abi.decode(data, (PoolKey, bool, uint256));

        int256 delta = poolManager.swap(
            key,
            IPoolManager.SwapParams({
                zeroForOne: zeroForOne,
                amountSpecified: -int256(amountIn), // exact input
                sqrtPriceLimitX96: zeroForOne ? MIN_SQRT_PRICE + 1 : MAX_SQRT_PRICE - 1
            }),
            ""
        );
        // BalanceDelta packing: amount0 = upper int128, amount1 = lower int128
        int128 amount0 = int128(delta >> 128);
        int128 amount1 = int128(delta);

        (address currencyIn, int128 inDelta, address currencyOut, int128 outDelta) = zeroForOne
            ? (key.currency0, amount0, key.currency1, amount1)
            : (key.currency1, amount1, key.currency0, amount0);
        require(inDelta < 0 && outDelta > 0, "v4 delta");
        uint256 owed = uint256(uint128(-inDelta));
        uint256 amountOut = uint256(uint128(outDelta));

        poolManager.sync(currencyIn);
        require(IERC20(currencyIn).transfer(address(poolManager), owed), "settle transfer");
        poolManager.settle();
        poolManager.take(currencyOut, address(this), amountOut);
        return abi.encode(amountOut);
    }

    // ---------------------------------------------------------------- legs

    function _legInput(Leg memory leg) internal pure returns (address a) {
        if (leg.kind == 1) return leg.zeroForOne ? leg.key.currency0 : leg.key.currency1;
        bytes memory p = leg.v3Path;
        assembly {
            a := shr(96, mload(add(p, 32)))
        }
    }

    function _legOutput(Leg memory leg) internal pure returns (address a) {
        if (leg.kind == 1) return leg.zeroForOne ? leg.key.currency1 : leg.key.currency0;
        bytes memory p = leg.v3Path;
        uint256 len = p.length;
        assembly {
            a := shr(96, mload(add(add(p, 32), sub(len, 20))))
        }
    }
}
